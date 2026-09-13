"""`voicedock.worker` のループを固定する（SPEC §10.0, §15.2, §22 R-23）。

**Helper のハートビートが古ければ取り込みを進めない**のがこのループの最重要の性質である。
Helper が止まっているのに「新しい録音が無い」と解釈して静かに待ち続ける状態を作ると、
**録音は取り込まれないまま、何も異常が見えない**（§22 R-23）。

**デバイスを一切見ない。**見るのは `/inbox` と `/state` だけである（§10.0）。
"""

from __future__ import annotations

import io
import json
import signal
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from tests.helpers import write_heartbeat
from voicedock.config import Config
from voicedock.db import Database, EntityType, Recording, Session
from voicedock.errors import ErrorCode
from voicedock.log import Logger
from voicedock.paths import DevicePath, partkey_for
from voicedock.states import PART_TERMINAL, PartStatus, SessionStatus
from voicedock.worker import Stopper, Worker

NOW = datetime(2026, 9, 13, 9, 0, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Tokyo"))
SESSION_KEY = "DJIMIC3:20260912"


@pytest.fixture
def logger() -> tuple[Logger, io.StringIO]:
    stream = io.StringIO()
    return Logger(level="DEBUG", fmt="text", stream=stream), stream


@pytest.fixture
def cfg(make_config: Callable[..., Config], tmp_path: Path, db_path: Path) -> Config:
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    return make_config({"database": {"path": str(db_path)}, "import": {"inbox_root": str(inbox)}})


@pytest.fixture
def alive(state_root: Path) -> Path:
    """Helper が生きている状態の `state/`。"""
    write_heartbeat(state_root, updated_at=NOW.isoformat(), helper_version="5.4.0")
    (state_root / "inventory.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "generated_at": NOW.isoformat(),
                "mount_readonly": True,
                "device_free_bytes": {},
                "devices": {},
            }
        ),
        encoding="utf-8",
    )
    return state_root


def build(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], state_root: Path
) -> Worker:
    return Worker(
        cfg=cfg,
        log=logger[0],
        database=database,
        state_root=state_root,
        sleep=lambda _seconds: None,
        clock=lambda: NOW,
    )


def set_devices(state_root: Path, devices: dict[str, list[str]]) -> None:
    (state_root / "inventory.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "generated_at": NOW.isoformat(),
                "mount_readonly": True,
                "device_free_bytes": {},
                "devices": devices,
            }
        ),
        encoding="utf-8",
    )


# --- §22 R-23: Helper の死で取り込みを止める ----------------------------


def test_a_missing_heartbeat_stops_ingest(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], state_root: Path
) -> None:
    """**`heartbeat.json` が無ければ取り込みを進めない**（§10.0 / §22 R-23）。

    「新しい録音が無い」と解釈して静かに待ち続ける状態を作らない。
    """
    runner = build(database, cfg, logger, state_root)
    assert runner.tick() is False
    assert "helper_heartbeat_stale" in logger[1].getvalue()


def test_a_stale_heartbeat_stops_ingest(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], state_root: Path
) -> None:
    old = NOW - timedelta(seconds=cfg.import_.helper_heartbeat_max_age_seconds + 1)
    write_heartbeat(state_root, updated_at=old.isoformat())
    runner = build(database, cfg, logger, state_root)
    assert runner.tick() is False
    assert "helper_heartbeat_stale" in logger[1].getvalue()


def test_an_unreadable_inventory_is_treated_as_stale(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], state_root: Path
) -> None:
    """**`inventory.json` が読めないときも同じ扱い。**不明は安全側へ倒す（§7.5）。

    heartbeat が新しくても inventory が壊れていれば、デバイスの状態が分からない。
    """
    write_heartbeat(state_root, updated_at=NOW.isoformat())
    (state_root / "inventory.json").write_text("{ broken", encoding="utf-8")
    runner = build(database, cfg, logger, state_root)
    # heartbeat が生きているので取り込みは進む（inventory は §15.2 の判定にだけ使う）
    assert runner.tick() is True
    # ただし再投入の契機にはしない（`None` を「空」として扱う）
    assert runner.last_inventory_empty is True


def test_a_live_heartbeat_proceeds(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    runner = build(database, cfg, logger, alive)
    assert runner.tick() is True
    assert "helper_heartbeat_stale" not in logger[1].getvalue()


def test_the_stale_check_uses_the_configured_limit(
    database: Database,
    make_config: Callable[..., Config],
    logger: tuple[Logger, io.StringIO],
    state_root: Path,
    tmp_path: Path,
    db_path: Path,
) -> None:
    """`import.helper_heartbeat_max_age_seconds`（V-31）を使うこと。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    cfg = make_config(
        {
            "database": {"path": str(db_path)},
            "import": {"inbox_root": str(inbox), "helper_heartbeat_max_age_seconds": 60},
        }
    )
    write_heartbeat(state_root, updated_at=(NOW - timedelta(seconds=61)).isoformat())
    assert build(database, cfg, logger, state_root).tick() is False

    write_heartbeat(state_root, updated_at=(NOW - timedelta(seconds=59)).isoformat())
    assert build(database, cfg, logger, state_root).tick() is True


# --- §10.0 の順序 --------------------------------------------------------


def test_the_tick_follows_the_spec_order(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """**§10.0 の擬似コードの順序どおりである。**

    並べ替えてはならない — `discover_parts` より先に `process_pending_parts` を呼ぶと、
    **その周回で見つかった Part が 1 周遅れる**（5 秒の poll なので実害は小さいが、
    32 Part の日で 160 秒の遅れになる）。
    """
    runner = build(database, cfg, logger, alive)
    order: list[str] = []

    def note(name: str) -> Callable[..., None]:
        def inner() -> None:
            order.append(name)

        return inner

    def note_requeue(*_args: object, **_kwargs: object) -> int:
        order.append("requeue_failed")
        return 0

    runner.discover_parts = note("discover_parts")  # type: ignore[method-assign]
    runner.close_idle_sessions = note("close_idle_sessions")  # type: ignore[method-assign]
    runner.process_pending_parts = note("process_pending_parts")  # type: ignore[method-assign]
    runner.requeue_failed = note_requeue  # type: ignore[method-assign]

    runner.tick()
    assert order == [
        "discover_parts",
        "close_idle_sessions",
        "process_pending_parts",
        "requeue_failed",
    ]


def test_pending_parts_are_ordered_by_started_at(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """**処理順は `started_at` 昇順**（古い録音から。§10.0）。"""
    for hour in (15, 9, 12):
        _add_part(database, hour=hour)
    keys = build(database, cfg, logger, alive).pending_partkeys()
    hours = [key.split("_")[-2][:2] for key in keys]
    assert hours == ["09", "12", "15"]


def test_terminal_parts_are_not_pending(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """終端状態の Part は拾わない（§9.1）。

    **`FAILED` も拾わない** — 再投入は §15.2 の契機でだけ起きる（#32）。毎周回で
    引き直すと、打ち切られた Part を 1 日 17,280 回やり直すことになる。
    """
    for index, state in enumerate(sorted(PART_TERMINAL)):
        _add_part(database, hour=9, minute=index, state=state)
    assert build(database, cfg, logger, alive).pending_partkeys() == []


def test_pending_parts_are_snapshotted(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """**一覧を先に確定させる**（§10.0）。

    処理の途中で足された Part を同じ周回で拾うと、**停止要求が効かないまま走り続けうる。**
    """
    _add_part(database, hour=9)
    runner = build(database, cfg, logger, alive)
    seen: list[str] = []

    def spy(partkey: str) -> object:
        seen.append(partkey)
        _add_part(database, hour=10)  # 処理中に増える
        return None

    import voicedock.pipeline as pipeline_module

    runner_pipeline = pipeline_module.Pipeline
    try:
        pipeline_module.Pipeline = lambda **_kwargs: type(  # type: ignore[assignment, misc]
            "Stub", (), {"process_part": staticmethod(spy)}
        )()
        runner.process_pending_parts()
    finally:
        pipeline_module.Pipeline = runner_pipeline  # type: ignore[misc]
    assert len(seen) == 1, "同じ周回で増えた Part を拾っている"


# --- §15.2 立ち上がりのエッジ -------------------------------------------


def test_requeue_is_armed_on_startup(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """契機その 2: **サービスの起動**（§15.2）。"""
    runner = build(database, cfg, logger, alive)
    assert runner.should_requeue(None, startup=True) is True


def test_startup_calls_requeue_once(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """`start()` が §9.4 の復帰処理の一部として 1 回だけ呼ぶこと。"""
    runner = build(database, cfg, logger, alive)
    calls: list[bool] = []

    def spy(inventory: object, *, startup: bool = False) -> int:
        calls.append(startup)
        return 0

    runner.requeue_failed = spy  # type: ignore[method-assign]
    runner.start()
    assert calls == [True]


def test_requeue_is_armed_on_the_rising_edge(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """契機その 1: **`devices` が前回は空で今回は非空**（§15.2）。"""
    from voicedock.device import read_inventory

    runner = build(database, cfg, logger, alive)
    runner.last_inventory_empty = True
    set_devices(alive, {"DJIMIC3": ["a.wav"]})
    assert runner.should_requeue(read_inventory(alive)) is True
    assert runner.last_inventory_empty is False


def test_requeue_is_not_armed_while_connected(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """**非空のまま続く周回では再投入しない**（§15.2）。

    毎周回の空回りを防ぐ。1 日 17,280 周で 32 件を引き直すことになる。
    """
    from voicedock.device import read_inventory

    set_devices(alive, {"DJIMIC3": ["a.wav"]})
    inventory = read_inventory(alive)
    runner = build(database, cfg, logger, alive)
    runner.last_inventory_empty = False
    assert runner.should_requeue(inventory) is False
    assert runner.should_requeue(inventory) is False, "2 周目でも立たない"


def test_requeue_is_not_armed_while_disconnected(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """空のまま続く周回でも立たない（立ち上がりが無い）。"""
    from voicedock.device import read_inventory

    runner = build(database, cfg, logger, alive)
    runner.last_inventory_empty = True
    assert runner.should_requeue(read_inventory(alive)) is False


def test_an_unreadable_inventory_is_not_a_rising_edge(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """**`inventory` が読めないときは「空」として扱う。**不明は安全側へ倒す（§7.5）。"""
    runner = build(database, cfg, logger, alive)
    runner.last_inventory_empty = True
    assert runner.should_requeue(None) is False
    assert runner.last_inventory_empty is True


def test_a_disconnect_arms_the_next_rising_edge(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """切断（非空 → 空）で次の接続を拾えるようになること。"""
    from voicedock.device import read_inventory

    runner = build(database, cfg, logger, alive)

    # **属性を直接 assert しない。**`is False` で narrowing が効いてしまい、
    # 型検査器が以降を到達不能と判断する（`should_requeue` が書き換えることを知らない）
    def empty_flag() -> bool | None:
        return runner.last_inventory_empty

    set_devices(alive, {"DJIMIC3": ["a.wav"]})
    runner.should_requeue(read_inventory(alive))
    assert empty_flag() is False

    set_devices(alive, {})
    assert not runner.should_requeue(read_inventory(alive))
    assert empty_flag() is True

    set_devices(alive, {"DJIMIC3": ["a.wav"]})
    assert runner.should_requeue(read_inventory(alive)), "切断のあとの接続を拾えていない"


def test_requeue_failed_goes_through_should_requeue(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """`requeue_failed()` が判定を飛ばさないこと。

    **判定を独立させた理由がこれである。**判定と中身をまとめていると「毎周回走らせる」
    壊し方をしても戻り値が変わらず**テストが通る**（実際に通った）。
    """
    runner = build(database, cfg, logger, alive)
    asked: list[bool] = []

    def spy(inventory: object, *, startup: bool = False) -> bool:
        asked.append(startup)
        return False

    runner.should_requeue = spy  # type: ignore[method-assign]
    assert runner.requeue_failed(None) == 0
    assert asked == [False]


# --- 停止（§18.3） ------------------------------------------------------


def test_the_stopper_only_sets_a_flag() -> None:
    """**ハンドラの中で何もしない。**

    DB を触ったりログを書いたりすると、シグナルがトランザクションの途中で届いたときに
    壊れる。**フラグを立てて、ループの区切りで止まる。**
    """
    stopper = Stopper()
    assert not stopper.should_stop()
    stopper.request(signal.SIGTERM, None)
    assert stopper.should_stop()


def test_run_stops_after_the_first_tick(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """停止要求で `service_stopping` を出して止まること（§16.4 / §18.3）。"""
    runner = build(database, cfg, logger, alive)
    ticks: list[int] = []

    def once() -> bool:
        ticks.append(1)
        runner.stopper.request()
        return True

    runner.tick = once  # type: ignore[method-assign]
    runner.run()
    assert len(ticks) == 1
    out = logger[1].getvalue()
    assert "service_started" in out
    assert "service_stopping" in out


def test_run_does_not_sleep_after_a_stop_request(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """**停止要求のあとに sleep しない。**`docker compose stop` の猶予（既定 10 秒）を
    5 秒の sleep で食い潰すと、`SIGKILL` で落とされうる。
    """
    slept: list[float] = []
    runner = Worker(
        cfg=cfg,
        log=logger[0],
        database=database,
        state_root=alive,
        sleep=lambda seconds: slept.append(seconds),
        clock=lambda: NOW,
    )

    def once() -> bool:
        runner.stopper.request()
        return True

    runner.tick = once  # type: ignore[method-assign]
    runner.run()
    assert slept == []


def test_process_pending_parts_checks_for_stop_between_parts(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """**停止要求は 1 件ごとに見る。**

    30 分の文字起こしの途中では止まれないが、次の Part へ進む前には止まれる。
    """
    for minute in range(3):
        _add_part(database, hour=9, minute=minute)
    runner = build(database, cfg, logger, alive)
    seen: list[str] = []

    import voicedock.pipeline as pipeline_module

    def spy(partkey: str) -> object:
        seen.append(partkey)
        runner.stopper.request()
        return None

    original = pipeline_module.Pipeline
    try:
        pipeline_module.Pipeline = lambda **_kwargs: type(  # type: ignore[assignment, misc]
            "Stub", (), {"process_part": staticmethod(spy)}
        )()
        runner.process_pending_parts()
    finally:
        pipeline_module.Pipeline = original  # type: ignore[misc]
    assert len(seen) == 1, "停止要求のあとも処理を続けている"


# --- §9.3 READY → MERGING のガード --------------------------------------


def test_ready_sessions_need_every_part_terminal(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """**「全 Part が終端状態」を取り違えない**（§9.3）。

    取り違えると、**進行中の Part を含むセッションを統合して本文が欠けたノートを書く。**
    そのノートが §14.1 の削除根拠になる。
    """
    database.insert_session(
        Session(
            session_key=SESSION_KEY,
            day_date="2026-09-12",
            device_id="DJIMIC3",
            status=SessionStatus.READY,
            updated_at="2026-09-12T12:00:00+09:00",
        )
    )
    _add_part(database, hour=9, state=PartStatus.RAW_SAVED, session_key=SESSION_KEY)
    _add_part(database, hour=10, state=PartStatus.TRANSCRIBING, session_key=SESSION_KEY)

    runner = build(database, cfg, logger, alive)
    assert runner.ready_session_keys() == [], "進行中の Part が在るのに READY と判定した"


def test_ready_sessions_are_returned_when_all_terminal(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    database.insert_session(
        Session(
            session_key=SESSION_KEY,
            day_date="2026-09-12",
            device_id="DJIMIC3",
            status=SessionStatus.READY,
            updated_at="2026-09-12T12:00:00+09:00",
        )
    )
    _add_part(database, hour=9, state=PartStatus.RAW_SAVED, session_key=SESSION_KEY)
    _add_part(database, hour=10, state=PartStatus.SKIPPED, session_key=SESSION_KEY)
    assert build(database, cfg, logger, alive).ready_session_keys() == [SESSION_KEY]


def test_a_session_with_no_parts_is_not_ready(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """Part を 0 件持つ `READY` は返さない。

    **`all()` は空集合で真になる。**`parts and all(...)` にしないと、Part が無い
    セッションを統合しようとして `session_empty` の経路へ落ちる（#27 の責務）。
    """
    database.insert_session(
        Session(
            session_key=SESSION_KEY,
            day_date="2026-09-12",
            device_id="DJIMIC3",
            status=SessionStatus.READY,
            updated_at="2026-09-12T12:00:00+09:00",
        )
    )
    assert build(database, cfg, logger, alive).ready_session_keys() == []


# --- §15.2 工程内リトライ（待つのは worker） ----------------------------


def failing_pipeline(
    database: Database, calls: list[str], *, code: str = ErrorCode.WHISPER_FAILED
) -> Callable[[str], None]:
    """呼ばれるたびに `NORMALIZING → FAILED` を記録する偽 Pipeline の中身。"""

    def process_part(partkey: str) -> None:
        calls.append(partkey)
        row = database.get_recording(partkey)
        assert row is not None
        if row.status != PartStatus.NORMALIZING:
            database.record_transition(
                EntityType.RECORDING,
                partkey,
                from_status=row.status,
                to_status=PartStatus.NORMALIZING,
            )
        database.record_transition(
            EntityType.RECORDING,
            partkey,
            from_status=PartStatus.NORMALIZING,
            to_status=PartStatus.FAILED,
            error_code=code,
        )

    return process_part


def run_with_stub(runner: Worker, process_part: Callable[[str], None]) -> None:
    import voicedock.pipeline as pipeline_module

    original = pipeline_module.Pipeline
    try:
        pipeline_module.Pipeline = lambda **_kwargs: type(  # type: ignore[assignment, misc]
            "Stub", (), {"process_part": staticmethod(process_part)}
        )()
        runner.process_pending_parts()
    finally:
        pipeline_module.Pipeline = original  # type: ignore[misc]


def test_the_worker_waits_the_backoff_between_attempts(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """**待つのは worker である**（§15.2）。`pipeline` は時間を持たない。

    `max_attempts` 回だけ試し、間に `backoff_seconds` を待つ。**回数と待機の両方を
    見る** — 片方だけだと「待たずに 3 回」も「1 回で 2 回待つ」も通る。
    """
    _add_part(database, hour=9)
    slept: list[float] = []
    runner = Worker(
        cfg=cfg,
        log=logger[0],
        database=database,
        state_root=alive,
        sleep=lambda seconds: slept.append(seconds),
        clock=lambda: NOW,
    )
    calls: list[str] = []
    run_with_stub(runner, failing_pipeline(database, calls))

    assert len(calls) == cfg.retry.max_attempts
    assert slept == [float(s) for s in cfg.retry.backoff_seconds[: cfg.retry.max_attempts - 1]]


def test_the_worker_does_not_retry_an_exempt_error(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """`max_attempts` の対象外は工程内で回さない（§15.2）。1 回で終わる。"""
    _add_part(database, hour=9)
    slept: list[float] = []
    runner = Worker(
        cfg=cfg,
        log=logger[0],
        database=database,
        state_root=alive,
        sleep=lambda seconds: slept.append(seconds),
        clock=lambda: NOW,
    )
    calls: list[str] = []
    run_with_stub(runner, failing_pipeline(database, calls, code=ErrorCode.HELPER_UNAVAILABLE))

    assert len(calls) == 1
    assert slept == []


def test_the_retry_loop_stops_on_request(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """**停止要求は待機の前後で見る**（§18.3）。30 秒の backoff を挟んで止まれないと
    `docker compose stop` の猶予を食い潰す。
    """
    _add_part(database, hour=9)
    slept: list[float] = []
    runner = Worker(
        cfg=cfg,
        log=logger[0],
        database=database,
        state_root=alive,
        sleep=lambda seconds: slept.append(seconds),
        clock=lambda: NOW,
    )
    calls: list[str] = []
    inner = failing_pipeline(database, calls)

    def stop_after_first(partkey: str) -> None:
        inner(partkey)
        runner.stopper.request()

    run_with_stub(runner, stop_after_first)
    assert len(calls) == 1
    assert slept == [], "停止要求のあとに待ってはならない"


def test_requeue_failed_resumes_rows(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """起動の契機で `FAILED` が直前の進行中状態へ戻ること（§15.2 / §9.4）。

    **`worker` は件数を返すだけで、規則は `pipeline` が持つ。**
    """
    partkey = _add_part(database, hour=9)
    _fail(database, partkey)
    database.update_recording(partkey, retry_count=3)

    runner = build(database, cfg, logger, alive)
    assert runner.requeue_failed(None, startup=True) == 1
    row = database.get_recording(partkey)
    assert row is not None
    assert (row.status, row.retry_count) == (PartStatus.NORMALIZING, 0)


def test_requeue_does_nothing_without_a_trigger(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], alive: Path
) -> None:
    """**契機が無ければ 1 件も触らない**（§15.2）。接続中の毎周回で引き直さない。"""
    from voicedock.device import read_inventory

    partkey = _add_part(database, hour=9)
    _fail(database, partkey)
    runner = build(database, cfg, logger, alive)
    runner.last_inventory_empty = False
    set_devices(alive, {"DJIMIC3": ["a.wav"]})

    assert runner.requeue_failed(read_inventory(alive)) == 0
    row = database.get_recording(partkey)
    assert row is not None
    assert row.status == PartStatus.FAILED


# --- 補助 ----------------------------------------------------------------


def _add_part(
    database: Database,
    *,
    hour: int,
    minute: int = 0,
    state: str = PartStatus.DISCOVERED,
    session_key: str | None = None,
) -> str:
    folder = f"TX_MIC001_20260912_{hour:02d}{minute:02d}00"
    relpath = f"{folder}/TX00_MIC001_20260912_{hour:02d}{minute:02d}00_orig.wav"
    key = partkey_for("DJIMIC3", DevicePath(PurePosixPath(relpath)))
    database.insert_recording(
        Recording(
            partkey=key,
            device_id="DJIMIC3",
            source_folder=folder,
            transmitter_id="TX00",
            mic_index=1,
            started_at=f"2026-09-12T{hour:02d}:{minute:02d}:00+09:00",
            status=state,
            updated_at="2026-09-12T12:00:00+09:00",
            duration_seconds=1.0,
            source_path=relpath,
            session_key=session_key,
        )
    )
    return key


def _fail(database: Database, partkey: str) -> None:
    """`DISCOVERED → NORMALIZING → FAILED` を実際に通す（`events` を残すため）。"""
    database.record_transition(
        EntityType.RECORDING,
        partkey,
        from_status=PartStatus.DISCOVERED,
        to_status=PartStatus.NORMALIZING,
    )
    database.record_transition(
        EntityType.RECORDING,
        partkey,
        from_status=PartStatus.NORMALIZING,
        to_status=PartStatus.FAILED,
        error_code=ErrorCode.NORMALIZE_VERIFY_FAILED,
    )
