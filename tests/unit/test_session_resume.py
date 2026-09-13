"""`FAILED` から戻したセッションに受け手がいることを固定する（SPEC §9.3 / §9.4 / §15.2）。

**2026-09-14 に実機で踏んだ**（#97）。LLM へ到達できず `LLM_UNAVAILABLE` になった
セッションが `ANALYZING` に座ったまま二度と動かなかった。

`resume_failed()` は `FAILED` を**落ちた工程へ**戻す（§15.2）。戻り先は
`SESSION_RETRYABLE_FROM_FAILED` の 3 つだが、**`worker.ready_session_keys()` が
走査していたのは `{READY, MERGING}` だけだった。**`ANALYZING` / `WRITING` に戻した行は
誰にも拾われない。

**再起動しても直らなかった。**§9.4 の巻き戻しは `ANALYZING → MERGED` へ移すだけで、
`MERGED` もまた走査対象外だったからである。実機では poll 5 秒 × 18 周を経ても
`MERGED` のまま動かなかった。

だからここでは**個別の経路ではなく不変条件を固定する** — 「戻りうる状態すべてに
受け手がいること」。工程を 1 つ足して配線を忘れたら落ちる。
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from voicedock import paths, pipeline, session
from voicedock.config import Config
from voicedock.db import Database, EntityType, Recording, Session
from voicedock.log import Logger
from voicedock.paths import DevicePath, SessionKey
from voicedock.states import (
    SESSION_INITIAL,
    SESSION_RECOVERY,
    SESSION_RETRYABLE_FROM_FAILED,
    PartStatus,
    SessionStatus,
)
from voicedock.worker import Worker

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


def add_session(database: Database, *, status: str) -> None:
    database.insert_session(
        Session(
            session_key=KEY,
            day_date="2026-09-12",
            device_id=DEVICE,
            status=SESSION_INITIAL,
            updated_at=NOW.isoformat(timespec="seconds"),
        )
    )
    database.conn.execute("UPDATE sessions SET status = ? WHERE session_key = ?", (status, KEY))
    database.conn.commit()


def add_terminal_part(database: Database) -> None:
    """`ready_session_keys()` のガード「全 Part が終端状態」を満たす Part を 1 件。"""
    started = datetime(2026, 9, 12, 9, 0, 0, tzinfo=JST)
    folder = "TX_MIC001_20260912_090000"
    relpath = f"{folder}/TX00_MIC001_20260912_090000_orig.wav"
    database.insert_recording(
        Recording(
            partkey=paths.partkey_for(DEVICE, DevicePath(PurePosixPath(relpath))),
            device_id=DEVICE,
            source_folder=folder,
            transmitter_id="TX00",
            mic_index=1,
            started_at=started.isoformat(timespec="seconds"),
            status=PartStatus.DISCOVERED,
            updated_at=started.isoformat(timespec="seconds"),
            duration_seconds=60.0,
            ended_at=(started + timedelta(seconds=60)).isoformat(timespec="seconds"),
            source_path=relpath,
        )
    )
    database.conn.execute(
        "UPDATE recordings SET status = ?, session_key = ?", (PartStatus.RAW_SAVED, KEY)
    )
    database.conn.commit()


def empty_transcript() -> session.SessionTranscript:
    return session.SessionTranscript(
        day_date=date(2026, 9, 12), segments=[], blocks=[], excluded_partkeys=[]
    )


def runner(database: Database, cfg: Config, log: Logger) -> pipeline.Pipeline:
    return pipeline.Pipeline(database=database, cfg=cfg, log=log, now=NOW)


def transitions(database: Database) -> list[tuple[str, str]]:
    return [
        (row[0], row[1])
        for row in database.conn.execute(
            "SELECT from_status, to_status FROM events "
            "WHERE entity_type = ? AND entity_key = ? ORDER BY id",
            (EntityType.SESSION.value, KEY),
        )
    ]


# --- 不変条件（**これが本体**） ------------------------------------------


def test_processable_is_exactly_the_union_of_the_stage_guards() -> None:
    """**工程の入口条件の和が走査対象である。**

    片方だけ足すと「進める工程はあるのに誰も呼ばない」か、その逆になる。
    """
    assert pipeline.PROCESSABLE == (pipeline.MERGEABLE | pipeline.ANALYZABLE | pipeline.WRITABLE)


@pytest.mark.parametrize("status", sorted(SESSION_RETRYABLE_FROM_FAILED))
def test_every_state_resume_can_produce_has_a_handler(status: SessionStatus) -> None:
    """**`resume_failed()` の戻り先すべてに受け手がいること**（§15.2）。

    `SESSION_RETRYABLE_FROM_FAILED` に 1 つ足して配線を忘れたら、ここで落ちる。
    **実機ではこれが `ANALYZING` と `WRITING` の 2 件について破れていた。**
    """
    assert status in pipeline.PROCESSABLE


@pytest.mark.parametrize(
    "status",
    sorted(
        SESSION_RECOVERY[key]
        for key in (SessionStatus.MERGING, SessionStatus.ANALYZING, SessionStatus.WRITING)
    ),
)
def test_every_recovery_target_that_produces_a_note_has_a_handler(status: SessionStatus) -> None:
    """**§9.4 の巻き戻し先にも受け手がいること。**

    巻き戻しても走査されないなら、**再起動は何も直していない。**実機では
    `ANALYZING → MERGED` へ戻したあと `MERGED` で止まっていた。

    削除まわりの巻き戻し先（`SOURCE_DELETE_PENDING` / `SAVED`）は別の経路が拾うので
    ここでは見ない。
    """
    assert status in pipeline.PROCESSABLE


# --- 走査（`worker.ready_session_keys()`） -------------------------------


@pytest.mark.parametrize("status", sorted(pipeline.PROCESSABLE))
def test_the_worker_scans_every_processable_state(
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    state_root: Path,
    status: str,
) -> None:
    """**`PROCESSABLE` の全状態が実際に選ばれること。**定数だけ直しても走査が
    追随していなければ意味が無い。
    """
    add_session(database, status=status)
    add_terminal_part(database)
    worker = Worker(
        cfg=cfg,
        log=logger[0],
        database=database,
        state_root=state_root,
        sleep=lambda _seconds: None,
        clock=lambda: NOW,
    )
    assert worker.ready_session_keys() == [KEY], status


@pytest.mark.parametrize(
    "status",
    [
        SessionStatus.OPEN,
        SessionStatus.SAVED,
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
    ],
)
def test_the_worker_ignores_states_outside_processable(
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    state_root: Path,
    status: str,
) -> None:
    """陰性対照。**何でも拾う実装では検査にならない。**

    `OPEN` はまだ Part を受け付けている（`close_idle_sessions()` が `READY` へ送る）。
    `SAVED` / `COMPLETED` は確定済みで、再オープンは別の契機で起きる（§9.3）。
    `FAILED` は `requeue_failed()` の担当である（§15.2）。
    """
    add_session(database, status=status)
    add_terminal_part(database)
    worker = Worker(
        cfg=cfg,
        log=logger[0],
        database=database,
        state_root=state_root,
        sleep=lambda _seconds: None,
        clock=lambda: NOW,
    )
    assert worker.ready_session_keys() == [], status


# --- 工程の入口（戻ってきた行を受けること） ------------------------------


def test_analysis_resumes_from_analyzing(
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ANALYZING` に戻ってきた行を `ensure_analysis()` が受けること。

    **修正前はここで `False` を返して終わり**、セッションは永久に `ANALYZING` に座った。
    """
    add_session(database, status=SessionStatus.ANALYZING)
    monkeypatch.setattr(pipeline.Pipeline, "_analysis_is_valid", lambda *_args: True)

    assert runner(database, cfg, logger[0]).ensure_analysis(KEY, empty_transcript())

    row = database.get_session(KEY)
    assert row is not None
    assert row.status == SessionStatus.ANALYZED


def test_resuming_does_not_record_a_phantom_transition(
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**起きていない遷移を `events` に書かないこと。**

    `ANALYZING` から再開した行に `MERGED → ANALYZING` を書くと、障害を追う人が
    「もう一度 `MERGED` を通った」と読む。§8.4 の `events` は状態遷移の唯一の履歴である。
    """
    add_session(database, status=SessionStatus.ANALYZING)
    monkeypatch.setattr(pipeline.Pipeline, "_analysis_is_valid", lambda *_args: True)
    runner(database, cfg, logger[0]).ensure_analysis(KEY, empty_transcript())

    recorded = transitions(database)
    assert (SessionStatus.MERGED, SessionStatus.ANALYZING) not in recorded, recorded
    assert (SessionStatus.ANALYZING, SessionStatus.ANALYZED) in recorded, recorded


def test_analysis_still_starts_from_merged(
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """陰性対照。**通常の経路が壊れていないこと。**"""
    add_session(database, status=SessionStatus.MERGED)
    monkeypatch.setattr(pipeline.Pipeline, "_analysis_is_valid", lambda *_args: True)
    assert runner(database, cfg, logger[0]).ensure_analysis(KEY, empty_transcript())

    row = database.get_session(KEY)
    assert row is not None
    assert row.status == SessionStatus.ANALYZED
    assert (SessionStatus.MERGED, SessionStatus.ANALYZED) in transitions(database)


def test_the_daily_note_resumes_from_writing(
    database: Database,
    cfg: Config,
    logger: tuple[Logger, io.StringIO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`WRITING` に戻ってきた行を `ensure_daily_note()` が受けること。

    解析結果を読めない状態を作り、**ガードを抜けて `_fail_session()` まで届く**ことを見る。
    **修正前は `False` を返すだけで `events` に何も残らなかった**（＝誰も進めないまま沈黙）。
    """
    add_session(database, status=SessionStatus.WRITING)
    database.update_session(KEY, analysis_path="/data/analysis/x.json", now=NOW)
    monkeypatch.setattr(pipeline.Pipeline, "_load_analysis", lambda *_args: None)

    assert not runner(database, cfg, logger[0]).ensure_daily_note(KEY, empty_transcript())

    row = database.get_session(KEY)
    assert row is not None
    assert row.status == SessionStatus.FAILED
    assert (SessionStatus.WRITING, SessionStatus.FAILED) in transitions(database)
