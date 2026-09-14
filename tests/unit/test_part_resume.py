"""`FAILED` から戻した Part に受け手がいることを固定する（SPEC §9.3 / §9.4 / §15.2）。

**2026-09-15 に実機で踏んだ**（#123）。Whisper が失敗した Part が `TRANSCRIBING` に
戻され、**13 分以上そこに座った。**

`resume_failed()` は `FAILED` を**落ちた工程へ**戻す（§15.2）。戻り先は
`PART_RETRYABLE_FROM_FAILED` の 3 つだが、**各工程の入口は進行中状態を受け付けなかった。**

```python
if record.status != PartStatus.NORMALIZED or record.normalized_path is None:
    return False          # ← TRANSCRIBING はここで落ちる
```

**§15.2 の工程内リトライ（`max_attempts: 3`）は Part に対して一度も実行されていなかった。**
復帰はサービス再起動だけ（§9.4 の巻き戻し）であり、**症状は「何も起きない」**（§22 R-23）。

**これは Session 側では `ANALYZABLE` / `WRITABLE` として既に直っていた**（#97）。
同じ理屈を Part へ適用し忘れていた。

だからここでも**個別の経路ではなく不変条件を固定する** — 「戻りうる状態すべてに
受け手がいること」。工程を 1 つ足して配線を忘れたら落ちる。
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from voicedock import paths, pipeline
from voicedock.config import Config
from voicedock.db import Database, EntityType, Recording, Session
from voicedock.log import Logger
from voicedock.paths import DevicePath, SessionKey
from voicedock.states import (
    PART_RECOVERY,
    PART_RETRYABLE_FROM_FAILED,
    PartStatus,
)

JST = ZoneInfo("Asia/Tokyo")
DEVICE = "DJIMIC3"
NOW = datetime(2026, 9, 13, 9, 0, 0, tzinfo=JST)
KEY = SessionKey(f"{DEVICE}:20260912")
STARTED = datetime(2026, 9, 12, 9, 0, 0, tzinfo=JST)
FOLDER = "TX_MIC001_20260912_090000"
RELPATH = f"{FOLDER}/TX00_MIC001_20260912_090000_orig.wav"


@pytest.fixture
def cfg(make_config: Callable[..., Config]) -> Config:
    return make_config({"timezone": "Asia/Tokyo"})


@pytest.fixture
def logger() -> tuple[Logger, io.StringIO]:
    stream = io.StringIO()
    return Logger(level="DEBUG", fmt="text", stream=stream), stream


def partkey() -> str:
    return paths.partkey_for(DEVICE, DevicePath(PurePosixPath(RELPATH)))


def add_part(database: Database, *, status: str, normalized_path: str | None = None) -> str:
    key = partkey()
    if database.get_session(KEY) is None:
        database.insert_session(
            Session(
                session_key=KEY,
                day_date="2026-09-12",
                device_id=DEVICE,
                status=PartStatus.DISCOVERED and "OPEN",
                updated_at=NOW.isoformat(timespec="seconds"),
            )
        )
    database.insert_recording(
        Recording(
            partkey=key,
            device_id=DEVICE,
            source_folder=FOLDER,
            transmitter_id="TX00",
            mic_index=1,
            started_at=STARTED.isoformat(timespec="seconds"),
            status=PartStatus.DISCOVERED,
            updated_at=STARTED.isoformat(timespec="seconds"),
            duration_seconds=60.0,
            ended_at=(STARTED + timedelta(seconds=60)).isoformat(timespec="seconds"),
            source_path=RELPATH,
        )
    )
    database.conn.execute(
        "UPDATE recordings SET status = ?, session_key = ?, normalized_path = ? WHERE partkey = ?",
        (status, KEY, normalized_path, key),
    )
    database.conn.commit()
    return key


def reload(database: Database, key: str) -> Recording:
    """**`ensure_normalized_audio()` は `Recording | None` を受けない。**"""
    row = database.get_recording(key)
    assert row is not None
    return row


def transitions(database: Database) -> list[tuple[str, str]]:
    return [
        (row[0], row[1])
        for row in database.conn.execute(
            "SELECT from_status, to_status FROM events "
            "WHERE entity_type = ? AND entity_key = ? ORDER BY id",
            (EntityType.RECORDING.value, partkey()),
        )
    ]


# --- 不変条件（**これが本体**） ------------------------------------------


PART_STAGE_GUARDS = pipeline.NORMALIZABLE | pipeline.TRANSCRIBABLE | pipeline.RAW_WRITABLE


@pytest.mark.parametrize("status", sorted(PART_RETRYABLE_FROM_FAILED))
def test_every_state_resume_can_produce_has_a_handler(status: PartStatus) -> None:
    """**`resume_failed()` の戻り先すべてに受け手がいること**（§15.2）。

    `PART_RETRYABLE_FROM_FAILED` に 1 つ足して配線を忘れたら、ここで落ちる。
    **実機では 3 件すべてについて破れていた**（#123）。
    """
    assert status in PART_STAGE_GUARDS


@pytest.mark.parametrize("status", sorted(PART_RETRYABLE_FROM_FAILED))
def test_every_recovery_target_has_a_handler(status: PartStatus) -> None:
    """**§9.4 の巻き戻し先にも受け手がいること。**

    巻き戻しても受け手がいなければ、**再起動は何も直していない。**
    """
    assert PART_RECOVERY[status] in PART_STAGE_GUARDS


def test_the_stage_guards_do_not_overlap() -> None:
    """**工程の入口は重ならない。**重なると 1 つの状態を 2 つの工程が進めようとする。"""
    assert frozenset() == pipeline.NORMALIZABLE & pipeline.TRANSCRIBABLE
    assert frozenset() == pipeline.TRANSCRIBABLE & pipeline.RAW_WRITABLE
    assert frozenset() == pipeline.NORMALIZABLE & pipeline.RAW_WRITABLE


# --- 進行中状態から入っても進むこと --------------------------------------


def test_a_part_left_in_transcribing_is_picked_up(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], tmp_path: object
) -> None:
    """**これが実機で座った形である**（#123）。

    `TRANSCRIBING` の Part が `ensure_part_transcript()` に受け付けられること。
    **音声を用意しない**ので工程そのものは失敗するが、
    **「入口で門前払いされない」ことが要点**である。
    """
    key = add_part(database, status=PartStatus.TRANSCRIBING, normalized_path="/data/x/audio16k.wav")
    runner = pipeline.Pipeline(database=database, cfg=cfg, log=logger[0], now=NOW)

    runner.ensure_part_transcript(database.get_recording(key))

    # 入口を通ったなら、状態が `TRANSCRIBING` のままではない（失敗して `FAILED` になる）
    row = database.get_recording(key)
    assert row is not None
    assert row.status != PartStatus.TRANSCRIBING, "入口で門前払いされている（#123 の形）"


def test_entering_from_an_in_progress_state_records_no_phantom_transition(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    """**在りもしない遷移を events に残さない。**

    `TRANSCRIBING` から入った行に `NORMALIZED → TRANSCRIBING` を書くと、
    **一度も通っていない遷移が履歴に残る**（`ensure_analysis()` と同じ扱い）。
    """
    key = add_part(database, status=PartStatus.TRANSCRIBING, normalized_path="/data/x/audio16k.wav")
    runner = pipeline.Pipeline(database=database, cfg=cfg, log=logger[0], now=NOW)

    runner.ensure_part_transcript(database.get_recording(key))

    assert (PartStatus.NORMALIZED, PartStatus.TRANSCRIBING) not in transitions(database)


def test_a_part_left_in_normalizing_is_picked_up(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    """`NORMALIZING` も同じ（§9.4 の巻き戻しは起動時の 1 回だけである）。"""
    key = add_part(database, status=PartStatus.NORMALIZING)
    runner = pipeline.Pipeline(database=database, cfg=cfg, log=logger[0], now=NOW)

    runner.ensure_normalized_audio(reload(database, key))

    row = database.get_recording(key)
    assert row is not None
    assert row.status != PartStatus.NORMALIZING, "入口で門前払いされている"


def test_a_part_left_in_raw_writing_is_picked_up(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO]
) -> None:
    """`RAW_WRITING` も同じ。"""
    key = add_part(database, status=PartStatus.RAW_WRITING)
    runner = pipeline.Pipeline(database=database, cfg=cfg, log=logger[0], now=NOW)

    # 受け付けられること自体を見る（transcript が無いので進めはしない）
    assert runner.ensure_raw_note(database.get_recording(key)) is False
    assert (PartStatus.TRANSCRIBED, PartStatus.RAW_WRITING) not in transitions(database)


# --- 終端は進めないこと（逆向きの固定） ----------------------------------


@pytest.mark.parametrize("status", [PartStatus.FAILED, PartStatus.SKIPPED])
def test_terminal_states_are_not_picked_up(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], status: str
) -> None:
    """**`FAILED` / `SKIPPED` は工程が拾わない。**再投入は §15.2 の契機が行う。

    入口を広げすぎると、**再投入の契機を待たずに無限に回る。**
    """
    key = add_part(database, status=status, normalized_path="/data/x/audio16k.wav")
    runner = pipeline.Pipeline(database=database, cfg=cfg, log=logger[0], now=NOW)

    assert runner.ensure_normalized_audio(reload(database, key)) is False
    assert runner.ensure_part_transcript(database.get_recording(key)) is False
    assert runner.ensure_raw_note(database.get_recording(key)) is False
