"""リトライを固定する（SPEC §15.2, §9.3, §15.1）。

**リトライは 2 層しかない**（§15.2）。

| 層 | 範囲 | 回数 |
|---|---|---|
| **工程内リトライ** | 1 工程の中での即時再試行 | `max_attempts` 回、`backoff_seconds` 間隔 |
| **再評価** | `FAILED` に落ちた行をもう一度流す | **無制限**（契機のたびに 1 回） |

**上限を持たないのは意図である。**v4.6 は 3 ラウンドを超えた行を「以降は自動で触らない」
状態に置いていたが、**原因が直っても人が手を動かすまで永久に戻らなかった。**
`retry` コマンドも v5.0 で削除したので、上限を残すと復帰経路が無くなる。
"""

from __future__ import annotations

import ast
import inspect
import io
from collections.abc import Callable
from pathlib import PurePosixPath

import pytest

from tests.spec_sync import (
    spec_delete_evaluation_backoff,
    spec_max_attempts_exempt_codes,
    spec_retry_count_is_unchanged_on_retry,
    spec_retry_reset_statuses,
    spec_retry_settings,
)
from voicedock import pipeline
from voicedock.config import Config
from voicedock.db import Database, EntityType, Recording, Session
from voicedock.errors import ErrorCode, counts_against_max_attempts
from voicedock.log import Logger
from voicedock.paths import DevicePath, partkey_for
from voicedock.states import (
    PART_RETRY_RESET,
    RETRY_RESET_STATUSES,
    SESSION_RETRY_RESET,
    PartStatus,
    SessionStatus,
)

SESSION_KEY = "DJIMIC3:20260912"
RELPATH = "TX_MIC001_20260912_120950/TX00_MIC001_20260912_120950_orig.wav"
PARTKEY = partkey_for("DJIMIC3", DevicePath(PurePosixPath(RELPATH)))

EXEMPT_CODES: tuple[str, ...] = tuple(
    sorted(name for name in spec_max_attempts_exempt_codes() if name in ErrorCode.__members__)
)
"""§15.2 の「`max_attempts` の対象外」のうち `ErrorCode` であるもの。"""


@pytest.fixture
def logger() -> tuple[Logger, io.StringIO]:
    stream = io.StringIO()
    return Logger(level="DEBUG", fmt="text", stream=stream), stream


@pytest.fixture
def cfg(make_config: Callable[..., Config]) -> Config:
    return make_config()


def add_session(database: Database, *, state: str = SessionStatus.READY) -> None:
    if database.get_session(SESSION_KEY) is not None:
        return
    database.insert_session(
        Session(
            session_key=SESSION_KEY,
            day_date="2026-09-12",
            device_id="DJIMIC3",
            status=state,
            updated_at="2026-09-12T12:00:00+09:00",
        )
    )


def add_part(database: Database, *, state: str = PartStatus.DISCOVERED) -> None:
    add_session(database)
    database.insert_recording(
        Recording(
            partkey=PARTKEY,
            device_id="DJIMIC3",
            source_folder=RELPATH.split("/")[0],
            transmitter_id="TX00",
            mic_index=1,
            started_at="2026-09-12T12:09:50+09:00",
            status=state,
            updated_at="2026-09-12T12:09:50+09:00",
            session_key=SESSION_KEY,
        )
    )


def fail_part(database: Database, *, code: ErrorCode = ErrorCode.WHISPER_FAILED) -> None:
    """`NORMALIZED → TRANSCRIBING → FAILED` を実際に通す（`events` を残すため）。"""
    database.record_transition(
        EntityType.RECORDING,
        PARTKEY,
        from_status=PartStatus.DISCOVERED,
        to_status=PartStatus.NORMALIZING,
    )
    database.record_transition(
        EntityType.RECORDING,
        PARTKEY,
        from_status=PartStatus.NORMALIZING,
        to_status=PartStatus.NORMALIZED,
    )
    database.record_transition(
        EntityType.RECORDING,
        PARTKEY,
        from_status=PartStatus.NORMALIZED,
        to_status=PartStatus.TRANSCRIBING,
    )
    database.record_transition(
        EntityType.RECORDING,
        PARTKEY,
        from_status=PartStatus.TRANSCRIBING,
        to_status=PartStatus.FAILED,
        error_code=code,
    )


def part(database: Database) -> Recording:
    row = database.get_recording(PARTKEY)
    assert row is not None
    return row


# --- retry_count の増減（§15.2 / v5.6→v5.7 の変更 S-1） -----------------


def test_the_spec_says_retry_does_not_change_the_counter() -> None:
    """§9.3 の 2 つの「工程内リトライ」行が `retry_count` を変えないと書いていること。

    v5.6 まで「`retry_count += 1`」と書いてあった。**`FAILED` へ入るときにも増えるので、
    両方で増やすと二重に数え、`max_attempts: 3` で実際には 4 回試す**（S-1）。
    """
    for effect in spec_retry_count_is_unchanged_on_retry():
        assert "変えない" in effect, effect
        assert "+= 1" not in effect, effect


def test_retry_count_increases_only_on_failure(database: Database) -> None:
    """**増やす場所は `FAILED` への遷移だけである**（S-1）。

    リトライ側でも増やすと二重に数え、**`max_attempts: 3` で実際には 4 回試す。**
    """
    add_part(database)
    fail_part(database)
    assert part(database).retry_count == 1

    database.record_transition(
        EntityType.RECORDING,
        PARTKEY,
        from_status=PartStatus.FAILED,
        to_status=PartStatus.TRANSCRIBING,
    )
    assert part(database).retry_count == 1, "リトライで増えてはならない"


@pytest.mark.parametrize("state", sorted(RETRY_RESET_STATUSES))
def test_passing_a_stage_resets_the_counter(database: Database, state: str) -> None:
    """**工程を通過したら 0 にリセットする**（§15.2）。

    工程をまたいで累積させると、**前段で 2 回失敗した行が後段の 1 回の失敗で
    打ち切られる**（v3.0 の不具合）。
    """
    entity = (
        EntityType.RECORDING if state in {s.value for s in PART_RETRY_RESET} else EntityType.SESSION
    )
    if entity is EntityType.RECORDING:
        add_part(database)
        fail_part(database)
        assert part(database).retry_count == 1
        database.record_transition(
            EntityType.RECORDING,
            PARTKEY,
            from_status=PartStatus.FAILED,
            to_status=state,
        )
        assert part(database).retry_count == 0
    else:
        add_session(database, state=SessionStatus.FAILED)
        database.update_session(SESSION_KEY, retry_count=2)
        database.record_transition(
            EntityType.SESSION, SESSION_KEY, from_status=SessionStatus.FAILED, to_status=state
        )
        row = database.get_session(SESSION_KEY)
        assert row is not None
        assert row.retry_count == 0


def test_the_reset_set_matches_the_spec() -> None:
    """§15.2 が名指しする 6 状態と一致すること（**SPEC から読む**）。"""
    assert spec_retry_reset_statuses() == RETRY_RESET_STATUSES
    assert len(PART_RETRY_RESET | SESSION_RETRY_RESET) == len(RETRY_RESET_STATUSES)


# --- 工程内リトライ（§15.2） --------------------------------------------


def test_the_backoff_follows_the_spec(cfg: Config) -> None:
    """`backoff_seconds` の**回数と待機秒を SPEC から取る**（§15.2）。

    **数字を直書きしない。**§15.2 の `retry:` ブロックを変えたのにテストが通ると、
    SPEC と実装が静かに食い違う。
    """
    spec = spec_retry_settings()
    backoff = spec["backoff_seconds"]
    assert isinstance(backoff, list)
    assert cfg.retry.max_attempts == spec["max_attempts"]
    assert list(cfg.retry.backoff_seconds) == backoff

    for attempt, seconds in enumerate(backoff[: cfg.retry.max_attempts - 1], start=1):
        assert pipeline.retry_delay(attempt, cfg) == float(seconds)


def test_no_delay_once_the_attempts_are_used(cfg: Config) -> None:
    """**`max_attempts` に達したら `FAILED` として残す**（§15.2）。"""
    assert pipeline.retry_delay(cfg.retry.max_attempts, cfg) is None
    assert pipeline.retry_delay(cfg.retry.max_attempts + 1, cfg) is None
    assert pipeline.retry_delay(0, cfg) is None


def test_in_process_retry_allows_three_attempts(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    """**ちょうど `max_attempts` 回試す**（S-1 の意図）。"""
    add_part(database)
    fail_part(database)

    attempts = 1  # 既に 1 回失敗している
    while True:
        delay = pipeline.in_process_retry(database, EntityType.RECORDING, PARTKEY, cfg=cfg)
        if delay is None:
            break
        assert pipeline.resume_failed(
            database, EntityType.RECORDING, PARTKEY, log=logger[0], reset_retry=False
        )
        database.record_transition(
            EntityType.RECORDING,
            PARTKEY,
            from_status=PartStatus.TRANSCRIBING,
            to_status=PartStatus.FAILED,
            error_code=ErrorCode.WHISPER_FAILED,
        )
        attempts += 1

    assert attempts == cfg.retry.max_attempts
    assert part(database).status == PartStatus.FAILED
    assert part(database).retry_count == cfg.retry.max_attempts


def test_in_process_retry_needs_the_failed_state(database: Database, cfg: Config) -> None:
    add_part(database, state=PartStatus.NORMALIZED)
    assert pipeline.in_process_retry(database, EntityType.RECORDING, PARTKEY, cfg=cfg) is None


@pytest.mark.parametrize("name", sorted(spec_max_attempts_exempt_codes()))
def test_every_exempt_name_is_a_code_or_a_state(name: str) -> None:
    """§15.2 が「`max_attempts` の対象外」と名指しする 3 つを固定する。

    **`SOURCE_DELETE_PENDING` だけは状態であって `ErrorCode` ではない**（§9.1）。
    §15.1 の「リトライ」列で扱うのではなく、`SAVED → SOURCE_DELETE_PENDING` の
    行き先として無期限に再評価される。**混同すると `ErrorCode` に足したくなる。**
    """
    if name in ErrorCode.__members__:
        assert not counts_against_max_attempts(ErrorCode(name))
    else:
        assert name in PartStatus.__members__, f"{name} は ErrorCode でも状態でもない"


@pytest.mark.parametrize("code", EXEMPT_CODES)
def test_the_exempt_codes_are_not_retried_in_process(
    database: Database, cfg: Config, code: str
) -> None:
    """**§15.2 が「`max_attempts` の対象外」と名指しするものを工程内で回さない。**

    `HELPER_UNAVAILABLE` と `DELETE_TIMEOUT` は**契機（Helper の復帰・デバイスの
    再接続）を待って無期限に再評価する。**3 秒後に試し直しても状況は変わらない。
    """
    add_part(database)
    fail_part(database, code=ErrorCode(code))
    assert pipeline.in_process_retry(database, EntityType.RECORDING, PARTKEY, cfg=cfg) is None


@pytest.mark.parametrize("code", [ErrorCode.DISK_SPACE_LOW])
def test_non_attempt_errors_are_not_retried_in_process(
    database: Database, cfg: Config, code: ErrorCode
) -> None:
    """**契機を待つものは工程内で回さない**（§15.1 の「リトライ」列）。

    `DISK_SPACE_LOW` を 3 秒後に試し直しても空かない。
    """
    add_part(database)
    fail_part(database, code=code)
    assert pipeline.in_process_retry(database, EntityType.RECORDING, PARTKEY, cfg=cfg) is None


def test_an_unknown_error_code_is_not_retried(database: Database, cfg: Config) -> None:
    """**未知のコードは安全側（試さない）へ倒す。**"""
    add_part(database)
    fail_part(database)
    database.update_recording(PARTKEY, error_code="SOMETHING_NEW")
    assert pipeline.in_process_retry(database, EntityType.RECORDING, PARTKEY, cfg=cfg) is None


def test_the_resume_target_comes_from_events(
    database: Database, logger: tuple[Logger, io.StringIO]
) -> None:
    """**リトライは最初からやり直さない**（§15.2）。戻り先は `events` が持つ。"""
    add_part(database)
    fail_part(database)
    assert pipeline.resume_failed(
        database, EntityType.RECORDING, PARTKEY, log=logger[0], reset_retry=False
    )
    assert part(database).status == PartStatus.TRANSCRIBING, "DISCOVERED へ戻してはならない"


def test_a_part_that_never_failed_cannot_resume(
    database: Database, logger: tuple[Logger, io.StringIO]
) -> None:
    add_part(database, state=PartStatus.FAILED)
    assert not pipeline.resume_failed(
        database, EntityType.RECORDING, PARTKEY, log=logger[0], reset_retry=False
    )


def test_a_non_retryable_origin_is_refused(
    database: Database, logger: tuple[Logger, io.StringIO]
) -> None:
    """`DISCOVERED` から `FAILED` へ落ちた行は戻せない（§9.3 の 2 行が許す状態だけ）。

    **`FAILED` へ入れる状態と戻れる状態を一致させてある**（`states.py` の等式）。
    """
    add_part(database)
    database.record_transition(
        EntityType.RECORDING,
        PARTKEY,
        from_status=PartStatus.DISCOVERED,
        to_status=PartStatus.FAILED,
        error_code=ErrorCode.WHISPER_FAILED,
    )
    assert not pipeline.resume_failed(
        database, EntityType.RECORDING, PARTKEY, log=logger[0], reset_retry=False
    )


# --- 再評価（§15.2）。**上限が無いことが要点** --------------------------


def test_requeue_resets_the_counter(database: Database, logger: tuple[Logger, io.StringIO]) -> None:
    """**`retry_count` を 0 に戻して無条件に再投入する**（§15.2）。"""
    add_part(database)
    fail_part(database)
    database.update_recording(PARTKEY, retry_count=3)

    assert pipeline.requeue_failed(database, log=logger[0]) == 1
    assert part(database).status == PartStatus.TRANSCRIBING
    assert part(database).retry_count == 0


def test_requeue_has_no_limit(database: Database, logger: tuple[Logger, io.StringIO]) -> None:
    """**上限を持たないのは意図である**（§15.2）。

    v4.6 は 3 ラウンドを超えた行を「以降は自動で触らない」状態に置いていたが、
    **原因が直っても人が手を動かすまで永久に戻らなかった。**`retry` コマンドも
    v5.0 で削除したので、上限を残すと復帰経路が無くなる。
    """
    add_part(database)
    fail_part(database)
    for round_number in range(5):
        assert pipeline.requeue_failed(database, log=logger[0]) == 1, round_number
        database.record_transition(
            EntityType.RECORDING,
            PARTKEY,
            from_status=PartStatus.TRANSCRIBING,
            to_status=PartStatus.FAILED,
            error_code=ErrorCode.WHISPER_FAILED,
        )
    assert part(database).status == PartStatus.FAILED


def test_requeue_covers_sessions_too(
    database: Database, logger: tuple[Logger, io.StringIO]
) -> None:
    add_session(database, state=SessionStatus.READY)
    database.record_transition(
        EntityType.SESSION,
        SESSION_KEY,
        from_status=SessionStatus.READY,
        to_status=SessionStatus.MERGING,
    )
    database.record_transition(
        EntityType.SESSION,
        SESSION_KEY,
        from_status=SessionStatus.MERGING,
        to_status=SessionStatus.FAILED,
        error_code=ErrorCode.LLM_UNAVAILABLE,
    )
    assert pipeline.requeue_failed(database, log=logger[0]) == 1
    row = database.get_session(SESSION_KEY)
    assert row is not None
    assert row.status == SessionStatus.MERGING


def test_requeue_is_recorded_in_events(
    database: Database, logger: tuple[Logger, io.StringIO]
) -> None:
    """再投入も状態遷移である（§8.4）。`from_status=FAILED` で残る。"""
    add_part(database)
    fail_part(database)
    pipeline.requeue_failed(database, log=logger[0])
    events = database.events_for(EntityType.RECORDING, PARTKEY)
    assert (events[-1].from_status, events[-1].to_status) == (
        PartStatus.FAILED,
        PartStatus.TRANSCRIBING,
    )
    assert events[-1].detail == "requeue"


def test_requeue_ignores_non_failed_rows(
    database: Database, logger: tuple[Logger, io.StringIO]
) -> None:
    add_part(database, state=PartStatus.COMPLETED)
    assert pipeline.requeue_failed(database, log=logger[0]) == 0


def source_of(func: object) -> str:
    """関数の**コードだけ**を返す（docstring とコメントを落とす）。

    素の `read_text()` で検査すると**説明文に書いた語に反応する** — 「`updated_at` を
    見ない」と書いた docstring 自体が「`updated_at` を見ている」と判定された。
    """
    tree = ast.parse(inspect.getsource(func))  # type: ignore[arg-type]
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    if ast.get_docstring(node) is not None:
        node.body = node.body[1:]
    return ast.unparse(node)


@pytest.mark.parametrize("forbidden", ["updated_at", "backoff", "max_attempts"])
def test_requeue_does_not_look_at_time(
    database: Database, logger: tuple[Logger, io.StringIO], forbidden: str
) -> None:
    """**時間も試行回数も見ない**（§15.2）。契機（呼ばれたこと）そのものが判定である。

    v5.0 で 24 時間タイマーを廃止した。**`backoff_seconds` は工程内リトライのものであり、
    再評価には掛からない。**
    """
    assert forbidden not in source_of(pipeline.requeue_failed)


def test_requeue_runs_immediately_after_the_failure(
    database: Database, logger: tuple[Logger, io.StringIO]
) -> None:
    """**待ち時間が無い。**失敗の直後に呼んでも再投入される。"""
    add_part(database)
    fail_part(database)
    assert pipeline.requeue_failed(database, log=logger[0]) == 1


def test_requeue_logs_the_count(database: Database, logger: tuple[Logger, io.StringIO]) -> None:
    """再投入したら `recovery_completed` を出す（§16.4）。**0 件なら出さない。**"""
    log, stream = logger
    assert pipeline.requeue_failed(database, log=log) == 0
    assert stream.getvalue() == "", "何も動かしていないのにログを出さない"

    add_part(database)
    fail_part(database)
    pipeline.requeue_failed(database, log=log)
    assert "recovery_completed" in stream.getvalue()
    assert "requeued=1" in stream.getvalue()


def test_requeue_retries_a_non_attempt_error(
    database: Database, logger: tuple[Logger, io.StringIO]
) -> None:
    """**再評価は `ATTEMPTS` 以外も対象にする**（§15.2）。

    工程内では回さない `DISK_SPACE_LOW` も、デバイスを繋ぎ直したときには試す価値がある
    （その間に容量が空いているかもしれない）。**`FAILED` だけを見る規則である。**
    """
    add_part(database)
    fail_part(database, code=ErrorCode.DISK_SPACE_LOW)
    assert pipeline.requeue_failed(database, log=logger[0]) == 1


# --- SAVED の backoff（§15.2 / §9.3） -----------------------------------


def test_the_delete_backoff_grows(cfg: Config) -> None:
    """`delete_evaluation_backoff_seconds`（§15.2。**SPEC から読む**）。

    **5 秒ごとに実ファイル検証を繰り返すビジーループを避ける。**
    """
    backoff = spec_delete_evaluation_backoff()
    assert list(cfg.cleanup.delete_evaluation_backoff_seconds) == backoff
    for attempts, seconds in enumerate(backoff, start=1):
        assert pipeline.delete_evaluation_delay(attempts, cfg) == float(seconds)


def test_the_delete_backoff_stops_growing(cfg: Config) -> None:
    """**無限に伸ばさない。**リストより大きくなったら最後の値を使い続ける。

    伸ばし続けると、原因が直っても次の評価が何日も先になる。
    """
    last = float(spec_delete_evaluation_backoff()[-1])
    assert pipeline.delete_evaluation_delay(99, cfg) == last


def test_the_delete_backoff_handles_zero(cfg: Config) -> None:
    """`delete_attempts` が 0（まだ一度も評価していない）でも最初の値を返す。"""
    assert pipeline.delete_evaluation_delay(0, cfg) == float(spec_delete_evaluation_backoff()[0])
