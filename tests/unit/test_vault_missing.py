"""Vault が見つからないとき、**Raw も Daily も書かずに `OBSIDIAN_NOT_FOUND` で止まる**
（SPEC §13.6 手順 0 / §15.1）。

E2E-04（Vault を利用不可にする）の中身である。**元音声は削除されず、Vault を戻して
サービスを起動し直せば復帰する**（§15.2 の 2 つの契機のうち「サービスの起動」）。

2026-09-15 の実機では、Vault を退避しても Docker が空ディレクトリを作るため
`obsidian_saved` が成功し続け、**E2E-04 の手順が検査になっていなかった**（#134）。
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest

from voicedock import llm, paths, pipeline
from voicedock.config import Config
from voicedock.db import Database, Recording, Session
from voicedock.errors import ErrorCode
from voicedock.log import Logger
from voicedock.paths import DevicePath, PartKey, SessionKey, partkey_for
from voicedock.pipeline import Pipeline, SessionOutcome
from voicedock.states import PartStatus, SessionStatus

SESSION_KEY = SessionKey("DJIMIC3:20260912")
FOLDER = "TX_MIC001_20260912_090000"
RELPATH = f"{FOLDER}/TX00_MIC001_20260912_090000_orig.wav"
PARTKEY = partkey_for("DJIMIC3", DevicePath(PurePosixPath(RELPATH)))
STARTED = "2026-09-12T09:00:00+09:00"


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "data"
    (root / "transcripts" / "parts").mkdir(parents=True)
    (root / "analysis").mkdir(parents=True)
    monkeypatch.setattr(paths, "DATA_ROOT", root)
    monkeypatch.setattr(paths, "TRANSCRIPTS_ROOT", root / "transcripts" / "parts")
    monkeypatch.setattr(paths, "ANALYSIS_ROOT", root / "analysis")


@pytest.fixture
def phantom(tmp_path: Path) -> Path:
    """**Docker が bind mount の source として作る空ディレクトリ。**目印が無い。"""
    root = tmp_path / "obsidian"
    root.mkdir()
    return root


@pytest.fixture
def cfg(make_config: Callable[..., Config], phantom: Path) -> Config:
    return make_config({"obsidian": {"root": str(phantom)}})


@pytest.fixture
def runner(database: Database, cfg: Config, frozen_now: datetime) -> Pipeline:
    return Pipeline(
        database=database,
        cfg=cfg,
        log=Logger(level="DEBUG", fmt="text", stream=io.StringIO()),
        now=frozen_now,
    )


def add_part(database: Database, *, state: str) -> Recording:
    database.insert_session(
        Session(
            session_key=str(SESSION_KEY),
            day_date="2026-09-12",
            device_id="DJIMIC3",
            status=SessionStatus.READY,
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
    )
    database.insert_recording(row)
    target = paths.transcript_path_for(PartKey(PARTKEY))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "partkey": PARTKEY,
                "language": "ja",
                "duration_seconds": 60.0,
                "started_at": STARTED,
                "text": "おはようございます。",
                "segments": [{"start": 0.0, "end": 3.0, "text": "おはようございます。"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return row


ANALYSIS = {
    "title": "開発の一日",
    "summary": "削除条件を整理した。",
    "key_points": ["論理式に落とした"],
    "tasks": [],
    "decisions": [],
    "ideas": [],
    "tags": ["VoiceDock"],
}


def stub_analysis(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """`llm.analyze_session` を差し替える。**ネットワークを使わない**（§20.2）。"""

    def fake(_transcript: object, **_kwargs: object) -> llm.SessionAnalysis:
        return llm.SessionAnalysis(
            result=llm.build_schema(cfg).model_validate(ANALYSIS),
            chunks=(),
            partials=(),
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(llm, "analyze_session", fake)


def reloaded(database: Database) -> Recording:
    row = database.get_recording(PARTKEY)
    assert row is not None
    return row


# --- Raw ノート ---------------------------------------------------------


def test_the_raw_note_is_not_written_into_a_phantom_vault(
    runner: Pipeline, database: Database, phantom: Path
) -> None:
    """**Raw 側でも確かめる。**Daily だけに置くと、Raw を幻へ書いたあとに気づくことになる。"""
    add_part(database, state=PartStatus.TRANSCRIBED)

    assert not runner.ensure_raw_note(reloaded(database))

    row = reloaded(database)
    assert row.status == PartStatus.FAILED
    assert row.error_code == ErrorCode.OBSIDIAN_NOT_FOUND, row.error_code
    assert list(phantom.rglob("*.md")) == [], "幻の Vault へ書いた"


def test_the_raw_note_is_written_once_the_vault_is_back(
    runner: Pipeline, database: Database, phantom: Path
) -> None:
    """**復帰の契機はサービスの起動である**（§15.2）。目印が戻れば次の評価で書ける。"""
    add_part(database, state=PartStatus.TRANSCRIBED)
    assert not runner.ensure_raw_note(reloaded(database))
    assert reloaded(database).status == PartStatus.FAILED

    (phantom / ".obsidian").mkdir()
    # **サービス起動が復帰の契機である**（§15.2）。`worker.requeue_failed()` が
    # 呼ぶものをそのまま呼ぶ — 手で遷移を書くと、配線が抜けていても通る
    assert pipeline.requeue_failed(database, log=runner.log, now=runner.now) == 1

    assert runner.ensure_raw_note(reloaded(database)), "Vault を戻しても復帰しない"
    assert reloaded(database).status == PartStatus.RAW_SAVED
    assert list(phantom.rglob("*.md")), "Raw ノートが書かれていない"


# --- Daily ノート -------------------------------------------------------


def test_the_daily_note_is_not_written_into_a_phantom_vault(
    runner: Pipeline,
    database: Database,
    cfg: Config,
    phantom: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**統合と解析までは通り、書き込みの手前で止まる。**

    `process_session` を通すのは、**解析が済んだ状態まで自力で運ぶ**ためである
    （`ensure_daily_note` は `ANALYZED` / `WRITING` でなければ何もしない）。
    """
    add_part(database, state=PartStatus.RAW_SAVED)
    stub_analysis(monkeypatch, cfg)

    assert runner.process_session(SESSION_KEY) is not SessionOutcome.SAVED

    row = database.get_session(SESSION_KEY)
    assert row is not None
    assert row.status == SessionStatus.FAILED, row.status
    assert row.error_code == ErrorCode.OBSIDIAN_NOT_FOUND, row.error_code
    assert list(phantom.rglob("*.md")) == [], "幻の Vault へ書いた"


def test_the_source_audio_is_never_requested_for_deletion(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**E2E-04 の判定条件。**Vault が無いあいだ、元音声の削除要求は書かれない。"""
    add_part(database, state=PartStatus.RAW_SAVED)
    stub_analysis(monkeypatch, cfg)
    runner.process_session(SESSION_KEY)

    assert not runner.delete_sources_if_safe(SESSION_KEY), "Vault が無いのに後始末へ進んだ"


# --- 無効化 -------------------------------------------------------------


def test_an_empty_marker_lets_the_write_through(
    make_config: Callable[..., Config],
    database: Database,
    frozen_now: datetime,
    phantom: Path,
) -> None:
    """**逃げ道**（§13.6）。`vault_marker: ""` なら目印を見ない。"""
    cfg = make_config({"obsidian": {"root": str(phantom), "vault_marker": ""}})
    runner = Pipeline(
        database=database,
        cfg=cfg,
        log=Logger(level="DEBUG", fmt="text", stream=io.StringIO()),
        now=frozen_now,
    )
    add_part(database, state=PartStatus.TRANSCRIBED)

    assert runner.ensure_raw_note(reloaded(database)), "無効化しても書けない"
    assert reloaded(database).status == PartStatus.RAW_SAVED
