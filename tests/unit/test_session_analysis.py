"""セッション統合と LLM 解析の pipeline 統合を固定する（SPEC §10.8, §10.9, §11.3）。

**最重要の安全性質はこれである: `LLM_INVALID_JSON` でも文字起こしは失われない。**
Raw ノートは既に Vault へ保存済みであり、元音声も削除されない — §14.1 が
`analysis_path` の妥当性を要求するため、`FAILED` では `analysis_path` が `NULL` の
ままになり**自動的に保証される。**

テストは**ネットワークを一切使わない**（§20.2）。
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from voicedock import llm, paths, pipeline, session
from voicedock.config import Config
from voicedock.db import Database, EntityType, Recording, Session
from voicedock.errors import ErrorCode
from voicedock.log import Logger
from voicedock.paths import DevicePath, PartKey, SessionKey, partkey_for
from voicedock.pipeline import Pipeline, SessionOutcome
from voicedock.states import PartStatus, SessionStatus

SESSION_KEY = SessionKey("DJIMIC3:20260912")
ANALYSIS = {
    "title": "開発の一日",
    "summary": "削除条件を整理した。",
    "key_points": ["論理式に落とした"],
    "tasks": [{"text": "ND テストを書く", "due": None}],
    "decisions": [],
    "ideas": [],
    "tags": ["VoiceDock"],
}


@pytest.fixture
def logger() -> tuple[Logger, io.StringIO]:
    stream = io.StringIO()
    return Logger(level="DEBUG", fmt="text", stream=stream), stream


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    (root / "transcripts" / "parts").mkdir(parents=True)
    (root / "analysis").mkdir(parents=True)
    monkeypatch.setattr(paths, "DATA_ROOT", root)
    monkeypatch.setattr(paths, "TRANSCRIPTS_ROOT", root / "transcripts" / "parts")
    monkeypatch.setattr(paths, "ANALYSIS_ROOT", root / "analysis")
    return root


@pytest.fixture
def cfg(make_config: Callable[..., Config], tmp_path: Path) -> Config:
    """Vault を `tmp_path` へ向ける。

    **#28 で `ensure_daily_note()` が繋がったので、Vault が無いと `FAILED` になる。**
    ここで見たいのは統合と解析なので、書ける Vault を与えて最後まで通す。
    """
    vault = tmp_path / "obsidian"
    (vault / ".obsidian").mkdir(parents=True, exist_ok=True)  # §13.6 の目印
    return make_config({"obsidian": {"root": str(vault)}})


@pytest.fixture
def runner(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], frozen_now: datetime
) -> Pipeline:
    return Pipeline(database=database, cfg=cfg, log=logger[0], now=frozen_now)


def add_session(database: Database, *, state: str = SessionStatus.READY) -> None:
    database.insert_session(
        Session(
            session_key=SESSION_KEY,
            day_date="2026-09-12",
            device_id="DJIMIC3",
            status=state,
            updated_at="2026-09-12T12:00:00+09:00",
        )
    )


def add_part(
    database: Database,
    *,
    hour: int = 9,
    state: str = PartStatus.RAW_SAVED,
    text: str | None = "おはようございます。",
) -> PartKey:
    folder = f"TX_MIC001_20260912_{hour:02d}0000"
    relpath = f"{folder}/TX00_MIC001_20260912_{hour:02d}0000_orig.wav"
    key = partkey_for("DJIMIC3", DevicePath(PurePosixPath(relpath)))
    started = f"2026-09-12T{hour:02d}:00:00+09:00"
    database.insert_recording(
        Recording(
            partkey=key,
            device_id="DJIMIC3",
            source_folder=folder,
            transmitter_id="TX00",
            mic_index=1,
            started_at=started,
            status=state,
            updated_at=started,
            duration_seconds=60.0,
            source_path=relpath,
            session_key=SESSION_KEY,
            ended_at=f"2026-09-12T{hour:02d}:01:00+09:00",
        )
    )
    if text is not None:
        path = paths.transcript_path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "partkey": key,
                    "language": "ja",
                    "duration_seconds": 60.0,
                    "started_at": started,
                    "text": text,
                    "segments": [{"start": 0.0, "end": 3.0, "text": text}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return key


def stub_analysis(
    monkeypatch: pytest.MonkeyPatch,
    cfg: Config,
    *,
    document: dict[str, object] | None = None,
    error: ErrorCode | None = None,
    chunks: int = 1,
) -> list[object]:
    """`llm.analyze_session` を差し替える。**ネットワークを使わない**（§20.2）。"""
    calls: list[object] = []

    def fake(transcript: object, **_kwargs: object) -> llm.SessionAnalysis:
        calls.append(transcript)
        if error is not None:
            return llm.SessionAnalysis(
                result=None, chunks=(), partials=(), error_code=error, error_message="stub"
            )
        model = llm.build_schema(cfg)
        partial_model = llm.build_schema(cfg, partial=True)
        # **Map 中間結果を `summary` と違う文面にする。**同じにすると、保存済みを
        # 使っているのか §13.4 の代替経路へ落ちているのかがテストで区別できない
        partials = tuple(
            partial_model.model_validate(
                {"summary": f"チャンク {index}", "key_points": [f"Map 由来の点 {index}"]}
            )
            for index in range(chunks)
        )
        start = datetime(2026, 9, 12, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        made_chunks = tuple(
            llm.Chunk(
                text="x",
                start_at=start + timedelta(hours=index),
                end_at=start + timedelta(hours=index, minutes=30),
                segments=(),
            )
            for index in range(chunks)
        )
        return llm.SessionAnalysis(
            result=model.model_validate(document or ANALYSIS),
            chunks=made_chunks,
            partials=partials,
            elapsed_seconds=12.5,
        )

    monkeypatch.setattr(llm, "analyze_session", fake)
    return calls


def write_fingerprint(transcript: session.SessionTranscript) -> Path:
    """`<slug>.source.json` を書く（§9.4）。**pipeline が書くものと同じ形にする。**"""
    target = paths.analysis_source_path(paths.analysis_path_for(SESSION_KEY))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"schema": 1, "transcript_sha256": session.transcript_fingerprint(transcript)}),
        encoding="utf-8",
    )
    return target


def status_of(database: Database) -> str:
    row = database.get_session(SESSION_KEY)
    assert row is not None
    return row.status


# --- §10.8 セッション統合 ------------------------------------------------


def test_a_ready_session_reaches_analyzed(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_session(database)
    add_part(database)
    stub_analysis(monkeypatch, cfg)
    assert runner.process_session(SESSION_KEY) is SessionOutcome.SAVED
    # **削除が無効なので `SAVED → CLEANUP → COMPLETED` まで進む**（§9.3 / §14.3）。
    # 元音声はデバイスに残ったままである
    assert status_of(database) == SessionStatus.COMPLETED


def test_failed_and_skipped_parts_are_excluded(
    runner: Pipeline,
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FAILED` / `SKIPPED` を統合から除外し、件数を `failed_part_count` に残す（§10.8）。"""
    add_session(database)
    add_part(database, hour=9)
    add_part(database, hour=10, state=PartStatus.FAILED)
    add_part(database, hour=11, state=PartStatus.SKIPPED)
    stub_analysis(monkeypatch, cfg)

    runner.process_session(SESSION_KEY)
    row = database.get_session(SESSION_KEY)
    assert row is not None
    assert row.failed_part_count == 2
    out = logger[1].getvalue()
    assert "session_merged" in out
    assert "parts=1" in out
    assert "excluded=2" in out
    assert "chars=" in out


def test_the_transcript_is_not_persisted(
    runner: Pipeline,
    database: Database,
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
    _data_root: Path,
) -> None:
    """**統合結果は永続化しない**（§10.8）。

    `/data` に増えるのは**解析結果・Map 中間結果・入力の指紋だけ**である
    （それぞれ §10.9 / R-1、§9.4）。**transcript そのものは書かない。**

    指紋は SHA-256 と件数であり、**本文を持たない。**復元できないので「永続化」ではない。
    """
    add_session(database)
    add_part(database)
    stub_analysis(monkeypatch, cfg)
    before = {p for p in _data_root.rglob("*") if p.is_file()}
    runner.process_session(SESSION_KEY)
    added = sorted(p.name for p in {p for p in _data_root.rglob("*") if p.is_file()} - before)
    slug = paths.key_slug(SESSION_KEY)
    assert added == [f"{slug}.json", f"{slug}.source.json", f"{slug}.timeline.json"]


# --- session_empty（§10.8 / §16.4） -------------------------------------


def test_no_valid_parts_completes_without_a_note(
    runner: Pipeline,
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**有効 Part が 0 件なら、ノートを作らずセッションを `COMPLETED`**（§10.8）。

    **`FAILED` にしない。**無音だけの日は異常ではない（§10.6 の `NO_SPEECH_DETECTED` は
    `SKIPPED` であり失敗ではない）。
    """
    add_session(database)
    add_part(database, hour=9, state=PartStatus.SKIPPED)
    calls = stub_analysis(monkeypatch, cfg)

    assert runner.process_session(SESSION_KEY) is SessionOutcome.EMPTY
    assert status_of(database) == SessionStatus.COMPLETED
    assert "session_empty" in logger[1].getvalue()
    assert calls == [], "LLM を呼んではならない"
    row = database.get_session(SESSION_KEY)
    assert row is not None
    assert row.analysis_path is None


def test_a_session_with_no_parts_completes(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_session(database)
    stub_analysis(monkeypatch, cfg)
    assert runner.process_session(SESSION_KEY) is SessionOutcome.EMPTY
    assert status_of(database) == SessionStatus.COMPLETED


def test_parts_without_a_transcript_are_empty(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """transcript が読めない Part だけなら `session_empty`（本文が 0 文字）。"""
    add_session(database)
    add_part(database, text=None)
    stub_analysis(monkeypatch, cfg)
    assert runner.process_session(SESSION_KEY) is SessionOutcome.EMPTY


# --- §10.9 LLM 解析 ------------------------------------------------------


def test_the_analysis_is_written_to_data(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/data/analysis/<slug>.json`（§8.3）。**Vault ではない** — 中間生成物である。"""
    add_session(database)
    add_part(database)
    stub_analysis(monkeypatch, cfg)
    runner.process_session(SESSION_KEY)

    target = paths.analysis_path_for(SESSION_KEY)
    assert target.is_file()
    assert target.name == f"{paths.key_slug(SESSION_KEY)}.json"
    document = json.loads(target.read_text(encoding="utf-8"))
    assert llm.build_schema(cfg).model_validate(document)

    row = database.get_session(SESSION_KEY)
    assert row is not None
    assert row.analysis_path == str(target)
    assert row.title == "開発の一日"


def test_llm_completed_is_logged(
    runner: Pipeline,
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§16.2: `llm_completed session_key=.. chunks=.. elapsed_s=..`。"""
    add_session(database)
    add_part(database)
    stub_analysis(monkeypatch, cfg, chunks=18)
    runner.process_session(SESSION_KEY)
    out = logger[1].getvalue()
    assert "llm_completed" in out
    assert "chunks=18" in out
    assert "elapsed_s=12.5" in out


def test_the_title_is_stored_but_not_used_for_the_filename(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`title` は DB に持つが**ファイル名には使わない**（§12.2 / V-14）。"""
    add_session(database)
    add_part(database)
    stub_analysis(monkeypatch, cfg)
    runner.process_session(SESSION_KEY)
    assert "{title}" not in cfg.obsidian.wiki.filename_template


# --- 冪等性（§9.4） -----------------------------------------------------


def test_a_valid_analysis_is_not_regenerated(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9.4: 実在しスキーマ検証を通り、**かつ指紋が一致すれば**再実行しない。"""
    add_session(database, state=SessionStatus.MERGED)
    add_part(database)
    target = paths.analysis_path_for(SESSION_KEY)
    target.write_text(json.dumps(ANALYSIS, ensure_ascii=False), encoding="utf-8")
    calls = stub_analysis(monkeypatch, cfg)

    transcript = runner.build_transcript(SESSION_KEY)
    assert transcript is not None
    write_fingerprint(transcript)
    assert runner.ensure_analysis(SESSION_KEY, transcript) is True
    assert calls == [], "LLM を再実行している"
    assert status_of(database) == SessionStatus.ANALYZED


def test_an_analysis_without_a_fingerprint_is_regenerated(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**指紋が無ければやり直す**（§9.4）。旧版からの移行と、書き込みに失敗した場合。

    **正しさを費用より優先する** — 余計に 1 回解析するほうが、嘘のノートを残すより良い。
    """
    add_session(database, state=SessionStatus.MERGED)
    add_part(database)
    paths.analysis_path_for(SESSION_KEY).write_text(
        json.dumps(ANALYSIS, ensure_ascii=False), encoding="utf-8"
    )
    calls = stub_analysis(monkeypatch, cfg)

    transcript = runner.build_transcript(SESSION_KEY)
    assert transcript is not None
    assert runner.ensure_analysis(SESSION_KEY, transcript) is True
    assert calls != [], "指紋が無いのに解析を飛ばしている"


def test_an_analysis_of_a_different_transcript_is_regenerated(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**入力が変われば「完了した工程」ではない**（§9.4 / #108）。

    2026-09-14 の実機では、6 回の再オープンすべてで解析が飛ばされ、
    **`parts: 8` と書かれたノートの本文が 2 Part 分のまま**だった。
    """
    add_session(database, state=SessionStatus.MERGED)
    add_part(database)
    paths.analysis_path_for(SESSION_KEY).write_text(
        json.dumps(ANALYSIS, ensure_ascii=False), encoding="utf-8"
    )
    calls = stub_analysis(monkeypatch, cfg)

    transcript = runner.build_transcript(SESSION_KEY)
    assert transcript is not None
    # 別の統合結果を解析したことにする
    write_fingerprint(
        session.SessionTranscript(
            day_date=transcript.day_date,
            segments=[
                session.AbsoluteSegment(
                    at=transcript.segments[0].at,
                    end_at=transcript.segments[0].end_at,
                    text="別の中身",
                )
            ],
            blocks=transcript.blocks,
            excluded_partkeys=[],
        )
    )
    assert runner.ensure_analysis(SESSION_KEY, transcript) is True
    assert calls != [], "入力が変わったのに解析を飛ばしている"


def test_a_broken_analysis_is_regenerated(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**スキーマ検証を通らないものは「無い」と同じ扱い**（§9.4）。"""
    add_session(database, state=SessionStatus.MERGED)
    add_part(database)
    paths.analysis_path_for(SESSION_KEY).write_text('{"title": "だけ"}', encoding="utf-8")
    calls = stub_analysis(monkeypatch, cfg)

    transcript = runner.build_transcript(SESSION_KEY)
    assert transcript is not None
    assert runner.ensure_analysis(SESSION_KEY, transcript) is True
    assert len(calls) == 1


@pytest.mark.parametrize("state", sorted(pipeline.ANALYZED_OR_BEYOND))
def test_an_analyzed_session_is_skipped(
    runner: Pipeline, database: Database, cfg: Config, state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_session(database, state=state)
    add_part(database)
    calls = stub_analysis(monkeypatch, cfg)
    outcome = runner.process_session(SESSION_KEY)
    assert outcome in {SessionOutcome.ANALYZED, SessionOutcome.SAVED}
    assert calls == [], "LLM を再実行している"


# --- 失敗（§10.9 / §15.1）。**最重要** ----------------------------------


@pytest.mark.parametrize(
    "code", [ErrorCode.LLM_UNAVAILABLE, ErrorCode.LLM_INVALID_JSON, ErrorCode.LLM_FAILED]
)
def test_an_llm_failure_keeps_the_transcript(
    runner: Pipeline,
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
    code: ErrorCode,
) -> None:
    """**`LLM_INVALID_JSON` でも文字起こしは失われない**（§10.9 / §12.3）。

    Raw ノートは既に Vault へ保存済みであり、**元音声も削除されない** — §14.1 が
    `analysis_path` の妥当性を要求するため、`FAILED` では `analysis_path` が `NULL` の
    ままになり**自動的に保証される。**
    """
    add_session(database)
    key = add_part(database)
    stub_analysis(monkeypatch, cfg, error=code)

    assert runner.process_session(SESSION_KEY) is SessionOutcome.STOPPED
    assert status_of(database) == SessionStatus.FAILED

    row = database.get_session(SESSION_KEY)
    assert row is not None
    assert row.error_code == code
    assert row.analysis_path is None, "削除の必要条件が満たされてはならない（§14.1）"

    # **文字起こしは残っている**
    assert paths.transcript_path_for(key).is_file()
    part = database.get_recording(key)
    assert part is not None
    assert part.status == PartStatus.RAW_SAVED, "Part を巻き添えにしてはならない"


def test_an_llm_failure_is_logged_as_llm_failed(
    runner: Pipeline,
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`llm_unavailable` / `llm_invalid_json` というイベント名を作らない**（§16.4）。

    v5.0 で `llm_failed` の `error_code=` に統合された。`log.py` は未登録の名前に
    `ValueError` を投げる。
    """
    add_session(database)
    add_part(database)
    stub_analysis(monkeypatch, cfg, error=ErrorCode.LLM_UNAVAILABLE)
    runner.process_session(SESSION_KEY)
    out = logger[1].getvalue()
    assert "llm_failed" in out
    assert "error_code=LLM_UNAVAILABLE" in out
    from voicedock.log import EVENTS

    assert "llm_unavailable" not in EVENTS
    assert "llm_invalid_json" not in EVENTS


def test_a_write_failure_does_not_claim_success(
    runner: Pipeline,
    database: Database,
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
    _data_root: Path,
) -> None:
    """解析結果を書けなければ `FAILED`。**`analysis_path` を残さない。**"""
    add_session(database)
    add_part(database)
    stub_analysis(monkeypatch, cfg)
    monkeypatch.setattr(paths, "ANALYSIS_ROOT", _data_root / "analysis" / "nope.txt")
    (_data_root / "analysis" / "nope.txt").write_text("x", encoding="utf-8")

    assert runner.process_session(SESSION_KEY) is SessionOutcome.STOPPED
    assert status_of(database) == SessionStatus.FAILED
    row = database.get_session(SESSION_KEY)
    assert row is not None
    assert row.analysis_path is None


# --- 状態のガード（§9.3） -----------------------------------------------


def test_a_session_that_is_not_ready_does_not_merge(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`READY` でなければ進めない（`FAILED` の再投入は §15.2 / #32 の責務）。"""
    add_session(database, state=SessionStatus.FAILED)
    add_part(database)
    calls = stub_analysis(monkeypatch, cfg)
    assert runner.process_session(SESSION_KEY) is SessionOutcome.STOPPED
    assert status_of(database) == SessionStatus.FAILED
    assert calls == []


def test_a_missing_session_stops(runner: Pipeline) -> None:
    assert runner.process_session(SessionKey("DJIMIC3:20991231")) is SessionOutcome.STOPPED


def test_every_transition_is_recorded(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§8.4: すべての状態遷移を `events` に残す。"""
    add_session(database)
    add_part(database)
    stub_analysis(monkeypatch, cfg)
    runner.process_session(SESSION_KEY)
    events = database.events_for(EntityType.SESSION, SESSION_KEY)
    assert [(e.from_status, e.to_status) for e in events] == [
        (None, SessionStatus.READY),
        (SessionStatus.READY, SessionStatus.MERGING),
        (SessionStatus.MERGING, SessionStatus.MERGED),
        (SessionStatus.MERGED, SessionStatus.ANALYZING),
        (SessionStatus.ANALYZING, SessionStatus.ANALYZED),
        (SessionStatus.ANALYZED, SessionStatus.WRITING),
        (SessionStatus.WRITING, SessionStatus.SAVED),
        # `delete_source_audio: false` の経路（§9.3 の `SAVED` 行）
        (SessionStatus.SAVED, SessionStatus.CLEANUP),
        (SessionStatus.CLEANUP, SessionStatus.COMPLETED),
    ]


# --- Timeline の素材（§13.4 / §10.9 の R-1） ----------------------------


def test_the_saved_timeline_is_preferred(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**保存済みの Map 中間結果を使う**（§10.9 / §13.4）。

    使わないと Timeline が代替経路（`summary` の各文）へ落ち、**どのブロックにも
    同じ要約が並ぶ**（実際に Daily ノートを生成して発覚した）。
    """
    from voicedock import daily

    add_session(database)
    add_part(database)
    stub_analysis(monkeypatch, cfg)
    runner.process_session(SESSION_KEY)

    note = next(Path(cfg.obsidian.root).rglob("*Voice.md")).read_text(encoding="utf-8")
    transcript = runner.build_transcript(SESSION_KEY)
    assert transcript is not None
    saved = daily.load_timeline(
        paths.analysis_path_for(SESSION_KEY),
        fingerprint=session.transcript_fingerprint(transcript),
    )
    assert saved, "Map 中間結果が保存されていない"
    assert saved[0].lines == ("Map 由来の点 0",)
    for block in saved:
        for line in block.lines:
            assert line in note, "保存済みの Map 中間結果を使っていない"


def test_a_missing_timeline_falls_back(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**読めなければ §13.4 の代替経路へ落ちる。失敗させない。**

    Timeline は要約の付随物であり、§1.3 の優先順位を上げる理由が無い。
    """
    from voicedock import daily

    add_session(database)
    add_part(database)
    stub_analysis(monkeypatch, cfg)

    original = daily.load_timeline
    monkeypatch.setattr(daily, "load_timeline", lambda _path, **_kw: [])
    assert runner.process_session(SESSION_KEY) is SessionOutcome.SAVED
    assert original is not daily.load_timeline

    note = next(Path(cfg.obsidian.root).rglob("*Voice.md")).read_text(encoding="utf-8")
    assert "## Timeline" in note
    assert "Map 由来の点" not in note, "代替経路へ落ちていない"


# --- §10.8 絶対時刻で統合されていること ---------------------------------


def test_the_transcript_uses_absolute_time(runner: Pipeline, database: Database) -> None:
    """§10.8: 各 Part の `started_at` を起点とした絶対時刻。

    **`offset += duration` 方式だと Part 間のギャップが詰まる**（#17 の対比テストと同じ性質を
    pipeline 経由でも確かめる）。
    """
    add_session(database)
    add_part(database, hour=9)
    add_part(database, hour=15)
    transcript = runner.build_transcript(SESSION_KEY)
    assert transcript is not None
    assert [segment.at.hour for segment in transcript.segments] == [9, 15]


# --- 指紋（§9.4 / #108） ------------------------------------------------


def a_transcript(*, text: str = "おはよう", blocks: int | None = None) -> session.SessionTranscript:
    start = datetime(2026, 9, 12, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    end = start + timedelta(minutes=30)
    return session.SessionTranscript(
        day_date=start.date(),
        segments=[session.AbsoluteSegment(at=start, end_at=end, text=text)],
        blocks=[(start, end)] * (blocks if blocks is not None else 1),
        excluded_partkeys=[],
    )


def test_the_fingerprint_is_stable() -> None:
    assert session.transcript_fingerprint(a_transcript()) == session.transcript_fingerprint(
        a_transcript()
    )


def test_the_fingerprint_changes_with_the_text() -> None:
    assert session.transcript_fingerprint(a_transcript()) != session.transcript_fingerprint(
        a_transcript(text="こんばんは")
    )


def test_the_fingerprint_changes_with_the_blocks() -> None:
    assert session.transcript_fingerprint(a_transcript()) != session.transcript_fingerprint(
        a_transcript(blocks=2)
    )


def test_the_fingerprint_ignores_excluded_parts() -> None:
    """**除外された Part は解析の入力に現れない。**その増減でやり直す理由が無い。"""
    base = a_transcript()
    with_excluded = session.SessionTranscript(
        day_date=base.day_date,
        segments=base.segments,
        blocks=base.blocks,
        excluded_partkeys=[PartKey("DJIMIC3/x/y_orig.wav")],
    )
    assert session.transcript_fingerprint(base) == session.transcript_fingerprint(with_excluded)


def test_a_segment_added_changes_the_fingerprint() -> None:
    """**これが 2026-09-14 に起きたことである** — Part が増えたのに解析が飛ばされた。"""
    base = a_transcript()
    later = datetime(2026, 9, 12, 15, 9, tzinfo=ZoneInfo("Asia/Tokyo"))
    grown = session.SessionTranscript(
        day_date=base.day_date,
        segments=[*base.segments, session.AbsoluteSegment(at=later, end_at=later, text="追加")],
        blocks=base.blocks,
        excluded_partkeys=[],
    )
    assert session.transcript_fingerprint(base) != session.transcript_fingerprint(grown)


def test_a_failed_analysis_write_leaves_no_fingerprint(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**指紋は解析本体を書いた後に置く**（§9.4）。

    順序を逆にすると、**解析の書き込みに失敗したのに指紋だけが残り、古い解析が
    「最新」と判定される。**次回に必ずやり直せることを固定する。
    """
    add_session(database, state=SessionStatus.MERGED)
    add_part(database)
    stub_analysis(monkeypatch, cfg)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(pipeline.Pipeline, "_write_analysis", boom)
    transcript = runner.build_transcript(SESSION_KEY)
    assert transcript is not None
    assert runner.ensure_analysis(SESSION_KEY, transcript) is False

    fingerprint = paths.analysis_source_path(paths.analysis_path_for(SESSION_KEY))
    assert not fingerprint.exists(), "解析を書けていないのに指紋が残っている"


# --- 再オープンの通し（#108 の再現） ------------------------------------


def test_a_reopened_session_is_analysed_again(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Part が増えたら解析をやり直す**（§9.4 / §9.2）。

    2026-09-14 の実機では 6 回の再オープンすべてで解析が飛ばされ、
    **`parts: 8` / `blocks: 2` と書かれたノートの本文が 2 Part・56 分ぶんのまま**だった。
    2 時間 51 分が Daily ノートから欠けた（#108）。
    """
    add_session(database)
    add_part(database, hour=9)
    calls = stub_analysis(monkeypatch, cfg)
    assert runner.process_session(SESSION_KEY) is SessionOutcome.SAVED
    assert len(calls) == 1

    # 同じ日の新しい Part が RAW_SAVED に到達 → §9.2 の再オープン
    add_part(database, hour=15, text="午後の打ち合わせ。")
    assert runner.reopen_session(SESSION_KEY) is True
    assert runner.process_session(SESSION_KEY) is SessionOutcome.SAVED

    assert len(calls) == 2, "再オープンしたのに解析を飛ばしている"
    # **2 回目の入力には新しい Part が含まれる**
    second = calls[1]
    assert any("午後の打ち合わせ" in seg.text for seg in second.segments)  # type: ignore[attr-defined]


def test_processing_twice_without_new_parts_does_not_reanalyse(
    runner: Pipeline, database: Database, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**§9.4 は生きている。**入力が同じなら解析をやり直さない（費用の歯止め）。"""
    add_session(database)
    add_part(database, hour=9)
    calls = stub_analysis(monkeypatch, cfg)
    assert runner.process_session(SESSION_KEY) is SessionOutcome.SAVED
    assert runner.reopen_session(SESSION_KEY) is True
    runner.process_session(SESSION_KEY)
    assert len(calls) == 1, "入力が同じなのに解析をやり直している"
