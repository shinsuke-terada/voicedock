"""セッションの再オープンと上限超過の行き先を固定する（SPEC §9.2 / §9.3 / §10.4）。

**再オープンの契機は `RAW_SAVED` である**（v5.7→v5.8 の変更 T-1）。分組は
`DISCOVERED` の時点で走るので、そこで `MERGING` へ戻すと**まだ文字起こししていない
Part を含んだまま統合し、本文が欠けた Daily ノートを書く**（§14.1 の削除根拠になる）。

**上限を超えた分は同じ日の 2 本目のセッションへ入れる**（T-2）。入れ続ければ上限が
無いのと同じで、拒めば**その Part が永久に処理されない**（§1.3 の優先順位 2 に反する）。
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from tests.spec_sync import spec_section_text
from voicedock import paths, pipeline, session
from voicedock.config import Config
from voicedock.db import Database, EntityType, Recording, Session
from voicedock.log import Logger
from voicedock.paths import DevicePath, SessionKey
from voicedock.states import SESSION_INITIAL, PartStatus, SessionStatus

JST = ZoneInfo("Asia/Tokyo")
DEVICE = "DJIMIC3"
NOW = datetime(2026, 9, 13, 9, 0, 0, tzinfo=JST)
KEY = SessionKey(f"{DEVICE}:20260912")


@pytest.fixture
def cfg(make_config: Callable[..., Config]) -> Config:
    return make_config({"timezone": "Asia/Tokyo"})


@pytest.fixture
def logger() -> tuple[Logger, io.StringIO]:
    stream = io.StringIO()
    return Logger(level="DEBUG", fmt="text", stream=stream), stream


def add_session(database: Database, *, status: str, regenerated: int = 0) -> None:
    database.insert_session(
        Session(
            session_key=KEY,
            day_date="2026-09-12",
            device_id=DEVICE,
            status=SESSION_INITIAL,
            updated_at=NOW.isoformat(timespec="seconds"),
            regenerated_count=regenerated,
        )
    )
    if status != SESSION_INITIAL:
        database.conn.execute("UPDATE sessions SET status = ? WHERE session_key = ?", (status, KEY))
        database.conn.commit()


def runner(database: Database, cfg: Config, log: Logger) -> pipeline.Pipeline:
    return pipeline.Pipeline(database=database, cfg=cfg, log=log, now=NOW)


# --- SPEC の規定そのもの（§10.4 / T-1 / T-2） ---------------------------


def test_the_spec_says_the_trigger_is_raw_saved() -> None:
    """§10.4 が「分組では状態を変えない」「契機は `RAW_SAVED`」と書いていること（T-1）。

    v5.7 までは「`OPEN` でないセッションへ Part を追加する場合は `MERGING` へ戻す」と
    書いてあった。**分組は `DISCOVERED` の時点で走る**ので、そのとおりに実装すると
    **まだ文字起こししていない Part を含んだまま統合する。**
    """
    text = spec_section_text("10.4")
    assert "セッションの状態はここでは変えない" in text
    assert "`RAW_SAVED` に達した時点" in text


def test_the_spec_defines_where_the_overflow_goes() -> None:
    """§10.4 が上限超過の行き先を定めていること（T-2）。

    v5.7 までは「暴走防止の上限」とだけ書かれ、**超えたときに何が起きるかが
    どこにも無かった。**
    """
    text = spec_section_text("10.4")
    assert "`<device_id>:<YYYYMMDD>#<n>`" in text
    assert "`n == 1` に接尾辞は付けない" in text


# --- 再オープン（§9.3 の `SAVED` / `COMPLETED` 行） ----------------------


@pytest.mark.parametrize("status", [SessionStatus.SAVED, SessionStatus.COMPLETED])
def test_a_finished_session_is_reopened(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], status: str
) -> None:
    """確定済みのセッションは `MERGING` へ戻る（§9.3）。"""
    add_session(database, status=status, regenerated=1)
    assert runner(database, cfg, logger[0]).reopen_session(KEY)

    row = database.get_session(KEY)
    assert row is not None
    assert row.status == SessionStatus.MERGING
    assert row.regenerated_count == 2, "何回作り直したかを数える"
    assert "session_reopened" in logger[1].getvalue()


@pytest.mark.parametrize(
    "status",
    [SessionStatus.OPEN, SessionStatus.READY, SessionStatus.MERGING, SessionStatus.ANALYZED],
)
def test_an_unfinished_session_is_not_reopened(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], status: str
) -> None:
    """**まだ確定していないセッションは戻さない。**次の統合で新しい Part を拾う。

    ここで戻すと `MERGING → MERGING` のような遷移を作ることになり、
    `events` が「作り直した」と「進んだ」を区別できなくなる。
    """
    add_session(database, status=status)
    assert not runner(database, cfg, logger[0]).reopen_session(KEY)

    row = database.get_session(KEY)
    assert row is not None
    assert row.status == status
    assert "session_reopened" not in logger[1].getvalue()


def test_allow_reopen_false_keeps_the_note_as_it_is(
    make_config: Callable[..., Config],
    database: Database,
    logger: tuple[Logger, io.StringIO],
) -> None:
    """`allow_reopen: false` なら何もしない（§10.4）。

    **その日のノートは最初の確定時のまま残る。**あとから来た Part の文字起こしは
    ノートに載らない（音声と transcript は残る）。
    """
    cfg = make_config({"timezone": "Asia/Tokyo", "session": {"allow_reopen": False}})
    add_session(database, status=SessionStatus.SAVED)
    assert not runner(database, cfg, logger[0]).reopen_session(KEY)

    row = database.get_session(KEY)
    assert row is not None
    assert row.status == SessionStatus.SAVED
    assert row.regenerated_count == 0


def test_reopening_a_missing_session_is_not_an_error(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    assert not runner(database, cfg, logger[0]).reopen_session(KEY)


def test_the_reopen_is_recorded_in_events(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    """作り直しも状態遷移である（§8.4）。`from_status=SAVED` で残る。"""
    add_session(database, status=SessionStatus.SAVED)
    runner(database, cfg, logger[0]).reopen_session(KEY)

    events = database.events_for(EntityType.SESSION, KEY)
    assert (events[-1].from_status, events[-1].to_status) == (
        SessionStatus.SAVED,
        SessionStatus.MERGING,
    )
    assert events[-1].detail == "reopen"


# --- 上限超過（§10.4 / T-2） --------------------------------------------


def add_part(
    database: Database, *, index: int, duration: float | None = 60.0, hour: int = 9
) -> Recording:
    started = datetime(2026, 9, 12, hour, 0, 0, tzinfo=JST) + timedelta(minutes=index)
    folder = f"TX_MIC001_20260912_{started.strftime('%H%M%S')}"
    relpath = f"{folder}/TX00_MIC001_20260912_{started.strftime('%H%M%S')}_orig.wav"
    row = Recording(
        partkey=paths.partkey_for(DEVICE, DevicePath(PurePosixPath(relpath))),
        device_id=DEVICE,
        source_folder=folder,
        transmitter_id="TX00",
        mic_index=1,
        started_at=started.isoformat(timespec="seconds"),
        status=PartStatus.DISCOVERED,
        updated_at=started.isoformat(timespec="seconds"),
        duration_seconds=duration,
        ended_at=(
            (started + timedelta(seconds=duration)).isoformat(timespec="seconds")
            if duration is not None
            else None
        ),
        source_path=relpath,
    )
    database.insert_recording(row)
    return row


def keys_of(database: Database) -> list[str]:
    rows = database.conn.execute(
        "SELECT session_key FROM recordings ORDER BY started_at"
    ).fetchall()
    return [row["session_key"] for row in rows]


def test_parts_below_the_limit_share_one_session(
    make_config: Callable[..., Config], database: Database
) -> None:
    """**上限に達しなければ 1 日 1 セッションのままである**（§10.4）。"""
    cfg = make_config({"timezone": "Asia/Tokyo", "session": {"max_parts": 4}})
    for index in range(4):
        add_part(database, index=index)
    session.group_parts(database, cfg=cfg, now=NOW)
    assert set(keys_of(database)) == {KEY}


def test_the_overflow_goes_to_a_second_session(
    make_config: Callable[..., Config], database: Database
) -> None:
    """`max_parts` を超えた分は `#2` へ（T-2）。**記録は 1 件も捨てない。**"""
    cfg = make_config({"timezone": "Asia/Tokyo", "session": {"max_parts": 2}})
    for index in range(5):
        add_part(database, index=index)
    session.group_parts(database, cfg=cfg, now=NOW)

    assert keys_of(database) == [KEY, KEY, f"{KEY}#2", f"{KEY}#2", f"{KEY}#3"]
    assert database.get_session(f"{KEY}#2") is not None
    assert database.get_session(f"{KEY}#3") is not None


def test_the_duration_limit_also_splits(
    make_config: Callable[..., Config], database: Database
) -> None:
    """`max_duration_seconds` でも割れる（T-2）。"""
    cfg = make_config(
        {"timezone": "Asia/Tokyo", "session": {"max_duration_seconds": 120, "max_parts": 99}}
    )
    for index in range(3):
        add_part(database, index=index, duration=60.0)
    session.group_parts(database, cfg=cfg, now=NOW)
    assert keys_of(database) == [KEY, KEY, f"{KEY}#2"]


def test_an_unknown_duration_counts_as_zero(
    make_config: Callable[..., Config], database: Database
) -> None:
    """**長さの分からない Part を「上限いっぱい」と見なさない**（T-2）。

    ffprobe が 1 回失敗しただけでセッションが割れると、その日のノートが 2 枚になる。
    件数の上限が別に効くので、時間側を甘くしても暴走はしない。
    """
    cfg = make_config(
        {"timezone": "Asia/Tokyo", "session": {"max_duration_seconds": 120, "max_parts": 99}}
    )
    for index in range(3):
        add_part(database, index=index, duration=None)
    session.group_parts(database, cfg=cfg, now=NOW)
    assert set(keys_of(database)) == {KEY}


def test_a_second_pass_fills_the_first_session_first(
    make_config: Callable[..., Config], database: Database
) -> None:
    """**空きのある最も小さい `n` を選ぶ**（T-2）。先に `#2` を作っても 1 本目に戻る。"""
    cfg = make_config({"timezone": "Asia/Tokyo", "session": {"max_parts": 2}})
    for index in range(3):
        add_part(database, index=index)
    session.group_parts(database, cfg=cfg, now=NOW)
    assert keys_of(database) == [KEY, KEY, f"{KEY}#2"]

    add_part(database, index=3)
    session.group_parts(database, cfg=cfg, now=NOW)
    assert keys_of(database)[-1] == f"{KEY}#2", "空いている #2 へ入れる"


# --- 鍵の形（§8.1 / T-2） ----------------------------------------------


def test_the_first_session_has_no_suffix() -> None:
    """**`n == 1` に接尾辞を付けない。**付けると既に保存したノートの
    `voicedock_session_key` と食い違い、§14.1 の削除条件が永久に偽になる。
    """
    started = datetime(2026, 8, 29, 7, 12, 4, tzinfo=JST)
    assert paths.session_key_for(DEVICE, started, tz=JST) == "DJIMIC3:20260829"
    assert paths.session_key_for(DEVICE, started, tz=JST, overflow=1) == "DJIMIC3:20260829"


def test_the_overflow_key_is_pinned() -> None:
    """固定入力に対する結果をバイト単位で固定する（§8.1 の鍵と同じ扱い）。"""
    started = datetime(2026, 8, 29, 7, 12, 4, tzinfo=JST)
    assert paths.session_key_for(DEVICE, started, tz=JST, overflow=2) == "DJIMIC3:20260829#2"


def test_the_day_ignores_the_suffix() -> None:
    """**2 本目も同じ日のものである。**`day_date` もノートのフォルダも 1 本目と同じ。"""
    assert paths.day_of_session(SessionKey("DJIMIC3:20260829#2")).isoformat() == "2026-08-29"
    assert paths.device_id_of_session(SessionKey("DJIMIC3:20260829#2")) == DEVICE


def test_the_overflow_number_is_readable() -> None:
    assert paths.overflow_of(SessionKey("DJIMIC3:20260829")) == 1
    assert paths.overflow_of(SessionKey("DJIMIC3:20260829#2")) == 2
    assert paths.next_overflow(SessionKey("DJIMIC3:20260829")) == "DJIMIC3:20260829#2"
    assert paths.next_overflow(SessionKey("DJIMIC3:20260829#2")) == "DJIMIC3:20260829#3"


@pytest.mark.parametrize("bad", ["DJIMIC3:20260829#1", "DJIMIC3:20260829#0", "DJIMIC3:20260829#x"])
def test_a_malformed_suffix_is_refused(bad: str) -> None:
    """**`#1` を作らない。**同じセッションを指す鍵が 2 つできる。"""
    with pytest.raises(ValueError):
        paths.overflow_of(SessionKey(bad))


def test_overflow_zero_is_refused() -> None:
    with pytest.raises(ValueError):
        paths.session_key_for(DEVICE, NOW, tz=JST, overflow=0)


def test_the_slug_differs_between_sessions() -> None:
    """**2 本目の中間ファイルが 1 本目を踏まない**（§11.2）。"""
    assert paths.key_slug(SessionKey("DJIMIC3:20260829")) != paths.key_slug(
        SessionKey("DJIMIC3:20260829#2")
    )


# --- 再オープン後に実際に動くこと（§10.0 の周回） ----------------------


def test_a_reopened_session_is_picked_up_in_the_same_run(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], state_root: object
) -> None:
    """**再オープンした行が同じ周回で拾われること**（§10.0）。

    `ready_session_keys()` が `READY` だけを見ていると、再オープンした `MERGING` の行は
    **次の起動まで動かない**（`recover_interrupted()` が巻き戻すのは起動時の 1 回だけ）。
    実際にその状態になっていたので、`MERGING` も拾うようにした。
    """
    from voicedock import worker

    add_session(database, status=SessionStatus.SAVED)
    part = add_part(database, index=0)
    database.conn.execute(
        "UPDATE recordings SET session_key = ?, status = ? WHERE partkey = ?",
        (KEY, PartStatus.RAW_SAVED, part.partkey),
    )
    database.conn.commit()
    assert runner(database, cfg, logger[0]).reopen_session(KEY)

    loop = worker.Worker(
        cfg=cfg,
        log=logger[0],
        database=database,
        state_root=state_root,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    assert KEY in loop.ready_session_keys()


def test_ensure_merged_continues_from_merging(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    """`MERGING` のまま残った行も進む（再オープンとクラッシュの両方に効く）。"""
    add_session(database, status=SessionStatus.MERGING)
    row = database.get_session(KEY)
    assert row is not None
    # transcript が None なら `session_empty` で `COMPLETED`（§10.8）。
    # **`False`（進めない）ではなく「進んだ結果」であることを見る**
    assert not runner(database, cfg, logger[0]).ensure_merged(row, None)
    after = database.get_session(KEY)
    assert after is not None
    assert after.status == SessionStatus.COMPLETED
    assert "session_empty" in logger[1].getvalue()


def test_reaching_raw_saved_reopens_the_session(
    database: Database,
    make_config: Callable[..., Config],
    vault: object,
    logger: tuple[Logger, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`ensure_raw_note()` が再オープンを呼ぶこと**（§9.3 の `SAVED` → `MERGING` 行）。

    **配線そのものを見る。**`reopen_session()` を直接呼ぶテストだけだと、
    **呼び出しを消しても全部緑のまま通る**（意図的に壊して確かめたら実際に通った）。
    #31 で直した不具合とまったく同じ種類の穴である。

    レンダリングは別のテスト（`test_raw_render.py`）が見るので、ここでは
    `write_raw_note()` を差し替えて「`RAW_SAVED` に達したあと何が起きるか」だけを見る。
    """
    from pathlib import Path

    from voicedock import notes, raw

    cfg = make_config({"timezone": "Asia/Tokyo", "obsidian": {"root": str(vault)}})
    add_session(database, status=SessionStatus.SAVED)
    part = add_part(database, index=0)
    database.conn.execute(
        "UPDATE recordings SET session_key = ?, status = ? WHERE partkey = ?",
        (KEY, PartStatus.TRANSCRIBED, part.partkey),
    )
    database.conn.commit()

    written = Path(cfg.obsidian.root) / "raw.md"
    written.write_text("# raw\n", encoding="utf-8")
    result = raw.RawNoteResult(
        path=written,  # type: ignore[arg-type]
        sha256="0" * 64,
        verification=(notes.CheckResult(rule="R-1", ok=True, detail=""),),
    )
    monkeypatch.setattr(
        pipeline.Pipeline,
        "_raw_parts",
        lambda _self, _key: [object()],
    )
    monkeypatch.setattr(raw, "write_raw_note", lambda *_args, **_kwargs: result)

    record = database.get_recording(part.partkey)
    assert runner(database, cfg, logger[0]).ensure_raw_note(record)

    assert database.get_recording(part.partkey).status == PartStatus.RAW_SAVED  # type: ignore[union-attr]
    row = database.get_session(KEY)
    assert row is not None
    assert row.status == SessionStatus.MERGING, "RAW_SAVED に達しても再オープンしていない"
    assert "session_reopened" in logger[1].getvalue()
