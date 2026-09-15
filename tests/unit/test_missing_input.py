"""文字起こしの入力が無いときの行き先を固定する（SPEC §10.6 / §9.3 / §15.1）。

**whisper を起動してはならない。**whisper-cli は `-f` のファイルを開けないと
**usage を吐いて終了コード 2 で落ちる。**そのまま `WHISPER_FAILED` として記録すると、
`error_message` の先頭 200 文字が whisper のヘルプになり、**原因を隠す。**

2026-09-15 の実機ではこれで 1 時間を失った（#135）。記録されていたのは次の文字列で、
モデルを疑い、設定を疑い、最後に staging が空であることへ辿り着いた。

```text
終了コード 2: rammar to guide decoding
  --grammar-rule RULE   [       ] top-level GBNF grammar rule name
```
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest

from voicedock import paths, transcribe
from voicedock.config import Config
from voicedock.db import Database, Recording, Session
from voicedock.errors import ErrorCode
from voicedock.log import Logger
from voicedock.paths import DevicePath, SessionKey, partkey_for
from voicedock.pipeline import Pipeline
from voicedock.states import PartStatus, SessionStatus

SESSION_KEY = SessionKey("DJIMIC3:20260912")
FOLDER = "TX_MIC001_20260912_090000"
RELPATH = f"{FOLDER}/TX00_MIC001_20260912_090000_orig.wav"
PARTKEY = partkey_for("DJIMIC3", DevicePath(PurePosixPath(RELPATH)))
STARTED = "2026-09-12T09:00:00+09:00"


@pytest.fixture
def staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    (root / "staging" / "slug").mkdir(parents=True)
    (root / "transcripts" / "parts").mkdir(parents=True)
    monkeypatch.setattr(paths, "DATA_ROOT", root)
    monkeypatch.setattr(paths, "TRANSCRIPTS_ROOT", root / "transcripts" / "parts")
    return root / "staging" / "slug"


@pytest.fixture
def runner(
    database: Database, make_config: Callable[..., Config], frozen_now: datetime, vault: Path
) -> Pipeline:
    cfg = make_config({"obsidian": {"root": str(vault)}})
    return Pipeline(
        database=database,
        cfg=cfg,
        log=Logger(level="DEBUG", fmt="text", stream=io.StringIO()),
        now=frozen_now,
    )


def add_part(database: Database, *, state: str, normalized: Path, inbox: Path | None) -> Recording:
    database.insert_session(
        Session(
            session_key=str(SESSION_KEY),
            day_date="2026-09-12",
            device_id="DJIMIC3",
            status=SessionStatus.OPEN,
            updated_at=STARTED,
        )
    )
    row = Recording(
        partkey=PARTKEY,
        device_id="DJIMIC3",
        source_folder=FOLDER,
        transmitter_id="TX00",
        mic_index=1,
        started_at=STARTED,
        status=state,
        updated_at=STARTED,
        duration_seconds=60.0,
        ended_at="2026-09-12T09:01:00+09:00",
        source_path=RELPATH,
        session_key=str(SESSION_KEY),
        inbox_path=None if inbox is None else str(inbox),
        normalized_path=str(normalized),
    )
    database.insert_recording(row)
    return row


def reloaded(database: Database) -> Recording:
    row = database.get_recording(PARTKEY)
    assert row is not None
    return row


@pytest.fixture
def forbid_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    """**呼ばれたら落とす。**「起動しない」を戻り値ではなく事実で確かめる。"""

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("入力が無いのに whisper を起動した")

    monkeypatch.setattr(transcribe, "transcribe", explode)


# --- inbox に原本が無い（実機で起きた形） --------------------------------


@pytest.mark.parametrize("state", [PartStatus.NORMALIZED, PartStatus.TRANSCRIBING])
def test_a_missing_input_fails_with_its_own_code(
    runner: Pipeline, database: Database, staging: Path, forbid_whisper: None, state: str
) -> None:
    """`WHISPER_FAILED` ではなく `NORMALIZED_MISSING` になる。

    **`TRANSCRIBING` からも入る。**実機で落ち続けていたのはこちらである
    （一度 `TRANSCRIBING` へ入ったあと `FAILED` ⇄ `TRANSCRIBING` を往復していた）。
    """
    add_part(database, state=state, normalized=staging / "audio16k.wav", inbox=None)

    assert not runner.ensure_part_transcript(reloaded(database))

    row = reloaded(database)
    assert row.status == PartStatus.FAILED
    assert row.error_code == ErrorCode.NORMALIZED_MISSING, row.error_code
    assert row.error_message is not None
    assert "採り直す" in row.error_message, "次に何をすればよいかが書かれていない"


def test_the_failure_is_recorded_from_normalizing(
    runner: Pipeline, database: Database, staging: Path, forbid_whisper: None
) -> None:
    """**再投入の戻り先が `NORMALIZING` になること**（§15.2 / §9.3）。

    `TRANSCRIBING` のまま落とすと、**入力が戻っても永久に文字起こしを試み続ける。**
    実機ではそれで 18 往復した。
    """
    add_part(
        database,
        state=PartStatus.TRANSCRIBING,
        normalized=staging / "audio16k.wav",
        inbox=None,
    )
    runner.ensure_part_transcript(reloaded(database))

    edges = [
        (row[0], row[1])
        for row in database.conn.execute(
            "SELECT from_status, to_status FROM events WHERE entity_key = ? ORDER BY id",
            (PARTKEY,),
        )
    ]
    assert edges[-1] == (PartStatus.NORMALIZING, PartStatus.FAILED), edges


# --- inbox に原本がある --------------------------------------------------


def test_an_available_source_goes_back_to_normalizing(
    runner: Pipeline, database: Database, staging: Path, tmp_path: Path, forbid_whisper: None
) -> None:
    """**作り直せるなら作り直す。**`FAILED` を経由しない。"""
    original = tmp_path / "inbox" / RELPATH
    original.parent.mkdir(parents=True)
    original.write_bytes(b"RIFF....WAVE")

    add_part(
        database,
        state=PartStatus.NORMALIZED,
        normalized=staging / "audio16k.wav",
        inbox=original,
    )

    assert not runner.ensure_part_transcript(reloaded(database))

    row = reloaded(database)
    assert row.status == PartStatus.NORMALIZING, row.status
    assert row.error_code is None, "作り直せるのに失敗として記録した"


# --- 入力が在るときは素通しする ------------------------------------------


def test_an_existing_input_still_reaches_whisper(
    runner: Pipeline, database: Database, staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**検査が通常経路を塞いでいないこと。**これが無いと「常に偽」でもテストが通る。"""
    target = staging / "audio16k.wav"
    target.write_bytes(b"RIFF....WAVE")
    add_part(database, state=PartStatus.NORMALIZED, normalized=target, inbox=None)

    seen: list[object] = []

    def fake(path: object, **_kwargs: object) -> transcribe.TranscribeResult:
        seen.append(path)
        transcript = transcribe.Transcript(
            partkey=PARTKEY,
            language="ja",
            duration_seconds=60.0,
            started_at=STARTED,
            text="おはようございます。",
            segments=(
                transcribe.TranscriptSegment(start=0.0, end=3.0, text="おはようございます。"),
            ),
        )
        target = paths.transcript_path_for(PARTKEY)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(transcript.to_document(), ensure_ascii=False), encoding="utf-8"
        )
        return transcribe.TranscribeResult(transcript=transcript, elapsed_seconds=1.0)

    monkeypatch.setattr(transcribe, "transcribe", fake)

    assert runner.ensure_part_transcript(reloaded(database))
    assert seen, "入力が在るのに whisper へ届いていない"
    assert reloaded(database).status == PartStatus.TRANSCRIBED


def test_an_empty_input_counts_as_missing(
    runner: Pipeline, database: Database, staging: Path, forbid_whisper: None
) -> None:
    """**`size > 0` まで見る**（§9.3）。0 バイトの残骸で whisper を起動しない。"""
    target = staging / "audio16k.wav"
    target.write_bytes(b"")
    add_part(database, state=PartStatus.NORMALIZED, normalized=target, inbox=None)

    assert not runner.ensure_part_transcript(reloaded(database))
    assert reloaded(database).error_code == ErrorCode.NORMALIZED_MISSING
