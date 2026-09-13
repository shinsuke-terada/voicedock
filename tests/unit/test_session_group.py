"""`voicedock.session` の分組と `OPEN` を閉じる条件を固定する（SPEC §10.4 / §9.3 Session 表）。

**「1 デバイス・1 日 = 1 セッション」が Daily ノートの単位である。**分組を間違えると、
終日録音が 24 時間 1 本のノートになるか（v3.0 の既知の失敗）、1 日が複数のノートに
割れて振り返りに使えなくなる。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.spec_sync import spec_section_code
from voicedock import paths, session
from voicedock.config import Config
from voicedock.db import Database, EntityType, Recording
from voicedock.states import PartStatus, SessionStatus

JST = ZoneInfo("Asia/Tokyo")
DEVICE = "DJIMIC3"


def part(
    database: Database,
    *,
    folder: str = "TX_MIC001_20260912_120950",
    name: str = "TX00_MIC001_20260912_120950_orig.wav",
    started: str = "2026-09-12T12:09:50+09:00",
    duration: float | None = 1800.0,
    device_id: str = DEVICE,
    status: str = PartStatus.DISCOVERED,
    tx: str = "TX00",
    mic: int = 1,
) -> Recording:
    """`recordings` へ 1 行入れて返す。**`partkey` は `paths.partkey_for()` で作る。**"""
    from pathlib import PurePosixPath

    from voicedock.paths import DevicePath

    relpath = f"{folder}/{name}" if folder else name
    started_at = datetime.fromisoformat(started)
    row = Recording(
        partkey=paths.partkey_for(device_id, DevicePath(PurePosixPath(relpath))),
        device_id=device_id,
        source_folder=folder,
        transmitter_id=tx,
        mic_index=mic,
        started_at=started_at.isoformat(timespec="seconds"),
        status=status,
        updated_at=started_at.isoformat(timespec="seconds"),
        duration_seconds=duration,
        ended_at=(
            (started_at + timedelta(seconds=duration)).isoformat(timespec="seconds")
            if duration is not None
            else None
        ),
        source_path=relpath,
    )
    database.insert_recording(row)
    return row


@pytest.fixture
def cfg(make_config: Callable[..., Config]) -> Config:
    return make_config({"timezone": "Asia/Tokyo"})


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 12, 23, 0, 0, tzinfo=JST)


# --- 鍵（§8.1 / §10.4） -------------------------------------------------


def test_the_key_matches_the_spec_formula() -> None:
    """§10.4 のコードブロックと同じ形であること。

    **SPEC の式を変えたらここが落ちる。**`session_key` の算出規則を変えることは
    §8.5 の唯一の禁則である。
    """
    block = spec_section_code("10.4", "python")
    assert 'f"{part.device_id}:{day}"' in block
    assert "%Y%m%d" in block
    assert "astimezone(cfg.tz)" in block


def test_session_key_is_pinned() -> None:
    """固定入力に対する結果をバイト単位で固定する（`partkey` と同じ扱い）。

    **このテストが落ちたら、期待値を書き換えて通すのではなく、変更をやめること。**
    §14.1 の削除条件はノートに載った鍵を見る。規則を変えると、それ以前に保存した
    録音が永久に削除対象にならない。
    """
    key = paths.session_key_for(DEVICE, datetime(2026, 8, 29, 7, 12, 4, tzinfo=JST), tz=JST)
    assert key == "DJIMIC3:20260829"


def test_the_day_comes_from_the_configured_timezone() -> None:
    """**日付は `tz` に変換してから取る。**

    DB から読み直した値や UTC の値が渡ることがある。変換を省くと
    **深夜の Part が前日のセッションへ入る。**
    """
    utc_midnight = datetime(2026, 8, 28, 23, 30, tzinfo=ZoneInfo("UTC"))  # JST では 8/29 08:30
    assert paths.session_key_for(DEVICE, utc_midnight, tz=JST) == "DJIMIC3:20260829"


def test_the_key_round_trips() -> None:
    key = paths.session_key_for("NO NAME", datetime(2026, 8, 29, 7, 12, tzinfo=JST), tz=JST)
    assert paths.device_id_of_session(key) == "NO NAME"
    assert paths.day_of_session(key).isoformat() == "2026-08-29"


@pytest.mark.parametrize("device_id", ["", "a:b", "a/b", ".hidden"])
def test_the_key_rejects_bad_device_ids(device_id: str) -> None:
    """**壊れた鍵を組み立てる前に弾く。**ノートに載ってしまうと直す手段が無い。"""
    with pytest.raises(ValueError, match="device_id"):
        paths.session_key_for(device_id, datetime(2026, 8, 29, tzinfo=JST), tz=JST)


# --- 分組（§10.4） -------------------------------------------------------


def test_the_same_day_is_one_session(database: Database, cfg: Config, now: datetime) -> None:
    """**送信機が変わっても、フォルダが変わっても、同じ日なら同じセッション**（§10.4）。

    DJI は 50 ファイルごとにフォルダを変える（§5.1）。
    """
    part(database, folder="TX_MIC001_20260912_120950")
    part(
        database,
        folder="TX_MIC009_20260912_160000",
        name="TX03_MIC009_20260912_160000_orig.wav",
        started="2026-09-12T16:00:00+09:00",
        tx="TX03",
        mic=9,
    )
    result = session.group_parts(database, cfg=cfg, now=now)

    assert len(result.created) == 1
    assert {key for _partkey, key in result.assigned} == {"DJIMIC3:20260912"}
    assert database.counts().sessions == 1
    row = database.get_session("DJIMIC3:20260912")
    assert row is not None
    assert row.part_count == 2
    assert row.status == SessionStatus.OPEN


def test_a_part_spanning_midnight_belongs_to_its_start_day(
    database: Database, cfg: Config, now: datetime
) -> None:
    """**23:50 開始の 30 分 Part は開始日に属する**（§10.4）。`ended_at` は見ない。"""
    part(
        database,
        folder="TX_MIC001_20260912_235000",
        name="TX00_MIC001_20260912_235000_orig.wav",
        started="2026-09-12T23:50:00+09:00",
        duration=1800.0,
    )
    result = session.group_parts(database, cfg=cfg, now=now)
    assert [key for _partkey, key in result.assigned] == ["DJIMIC3:20260912"]


def test_a_different_day_is_a_different_session(
    database: Database, cfg: Config, now: datetime
) -> None:
    part(database)
    part(
        database,
        folder="TX_MIC001_20260913_090000",
        name="TX00_MIC001_20260913_090000_orig.wav",
        started="2026-09-13T09:00:00+09:00",
    )
    session.group_parts(database, cfg=cfg, now=now)
    assert database.counts().sessions == 2


def test_a_different_device_is_a_different_session(
    database: Database, cfg: Config, now: datetime
) -> None:
    part(database)
    part(database, device_id="NO NAME")
    session.group_parts(database, cfg=cfg, now=now)
    assert {row.session_key for row in database.sessions_with_status(SessionStatus.OPEN)} == {
        "DJIMIC3:20260912",
        "NO NAME:20260912",
    }


def test_only_ungrouped_parts_are_considered(
    database: Database, cfg: Config, now: datetime
) -> None:
    """**対象は `session_key IS NULL` の Part のみ**（§10.4）。

    再接続のたびに全件を引き直すと、**処理済みの Part が別のセッションへ移りうる。**
    """
    part(database)
    session.group_parts(database, cfg=cfg, now=now)
    second = session.group_parts(database, cfg=cfg, now=now)
    assert second.assigned == ()
    assert second.created == ()


def test_the_columns_are_recounted_from_the_parts(
    database: Database, cfg: Config, now: datetime
) -> None:
    """`part_count` / `started_at` / `ended_at` / `recorded_seconds` / `failed_part_count`。

    **毎回すべて数え直す。**加算で持つと 1 回の取りこぼしが恒久的なずれになる。
    """
    part(database, duration=600.0)
    part(
        database,
        folder="TX_MIC001_20260912_160000",
        name="TX00_MIC001_20260912_160000_orig.wav",
        started="2026-09-12T16:00:00+09:00",
        duration=1200.0,
        status=PartStatus.SKIPPED,
    )
    session.group_parts(database, cfg=cfg, now=now)

    row = database.get_session("DJIMIC3:20260912")
    assert row is not None
    assert row.part_count == 2
    assert row.failed_part_count == 1, "SKIPPED は統合から除外される（§10.8）"
    assert row.recorded_seconds == pytest.approx(1800.0)
    assert row.started_at is not None and row.started_at.startswith("2026-09-12T12:09:50")
    assert row.ended_at is not None and row.ended_at.startswith("2026-09-12T16:20:00")


def test_the_day_date_comes_from_the_key(database: Database, cfg: Config, now: datetime) -> None:
    """`day_date` は**鍵から導く。**Part から取り直すと `tz` の扱いが 2 箇所になる。"""
    part(database)
    session.group_parts(database, cfg=cfg, now=now)
    row = database.get_session("DJIMIC3:20260912")
    assert row is not None
    assert row.day_date == "2026-09-12"


# --- events（§9.3 の注記） ----------------------------------------------


def test_adding_a_part_to_an_open_session_is_recorded(
    database: Database, cfg: Config, now: datetime
) -> None:
    """**`OPEN → OPEN`（Part 追加）は遷移である**（§9.3 の注記）。

    `READY のまま` / `SAVED のまま` は遷移ではないので `events` を書かないが、
    こちらは表が「のまま」と書いていない。
    """
    part(database)
    part(
        database,
        folder="TX_MIC001_20260912_160000",
        name="TX00_MIC001_20260912_160000_orig.wav",
        started="2026-09-12T16:00:00+09:00",
    )
    session.group_parts(database, cfg=cfg, now=now)

    events = database.events_for(EntityType.SESSION, "DJIMIC3:20260912")
    # 行の作成（from_status=None）+ Part 追加 2 件
    assert [(e.from_status, e.to_status) for e in events] == [
        (None, SessionStatus.OPEN),
        (SessionStatus.OPEN, SessionStatus.OPEN),
        (SessionStatus.OPEN, SessionStatus.OPEN),
    ]


def test_adding_to_a_closed_session_writes_no_event(
    database: Database, cfg: Config, now: datetime
) -> None:
    """**`OPEN` でないセッションへの追加は `events` を書かない**（§9.3）。

    §9.3 の再オープンは「同日の新 Part が **`RAW_SAVED`** になったとき」に起きる（#32）。
    分組は `DISCOVERED` の時点で走るので、**ここで再オープンを判断してはならない。**
    """
    part(database)
    session.group_parts(database, cfg=cfg, now=now)
    database.record_transition(
        EntityType.SESSION,
        "DJIMIC3:20260912",
        from_status=SessionStatus.OPEN,
        to_status=SessionStatus.READY,
    )
    before = len(database.events_for(EntityType.SESSION, "DJIMIC3:20260912"))

    part(
        database,
        folder="TX_MIC001_20260912_160000",
        name="TX00_MIC001_20260912_160000_orig.wav",
        started="2026-09-12T16:00:00+09:00",
    )
    result = session.group_parts(database, cfg=cfg, now=now)

    assert len(result.assigned) == 1, "session_key は割り当てる"
    after = database.events_for(EntityType.SESSION, "DJIMIC3:20260912")
    assert len(after) == before, "events は増えない"
    row = database.get_session("DJIMIC3:20260912")
    assert row is not None
    assert row.status == SessionStatus.READY, "再オープンしない（#32 の責務）"
    assert row.part_count == 2, "列は更新する"


# --- `OPEN` を閉じる（§10.4 の 3 条件） ---------------------------------


def test_a_past_day_open_session_becomes_ready(
    database: Database, cfg: Config, now: datetime
) -> None:
    """条件 1 / 3: 当該セッションの日付が「今日」でない。

    §10.4 の条件 3（起動時のリカバリ）は**呼ぶ場面**の指定であって別の規則ではない。
    """
    part(database)
    session.group_parts(database, cfg=cfg, now=now)
    tomorrow = now + timedelta(days=1)

    closed = session.close_open_sessions(database, cfg=cfg, now=tomorrow)
    assert closed == ("DJIMIC3:20260912",)
    row = database.get_session("DJIMIC3:20260912")
    assert row is not None
    assert row.status == SessionStatus.READY


def test_an_idle_session_becomes_ready_on_the_same_day(
    database: Database, cfg: Config, now: datetime
) -> None:
    """条件 2: 当日であっても、最後の Part 追加から `idle_close_seconds` が経過した。"""
    part(database)
    session.group_parts(database, cfg=cfg, now=now)
    later = now + timedelta(seconds=cfg.session.idle_close_seconds + 1)

    assert session.close_open_sessions(database, cfg=cfg, now=later) == ("DJIMIC3:20260912",)


def test_a_recent_session_on_the_same_day_stays_open(
    database: Database, cfg: Config, now: datetime
) -> None:
    part(database)
    session.group_parts(database, cfg=cfg, now=now)
    soon = now + timedelta(seconds=cfg.session.idle_close_seconds - 1)

    assert session.close_open_sessions(database, cfg=cfg, now=soon) == ()
    row = database.get_session("DJIMIC3:20260912")
    assert row is not None
    assert row.status == SessionStatus.OPEN


def test_closing_is_idempotent(database: Database, cfg: Config, now: datetime) -> None:
    """2 回目は何も起きない（`READY` は `OPEN` ではない）。"""
    part(database)
    session.group_parts(database, cfg=cfg, now=now)
    tomorrow = now + timedelta(days=1)
    session.close_open_sessions(database, cfg=cfg, now=tomorrow)
    assert session.close_open_sessions(database, cfg=cfg, now=tomorrow) == ()


def test_closing_records_the_reason(database: Database, cfg: Config, now: datetime) -> None:
    part(database)
    session.group_parts(database, cfg=cfg, now=now)
    session.close_open_sessions(database, cfg=cfg, now=now + timedelta(days=1))

    events = database.events_for(EntityType.SESSION, "DJIMIC3:20260912")
    assert events[-1].to_status == SessionStatus.READY
    assert events[-1].detail == "stale_day"
