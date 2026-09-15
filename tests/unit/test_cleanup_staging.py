"""`cleaner.cleanup_staging()` が何を捨て、何を残すかを固定する（SPEC §14.3）。

**`FAILED` の 16 kHz 音声は残骸ではない。**§15.2 はデバイス再接続とサービス起動で
`FAILED` を無条件に再評価すると定めている。`inbox_retain: normalized`（既定）では
inbox の原本が正規化直後に消えているため、**staging を消すと再評価する材料が無くなる。**
§10.2 の墓標によりデバイスからの再コピーも起きないので、**復旧手段が完全に失われる。**

2026-09-15 の実機で実際に起きた（#133）。MIC018 は 1 時間以上、存在しないファイルに
対して `WHISPER_FAILED` を繰り返した。
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from voicedock import cleaner, paths
from voicedock.config import Config
from voicedock.db import Database, Recording, Session
from voicedock.log import Logger
from voicedock.paths import SessionKey
from voicedock.pipeline import Pipeline
from voicedock.states import PART_TERMINAL, STAGING_DISPOSABLE, PartStatus, SessionStatus

PARTKEY = "DJIMIC3/TX_MIC001_20260912_163444/TX00_MIC001_20260912_163444_orig.wav"


def part_with(status: str, normalized_path: str | None) -> Recording:
    return Recording(
        partkey=PARTKEY,
        device_id="DJIMIC3",
        source_folder="TX_MIC001_20260912_163444",
        transmitter_id="TX00",
        mic_index=1,
        started_at="2026-09-12T16:34:44+09:00",
        status=status,
        updated_at="2026-09-12T16:40:00+09:00",
        normalized_path=normalized_path,
    )


@pytest.fixture
def audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`/data` を `tmp_path` に差し替えて実ファイルを 1 本置く。

    **呼び出しの記録ではなくファイルの有無で判定する。**「消せと頼んだか」ではなく
    「消えたか」を見る（§20.5）。
    """
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    target = tmp_path / "staging" / "4de117f89d5401c6" / "audio16k.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"RIFF....WAVE")
    return target


# --- 終端状態ごとの可否 -------------------------------------------------


@pytest.mark.parametrize("status", sorted(PART_TERMINAL))
def test_every_terminal_state_except_failed_discards_its_audio(
    status: PartStatus, audio: Path
) -> None:
    """**`FAILED` だけが残る。**他の終端は文字起こしが済んでいるので捨ててよい。

    `SKIPPED` を含むのは、`NO_SPEECH_DETECTED` も `DUPLICATE_CONTENT` も
    §15.2 の再評価対象ではないからである（§10.2 は重複時に消すと明記している）。
    """
    cleaner.cleanup_staging(part_with(status, str(audio)))

    if status is PartStatus.FAILED:
        assert audio.exists(), "FAILED の 16 kHz 音声を消した（再試行の入力が失われる）"
    else:
        assert not audio.exists(), f"{status} の残骸が消えていない"


def test_a_non_terminal_state_is_never_touched(audio: Path) -> None:
    """**許可リストで判定する。**終端ですらない状態が渡ること自体が異常だが、
    **そのときも消さない**のが安全側である（#133 の設計判断 4）。
    """
    cleaner.cleanup_staging(part_with(PartStatus.TRANSCRIBING, str(audio)))
    assert audio.exists(), "終端でない Part の 16 kHz 音声を消した"


def test_a_part_without_a_normalized_path_is_harmless(audio: Path) -> None:
    """`normalized_path` が空でも落ちない（正規化前に終端へ落ちた Part）。"""
    cleaner.cleanup_staging(part_with(PartStatus.COMPLETED, None))
    assert audio.exists(), "無関係のファイルを消した"


# --- 集合そのもの -------------------------------------------------------


def test_staging_disposable_is_terminal_minus_failed() -> None:
    """**集合そのものを固定する。**

    上の parametrize は `PART_TERMINAL` を回すので、**定数を空にすると 0 件になって
    テストが黙って消える。**#117 と同じ型の穴なので、集合を直接 assert しておく。
    """
    assert frozenset(PART_TERMINAL) - {PartStatus.FAILED} == STAGING_DISPOSABLE
    assert PartStatus.FAILED not in STAGING_DISPOSABLE
    assert PartStatus.SKIPPED in STAGING_DISPOSABLE
    assert len(STAGING_DISPOSABLE) == len(PART_TERMINAL) - 1


# --- セッション完了の経路（#133 が実際に踏まれた場所） -------------------

SESSION_KEY = SessionKey("DJIMIC3:20260912")


def _part(partkey: str, status: str, normalized: Path) -> Recording:
    return Recording(
        partkey=partkey,
        device_id="DJIMIC3",
        source_folder="TX_MIC001_20260912_163444",
        transmitter_id="TX00",
        mic_index=1,
        started_at="2026-09-12T16:34:44+09:00",
        status=status,
        updated_at="2026-09-12T16:40:00+09:00",
        session_key=str(SESSION_KEY),
        normalized_path=str(normalized),
    )


def test_completing_a_session_keeps_the_failed_parts_audio(
    tmp_path: Path,
    database: Database,
    make_config: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**1 本だけ失敗した日**がセッション完了を通っても、その 16 kHz 音声は残る。

    `EXCLUDED_FROM_MERGE = {FAILED, SKIPPED}`（#131）により失敗 Part が居ても
    セッションは `SAVED` → `CLEANUP` へ進む。**つまりこの経路は通常運用で必ず通る。**

    ここが `cleanup_staging()` を経由していることも同時に固定する — 直に
    `unlink()` を書かれたら、状態で絞っても意味が無い。
    """
    data = tmp_path / "data"
    (data / "staging").mkdir(parents=True)
    monkeypatch.setattr(paths, "DATA_ROOT", data)

    database.insert_session(
        Session(
            session_key=str(SESSION_KEY),
            day_date="2026-09-12",
            device_id="DJIMIC3",
            status=SessionStatus.SAVED,
            updated_at="2026-09-12T17:00:00+09:00",
        )
    )

    audio = {}
    for name, status in (("ok", PartStatus.RAW_SAVED), ("ng", PartStatus.FAILED)):
        target = data / "staging" / name / "audio16k.wav"
        target.parent.mkdir()
        target.write_bytes(b"RIFF....WAVE")
        audio[name] = target
        database.insert_recording(_part(f"DJIMIC3/{name}.wav", status, target))

    # **安全ロック 1 は既定のまま**（`delete_source_audio: false`）。§9.3 のこの経路が
    # Phase 7 前の通常運用そのものである
    cfg = make_config({"cleanup": {"queue_root": str(tmp_path / "queue")}})
    runner = Pipeline(
        database=database,
        cfg=cfg,
        log=Logger(level="DEBUG", fmt="text", stream=io.StringIO()),
        now=datetime(2026, 9, 13, 9, 0, tzinfo=cfg.tz),
        state_root=tmp_path / "state",
    )

    assert runner.delete_sources_if_safe(SESSION_KEY), "セッションを完了できていない"

    assert not audio["ok"].exists(), "成功した Part の残骸が消えていない"
    assert audio["ng"].exists(), "失敗した Part の 16 kHz 音声を消した（#133）"
