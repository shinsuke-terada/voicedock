"""`voicedock.pipeline` の `ensure_*` 契約を固定する（SPEC §11.3, §9.4）。

**この契約が安全規範そのものである。**「偽を返したら以降の工程を実行しない」は、
#36 で `delete_sources_if_safe()` が保留を返したときに `cleanup_staging()` と
`mark_completed()` を呼んではならない（§14.3）という規範の一般形である。**いま枠として
固定しておかないと、削除を有効にする段で「なぜか進んでしまう」経路が残る。**

**すべての `ensure_*` は冪等である。**各関数は先頭で §9.4 の表に従って「既に完了して
いるか」を確認し、完了していれば即座に真を返す。
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from voicedock import pipeline
from voicedock.config import Config
from voicedock.db import Database, EntityType, Recording, Session
from voicedock.errors import ErrorCode
from voicedock.log import Logger
from voicedock.paths import DevicePath, PartKey, partkey_for
from voicedock.pipeline import PartOutcome, Pipeline
from voicedock.states import PartStatus, SessionStatus

RELPATH = "TX_MIC001_20260912_120950/TX00_MIC001_20260912_120950_orig.wav"
PARTKEY = partkey_for("DJIMIC3", DevicePath(PurePosixPath(RELPATH)))
SESSION_KEY = "DJIMIC3:20260912"
STARTED_AT = "2026-09-12T12:09:50+09:00"


@pytest.fixture
def logger() -> tuple[Logger, io.StringIO]:
    stream = io.StringIO()
    return Logger(level="DEBUG", fmt="text", stream=stream), stream


@pytest.fixture
def cfg(make_config: Callable[..., Config]) -> Config:
    return make_config()


@pytest.fixture
def runner(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], frozen_now: datetime
) -> Pipeline:
    return Pipeline(database=database, cfg=cfg, log=logger[0], now=frozen_now)


def add_part(
    database: Database, *, state: str = PartStatus.DISCOVERED, **columns: object
) -> Recording:
    """`recordings` へ 1 行。**`sessions` を先に作る** — `session_key` に FK がある（§8.2）。"""
    if database.get_session(SESSION_KEY) is None:
        add_session(database)
    row = Recording(
        partkey=PARTKEY,
        device_id="DJIMIC3",
        source_folder=RELPATH.split("/")[0],
        transmitter_id="TX00",
        mic_index=1,
        started_at=STARTED_AT,
        status=state,
        updated_at=STARTED_AT,
        duration_seconds=1.5,
        source_path=RELPATH,
        session_key=SESSION_KEY,
        **columns,  # type: ignore[arg-type]
    )
    database.insert_recording(row)
    return row


def add_session(database: Database, *, state: str = SessionStatus.OPEN) -> None:
    if database.get_session(SESSION_KEY) is not None:
        return
    database.insert_session(
        Session(
            session_key=SESSION_KEY,
            day_date="2026-09-12",
            device_id="DJIMIC3",
            status=state,
            updated_at=STARTED_AT,
        )
    )


def status_of(database: Database) -> str:
    row = database.get_recording(PARTKEY)
    assert row is not None
    return row.status


# --- 「偽なら以降を呼ばない」（§11.3 の中核） ---------------------------


def test_a_false_ensure_stops_the_rest(runner: Pipeline, database: Database) -> None:
    """**偽を返したら以降の工程を実行しない**（§11.3）。

    これが #36 の `delete_sources_if_safe()` が保留を返したときに
    `cleanup_staging()` / `mark_completed()` を呼んではならない規範の一般形である。
    """
    add_part(database)
    calls: list[str] = []

    def record(name: str, value: bool) -> Callable[..., bool]:
        def stub(*_args: object, **_kwargs: object) -> bool:
            calls.append(name)
            return value

        return stub

    runner.ensure_normalized_audio = record("normalize", False)  # type: ignore[method-assign]
    runner.ensure_part_transcript = record("transcribe", True)  # type: ignore[method-assign]
    runner.ensure_raw_note = record("raw", True)  # type: ignore[method-assign]

    assert runner.process_part(PartKey(PARTKEY)) is PartOutcome.STOPPED
    assert calls == ["normalize"], "偽を返したのに以降が呼ばれた"


def test_a_false_transcript_stops_the_raw_note(runner: Pipeline, database: Database) -> None:
    add_part(database)
    calls: list[str] = []

    def stub(name: str, value: bool) -> Callable[..., bool]:
        def inner(*_args: object, **_kwargs: object) -> bool:
            calls.append(name)
            return value

        return inner

    runner.ensure_normalized_audio = stub("normalize", True)  # type: ignore[method-assign]
    runner.ensure_part_transcript = stub("transcribe", False)  # type: ignore[method-assign]
    runner.ensure_raw_note = stub("raw", True)  # type: ignore[method-assign]

    assert runner.process_part(PartKey(PARTKEY)) is PartOutcome.STOPPED
    assert calls == ["normalize", "transcribe"]


def test_all_true_reaches_ready_for_session(runner: Pipeline, database: Database) -> None:
    add_part(database)

    def yes(*_args: object, **_kwargs: object) -> bool:
        return True

    runner.ensure_normalized_audio = yes  # type: ignore[method-assign]
    runner.ensure_part_transcript = yes  # type: ignore[method-assign]
    runner.ensure_raw_note = yes  # type: ignore[method-assign]
    assert runner.process_part(PartKey(PARTKEY)) is PartOutcome.READY_FOR_SESSION


def test_a_missing_part_stops(runner: Pipeline) -> None:
    """DB に無い鍵でも落ちない（周回の途中で `SKIPPED` になった Part を拾いうる）。"""
    assert runner.process_part(PartKey("DJIMIC3/absent/absent_orig.wav")) is PartOutcome.STOPPED


# --- 冪等性（§9.4 の「完了済みステップを飛ばす」） ----------------------


@pytest.mark.parametrize("state", sorted(pipeline.NORMALIZED_OR_BEYOND))
def test_normalize_is_skipped_when_already_done(
    runner: Pipeline, database: Database, state: str
) -> None:
    """**冪等。**`NORMALIZED` 以降なら即座に真を返す（ffmpeg を起動しない）。"""
    record = add_part(database, state=state)
    assert runner.ensure_normalized_audio(record)
    assert status_of(database) == state, "状態を動かしてはならない"


@pytest.mark.parametrize("state", sorted(pipeline.TRANSCRIBED_OR_BEYOND))
def test_transcript_is_skipped_when_already_done(
    runner: Pipeline, database: Database, state: str
) -> None:
    record = add_part(database, state=state)
    assert runner.ensure_part_transcript(record)
    assert status_of(database) == state


@pytest.mark.parametrize("state", sorted(pipeline.RAW_SAVED_OR_BEYOND))
def test_raw_note_is_skipped_when_already_done(
    runner: Pipeline, database: Database, state: str
) -> None:
    record = add_part(database, state=state)
    assert runner.ensure_raw_note(record)
    assert status_of(database) == state


@pytest.mark.parametrize(
    ("stage", "state"),
    [
        ("ensure_normalized_audio", PartStatus.FAILED),
        ("ensure_normalized_audio", PartStatus.SKIPPED),
        ("ensure_normalized_audio", PartStatus.NORMALIZING),
        ("ensure_part_transcript", PartStatus.DISCOVERED),
        ("ensure_part_transcript", PartStatus.FAILED),
        ("ensure_raw_note", PartStatus.NORMALIZED),
        ("ensure_raw_note", PartStatus.FAILED),
    ],
)
def test_a_wrong_state_does_not_advance(
    runner: Pipeline, database: Database, stage: str, state: str
) -> None:
    """**手前の状態でなければ進めない。**

    `FAILED` の再投入は §15.2（#32）、`NORMALIZING` は §9.4 の巻き戻しが扱う。
    **ここで勝手に進めると、巻き戻し前の部分出力を「完了」として扱いうる。**
    """
    record = add_part(database, state=state)
    assert getattr(runner, stage)(record) is False
    assert status_of(database) == state


# --- §9.3 取り込み直前の再確認 ------------------------------------------


def test_a_missing_source_is_skipped(
    runner: Pipeline, database: Database, logger: tuple[Logger, io.StringIO], tmp_path: Path
) -> None:
    """§9.3: `_orig` が存在しない / `size == 0` なら `SKIPPED`。

    **デバイス上のファイルには触れない。**`SOURCE_MISSING` を記録して `part_skipped` を出す。
    """
    record = add_part(database, inbox_path=str(tmp_path / "absent.wav"))
    assert runner.ensure_normalized_audio(record) is False
    assert status_of(database) == PartStatus.SKIPPED
    row = database.get_recording(PARTKEY)
    assert row is not None
    assert row.error_code == ErrorCode.SOURCE_MISSING
    assert "part_skipped" in logger[1].getvalue()
    assert "reason=source_missing" in logger[1].getvalue()


def test_a_zero_byte_source_is_skipped(
    runner: Pipeline, database: Database, tmp_path: Path
) -> None:
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    record = add_part(database, inbox_path=str(empty))
    assert runner.ensure_normalized_audio(record) is False
    assert status_of(database) == PartStatus.SKIPPED


def test_no_inbox_path_is_skipped(runner: Pipeline, database: Database) -> None:
    """`inbox_path` が `NULL`（変換後に消された後の再実行）でも落ちない。"""
    record = add_part(database, inbox_path=None)
    assert runner.ensure_normalized_audio(record) is False
    assert status_of(database) == PartStatus.SKIPPED


# --- §9.3 空き容量はガードである ----------------------------------------


def test_disk_space_low_does_not_fail_the_part(
    database: Database,
    make_config: Callable[..., Config],
    logger: tuple[Logger, io.StringIO],
    frozen_now: datetime,
    tmp_path: Path,
) -> None:
    """**空き容量は `DISCOVERED → NORMALIZING` のガードである**（§9.3）。

    遷移しないので `FAILED` にもならず、**次の周回で再評価される。**§15.1 が
    `DISK_SPACE_LOW` を「次回ポーリング」に分類しているのと整合する。
    **`FAILED` にすると `max_attempts` を 3 回消費して打ち切られる。**
    """
    source = tmp_path / "a.wav"
    source.write_bytes(b"RIFF")
    cfg = make_config({"import": {"free_space_multiplier": 10.0**12}})
    runner = Pipeline(database=database, cfg=cfg, log=logger[0], now=frozen_now)
    record = add_part(database, inbox_path=str(source))

    assert runner.ensure_normalized_audio(record) is False
    assert status_of(database) == PartStatus.DISCOVERED, "FAILED にしてはならない"
    assert "disk_space_low" in logger[1].getvalue()
    row = database.get_recording(PARTKEY)
    assert row is not None
    assert row.retry_count == 0, "リトライ回数を消費してはならない"


# --- 状態を変えるのは record_transition だけ（§8.4） --------------------


def test_update_recording_rejects_status(database: Database) -> None:
    """**`status` を列の更新で書けない**（§8.4）。

    書けると「`events` の無い状態変化」が生まれ、§8.4 の「すべての状態遷移を残す」が
    崩れる。**`events` は削除事故の事後追跡の唯一の手段である。**
    """
    add_part(database)
    with pytest.raises(ValueError, match="record_transition"):
        database.update_recording(PARTKEY, status=PartStatus.COMPLETED)


def test_update_recording_rejects_unknown_columns(database: Database) -> None:
    add_part(database)
    with pytest.raises(ValueError, match="に無い列"):
        database.update_recording(PARTKEY, nonexistent_column=1)


def test_update_recording_touches_updated_at(database: Database, frozen_now: datetime) -> None:
    """`updated_at` は常に更新する（**「いつの情報か」が分からない行を残さない**）。"""
    add_part(database)
    later = frozen_now + timedelta(hours=1)
    database.update_recording(PARTKEY, sha256="a" * 64, now=later)
    row = database.get_recording(PARTKEY)
    assert row is not None
    assert row.sha256 == "a" * 64
    assert row.updated_at == later.isoformat(timespec="seconds")


def test_update_recording_truncates_the_error_message(database: Database) -> None:
    """§8.4 / §16.3: 200 文字で切り詰める。**例外にしない。**"""
    add_part(database)
    database.update_recording(PARTKEY, error_message="あ" * 500)
    row = database.get_recording(PARTKEY)
    assert row is not None
    assert row.error_message is not None
    assert len(row.error_message) <= 200


# --- §9.4 クラッシュリカバリ --------------------------------------------


def test_recovery_rolls_back_every_in_progress_state(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    """§9.4 の表どおりに巻き戻すこと。

    **`SOURCE_DELETE_PENDING` は進行中ではない**（巻き戻しの行き先そのもの）。名前が
    `ING` で終わるため接尾辞だけで判定すると取り違える。逆に `CLEANUP` は `ING` で
    終わらないが進行中である。
    """
    from voicedock.states import PART_RECOVERY

    for index, (state, _target) in enumerate(PART_RECOVERY.items()):
        folder = f"TX_MIC001_20260912_1{index:05d}"
        database.insert_recording(
            Recording(
                partkey=partkey_for("DJIMIC3", DevicePath(PurePosixPath(f"{folder}/x_orig.wav"))),
                device_id="DJIMIC3",
                source_folder=folder,
                transmitter_id="TX00",
                mic_index=1,
                started_at=STARTED_AT,
                status=state,
                updated_at=STARTED_AT,
                source_path=f"{folder}/x_orig.wav",
            )
        )
    moved = pipeline.recover_interrupted(database, cfg=cfg, log=logger[0])
    assert moved == len(PART_RECOVERY)
    assert "recovery_completed" in logger[1].getvalue()
    for state, target in PART_RECOVERY.items():
        rows = database.conn.execute(
            "SELECT COUNT(*) AS n FROM recordings WHERE status = ?", (state,)
        ).fetchone()
        assert rows["n"] == 0, f"{state} が残っている"
        rows = database.conn.execute(
            "SELECT COUNT(*) AS n FROM recordings WHERE status = ?", (target,)
        ).fetchone()
        assert rows["n"] >= 1


def test_recovery_does_not_touch_source_delete_pending(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    """**`SOURCE_DELETE_PENDING` は巻き戻さない**（§9.4）。

    接尾辞 `ING` で判定すると取り違える。**巻き戻すと削除要求が無限に再投入される。**
    """
    add_part(database, state=PartStatus.SOURCE_DELETE_PENDING)
    assert pipeline.recover_interrupted(database, cfg=cfg, log=logger[0]) == 0
    assert status_of(database) == PartStatus.SOURCE_DELETE_PENDING


def test_recovery_is_quiet_when_nothing_is_interrupted(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    """**何も無ければログを出さない。**毎回の起動で `recovery_completed` が出ると、
    本当の復帰が起きたときに気づけない。
    """
    add_part(database, state=PartStatus.COMPLETED)
    assert pipeline.recover_interrupted(database, cfg=cfg, log=logger[0]) == 0
    assert "recovery_completed" not in logger[1].getvalue()


def test_recovery_removes_the_partial_output(
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**部分出力を削除してから巻き戻す**（§9.4）。

    残すと次回の再開で「変換済み」と誤認する（`audio.normalize()` の冪等判定は
    出力を検証して再利用するので、検証を通る中途半端な出力が最も危ない）。
    """
    from voicedock import paths

    data = tmp_path / "data"
    staging = data / "staging" / "x"
    staging.mkdir(parents=True)
    partial = staging / "audio16k.wav"
    partial.write_bytes(b"partial")
    monkeypatch.setattr(paths, "DATA_ROOT", data)

    add_part(database, state=PartStatus.NORMALIZING, normalized_path=str(partial))
    pipeline.recover_interrupted(database, cfg=cfg, log=logger[0])
    assert not partial.exists(), "部分出力が残っている"
    assert status_of(database) == PartStatus.DISCOVERED


def test_recovery_rolls_back_sessions(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    add_session(database, state=SessionStatus.MERGING)
    assert pipeline.recover_interrupted(database, cfg=cfg, log=logger[0]) == 1
    row = database.get_session(SESSION_KEY)
    assert row is not None
    assert row.status == SessionStatus.READY


def test_recovery_writes_events(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    """巻き戻しも状態遷移である（§8.4）。**`events` に残す。**"""
    add_part(database, state=PartStatus.NORMALIZING)
    pipeline.recover_interrupted(database, cfg=cfg, log=logger[0])
    events = database.events_for(EntityType.RECORDING, PARTKEY)
    assert events[-1].to_status == PartStatus.DISCOVERED
    assert events[-1].detail == "recovery"


def test_close_stale_open_sessions(database: Database, cfg: Config, frozen_now: datetime) -> None:
    """§9.4: 日付が過去の `OPEN` を `READY` へ。"""
    add_session(database, state=SessionStatus.OPEN)
    later = frozen_now + timedelta(days=2)
    assert pipeline.close_stale_open_sessions(database, cfg=cfg, now=later) == 1
    row = database.get_session(SESSION_KEY)
    assert row is not None
    assert row.status == SessionStatus.READY
