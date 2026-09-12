"""`voicedock.db` が SPEC §8 と一致することを固定する。

スキーマは **SPEC の SQL をメモリ DB へ流したもの**と、マイグレーションで作ったものを
`PRAGMA` レベルで突き合わせる。**書式の違いに影響されない**照合であり、SPEC に列を
足して写し忘れる事故が落ちる。
"""

from __future__ import annotations

import ast
import re
import sqlite3
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import REPO_ROOT
from tests.spec_sync import spec_retry_reset_statuses, spec_schema_sql
from voicedock import db
from voicedock.db import (
    FAILED_STATUS,
    RETRY_RESET_STATUSES,
    SCHEMA_VERSION,
    Database,
    EntityType,
    Event,
    Recording,
    Session,
    TransitionConflict,
    connect,
    structure,
    truncate,
)
from voicedock.log import MAX_VALUE_CHARS

NOW = datetime(2026, 9, 13, 9, 0, 0)


def make_recording(**overrides: Any) -> Recording:
    base: dict[str, Any] = {
        "device_id": "DJIMIC3",
        "source_folder": "TX_MIC001_20260912_120950",
        "transmitter_id": "TX00",
        "mic_index": 1,
        "started_at": "2026-09-12T12:09:50+09:00",
        "status": "DISCOVERED",
        "updated_at": "2026-09-13T09:00:00+09:00",
    }
    return Recording(**(base | overrides))


def make_session(**overrides: Any) -> Session:
    base: dict[str, Any] = {
        "session_key": "DJIMIC3:20260912",
        "day_date": "2026-09-12",
        "device_id": "DJIMIC3",
        "status": "OPEN",
        "updated_at": "2026-09-13T09:00:00+09:00",
    }
    return Session(**(base | overrides))


# --- スキーマ ------------------------------------------------------------


def test_schema_matches_spec(database: Database) -> None:
    """SPEC §8.2〜§8.5 の DDL と、マイグレーションで作った構造が一致すること。"""
    expected_conn = sqlite3.connect(":memory:")
    db.apply_sql(expected_conn, spec_schema_sql())
    assert structure(database.conn) == structure(expected_conn)


def test_schema_has_the_spec_tables(database: Database) -> None:
    tables = {
        key.removeprefix("table:") for key in structure(database.conn) if key.startswith("table:")
    }
    assert tables == {"recordings", "sessions", "events", "schema_version"}


def test_recordings_has_30_columns(database: Database) -> None:
    """v1 完全形であることの記録（§8.5 は破壊的変更を禁じている）。

    **後から足せない列を今入れた**ことを固定する。列を増やすときは `ALTER TABLE ADD COLUMN`
    しか使えない。
    """
    columns = structure(database.conn)["table:recordings"]
    assert isinstance(columns, list)
    assert len(columns) == 30


@pytest.mark.parametrize(
    ("row_type", "table"),
    [(Recording, "recordings"), (Session, "sessions"), (Event, "events")],
)
def test_row_dataclasses_match_columns(database: Database, row_type: type, table: str) -> None:
    """行 dataclass のフィールドが列と 1 対 1 であること。

    SPEC に列を足して dataclass を忘れると落ちる。
    """
    columns = {row[1] for row in database.conn.execute(f"PRAGMA table_info({table})")}
    assert {field.name for field in fields(row_type)} == columns


def test_indexes_exist(database: Database) -> None:
    names = {
        key.removeprefix("index:")
        for key in structure(database.conn)
        if key.startswith("index:") and not key.startswith("index:sqlite_autoindex")
    }
    assert names == {
        "idx_recordings_partkey",
        "idx_recordings_sha_denoised",
        "idx_recordings_sha_orig",
        "idx_recordings_status",
        "idx_recordings_session",
        "idx_recordings_started",
        "idx_sessions_status",
        "idx_sessions_day",
        "idx_events_entity",
    }


# --- PRAGMA（§8.1） -----------------------------------------------------


def test_pragmas(database: Database) -> None:
    assert database.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert database.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert database.conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert database.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000


def test_busy_timeout_is_configurable(db_path: Path) -> None:
    with connect(db_path, busy_timeout_ms=4321) as opened:
        assert opened.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321


def test_wal_files_appear(database: Database, db_path: Path) -> None:
    database.insert_recording(make_recording())
    assert db_path.with_name(f"{db_path.name}-wal").exists()


class _StubCursor:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...]:
        return self._row


class _StubConnection:
    """`journal_mode` の設定に失敗する接続。

    `sqlite3.Connection` は C 型なので差し替えられない。**`sqlite3` にも触らない** —
    `db.sqlite3.connect` を monkeypatch しているので、ここで呼ぶと自分に戻って再帰する。
    """

    def __init__(self) -> None:
        self.row_factory: Any = None
        self.closed = False

    def execute(self, sql: str, *_args: Any) -> _StubCursor:
        return _StubCursor(("delete",) if "journal_mode" in sql else (1,))

    def close(self) -> None:
        self.closed = True


def test_journal_mode_failure_raises(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WAL にできなければ `RuntimeError`。

    黙って `delete` のままになると §8.1 の耐久性の前提（電源断で状態を失わない）が崩れる。
    """
    stub = _StubConnection()
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: stub)
    with pytest.raises(RuntimeError, match="journal_mode を WAL にできません"):
        connect(db_path)
    assert stub.closed, "失敗時に接続を閉じること"


# --- マイグレーション（§8.5） -------------------------------------------


def test_schema_version_is_recorded(database: Database) -> None:
    assert database.schema_version() == SCHEMA_VERSION
    rows = database.conn.execute("SELECT version, applied_at FROM schema_version").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == SCHEMA_VERSION


def test_migration_is_idempotent(db_path: Path) -> None:
    """2 回 `connect()` しても `schema_version` が 1 行のまま（`restart` 相当）。"""
    with connect(db_path) as first:
        assert first.schema_version() == SCHEMA_VERSION
    with connect(db_path) as second:
        rows = second.conn.execute("SELECT version FROM schema_version").fetchall()
    assert [row[0] for row in rows] == [SCHEMA_VERSION]


def test_migrate_returns_nothing_when_current(database: Database) -> None:
    assert database.migrate() == []


def test_schema_version_primary_key_blocks_duplicates(database: Database) -> None:
    """`version` が PRIMARY KEY であること（v4.5 / J-1）。

    **これが無いと二重適用で行が増える。**後から UNIQUE を張るのは §8.5 が禁じる
    「テーブル作り直し」に直結するため、v1 で入れている。
    """
    with pytest.raises(sqlite3.IntegrityError):
        database.conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, "2026-09-13T09:00:00+00:00"),
        )


def test_migration_backs_up_before_applying(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """既存データがある状態で新しい版を当てるときだけバックアップを作ること。"""
    with connect(db_path) as opened:
        opened.insert_recording(make_recording())

    real = db.load_migrations()  # monkeypatch の前に取る（取らないと無限再帰する）
    extra = db.Migration(version=2, name="0002_extra.sql", sql="ALTER TABLE events ADD COLUMN x;")
    monkeypatch.setattr(db, "load_migrations", lambda: [*real, extra])

    with connect(db_path) as opened:
        assert opened.schema_version() == 2
    backups = sorted(db_path.parent.glob(f"{db_path.name}.backup-v1-*"))
    assert len(backups) == 1
    restored = sqlite3.connect(backups[0])
    assert restored.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 1


def test_no_backup_on_first_creation(db_path: Path) -> None:
    """初回作成ではバックアップを作らない（退避するものが無い）。"""
    with connect(db_path):
        pass
    assert list(db_path.parent.glob(f"{db_path.name}.backup-*")) == []


# --- 一意性制約 ----------------------------------------------------------


def test_partkey_unique(database: Database) -> None:
    database.insert_recording(make_recording())
    with pytest.raises(sqlite3.IntegrityError, match="transmitter_id"):
        database.insert_recording(make_recording())


def test_partkey_allows_a_different_mic(database: Database) -> None:
    database.insert_recording(make_recording())
    database.insert_recording(make_recording(mic_index=2))
    assert database.counts().recordings == 2


@pytest.mark.parametrize("variant", ["denoised", "orig"])
def test_sha_partial_unique(database: Database, variant: str) -> None:
    column = f"sha256_{variant}"
    database.insert_recording(make_recording(mic_index=1, **{column: "deadbeef"}))
    with pytest.raises(sqlite3.IntegrityError, match=column):
        database.insert_recording(make_recording(mic_index=2, **{column: "deadbeef"}))


@pytest.mark.parametrize("variant", ["denoised", "orig"])
def test_sha_null_does_not_collide(database: Database, variant: str) -> None:
    """**未ハッシュ行（NULL）は何行でも通る**（§8.2 の部分 UNIQUE の狙い）。"""
    for index in range(3):
        database.insert_recording(make_recording(mic_index=index))
    assert database.counts().recordings == 3


def test_session_key_unique(database: Database) -> None:
    database.insert_session(make_session())
    with pytest.raises(sqlite3.IntegrityError, match="session_key"):
        database.insert_session(make_session())


def test_autoincrement_is_not_reused(database: Database) -> None:
    """削除しても id が再利用されないこと（§8.5 の前提）。

    §14.1 の削除条件は `recordings.id` の同一性に削除可否を賭けている。id が再利用されると
    **ノートの ID 42 と DB の ID 42 が別の Part を指しうる** — ND-01〜ND-28 がどれも
    検出できない削除事故の経路である。
    """
    first = database.insert_recording(make_recording(mic_index=1))
    second = database.insert_recording(make_recording(mic_index=2))
    assert (first, second) == (1, 2)
    database.conn.execute("DELETE FROM recordings WHERE id = ?", (second,))
    database.conn.commit()
    third = database.insert_recording(make_recording(mic_index=3))
    assert third == 3, "id が再利用された（AUTOINCREMENT が効いていない）"


def test_foreign_key_set_null(database: Database) -> None:
    session_id = database.insert_session(make_session())
    recording_id = database.insert_recording(make_recording(session_id=session_id))
    database.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    database.conn.commit()
    row = database.get_recording(recording_id)
    assert row is not None
    assert row.session_id is None


# --- record_transition（§8.4） ------------------------------------------


def test_record_transition_writes_both(database: Database) -> None:
    recording_id = database.insert_recording(make_recording())
    database.record_transition(
        EntityType.RECORDING,
        recording_id,
        from_status="DISCOVERED",
        to_status="NORMALIZING",
        now=NOW,
    )
    row = database.get_recording(recording_id)
    assert row is not None
    assert row.status == "NORMALIZING"
    events = database.events_for(EntityType.RECORDING, recording_id)
    assert [(e.from_status, e.to_status) for e in events] == [
        (None, "DISCOVERED"),
        ("DISCOVERED", "NORMALIZING"),
    ]


def test_record_transition_is_atomic(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """`events` の INSERT が失敗したら `status` も巻き戻ること（§8.4）。

    **「status だけ進んで events が無い」状態を作らない。**
    """
    recording_id = database.insert_recording(make_recording())

    def boom(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.IntegrityError("events が書けない")

    monkeypatch.setattr(database, "_insert_event", boom)
    with pytest.raises(sqlite3.IntegrityError):
        database.record_transition(
            EntityType.RECORDING,
            recording_id,
            from_status="DISCOVERED",
            to_status="NORMALIZING",
        )
    row = database.get_recording(recording_id)
    assert row is not None
    assert row.status == "DISCOVERED", "status だけ進んでしまった"
    assert len(database.events_for(EntityType.RECORDING, recording_id)) == 1


def test_record_transition_detects_conflict(database: Database) -> None:
    """`from_status` が現在値と違えば `TransitionConflict`。何も変わらないこと。"""
    recording_id = database.insert_recording(make_recording())
    with pytest.raises(TransitionConflict, match="status が 'TRANSCRIBING' ではありません"):
        database.record_transition(
            EntityType.RECORDING,
            recording_id,
            from_status="TRANSCRIBING",
            to_status="TRANSCRIBED",
        )
    row = database.get_recording(recording_id)
    assert row is not None
    assert row.status == "DISCOVERED"
    assert len(database.events_for(EntityType.RECORDING, recording_id)) == 1


def test_record_transition_on_a_missing_row(database: Database) -> None:
    with pytest.raises(TransitionConflict):
        database.record_transition(
            EntityType.RECORDING, 999, from_status="DISCOVERED", to_status="NORMALIZING"
        )


def test_session_transition(database: Database) -> None:
    session_id = database.insert_session(make_session())
    database.record_transition(
        EntityType.SESSION, session_id, from_status="OPEN", to_status="READY"
    )
    row = database.get_session(session_id)
    assert row is not None
    assert row.status == "READY"
    assert len(database.events_for(EntityType.SESSION, session_id)) == 2


def test_events_are_separated_by_entity_type(database: Database) -> None:
    """`entity_type` が違えば同じ id でも混ざらないこと。"""
    recording_id = database.insert_recording(make_recording())
    session_id = database.insert_session(make_session())
    assert recording_id == session_id == 1
    assert len(database.events_for(EntityType.RECORDING, 1)) == 1
    assert len(database.events_for(EntityType.SESSION, 1)) == 1


def test_insert_writes_the_first_event(database: Database) -> None:
    """行の誕生も状態遷移である（§8.4 の「すべての状態遷移」）。"""
    recording_id = database.insert_recording(make_recording())
    events = database.events_for(EntityType.RECORDING, recording_id)
    assert len(events) == 1
    assert events[0].from_status is None
    assert events[0].to_status == "DISCOVERED"


# --- retry_count（§15.2） ----------------------------------------------


def test_retry_count_reset_statuses_match_spec() -> None:
    """`RETRY_RESET_STATUSES` が §15.2 の名指しと一致すること。"""
    assert spec_retry_reset_statuses() == set(RETRY_RESET_STATUSES)


def test_retry_count_increments_on_failed(database: Database) -> None:
    recording_id = database.insert_recording(make_recording())
    database.record_transition(
        EntityType.RECORDING, recording_id, from_status="DISCOVERED", to_status="NORMALIZING"
    )
    database.record_transition(
        EntityType.RECORDING,
        recording_id,
        from_status="NORMALIZING",
        to_status=FAILED_STATUS,
        error_code="IMPORT_FAILED",
    )
    row = database.get_recording(recording_id)
    assert row is not None
    assert row.retry_count == 1
    assert row.error_code == "IMPORT_FAILED"


@pytest.mark.parametrize("to_status", sorted(RETRY_RESET_STATUSES))
def test_retry_count_resets_on_stage_pass(database: Database, to_status: str) -> None:
    """工程を通過したら 0 に戻ること（§15.2）。"""
    recording_id = database.insert_recording(make_recording(retry_count=2))
    database.record_transition(
        EntityType.RECORDING, recording_id, from_status="DISCOVERED", to_status=to_status
    )
    row = database.get_recording(recording_id)
    assert row is not None
    assert row.retry_count == 0


def test_retry_count_sequence(database: Database) -> None:
    """失敗 → 再試行 → 失敗 → 通過 で 1 → 2 → 0 になること。"""
    recording_id = database.insert_recording(make_recording())
    steps = [
        ("DISCOVERED", "NORMALIZING", None),
        ("NORMALIZING", FAILED_STATUS, 1),
        (FAILED_STATUS, "NORMALIZING", 1),
        ("NORMALIZING", FAILED_STATUS, 2),
        (FAILED_STATUS, "NORMALIZING", 2),
        ("NORMALIZING", "NORMALIZED", 0),
    ]
    for from_status, to_status, expected in steps:
        database.record_transition(
            EntityType.RECORDING, recording_id, from_status=from_status, to_status=to_status
        )
        if expected is not None:
            row = database.get_recording(recording_id)
            assert row is not None
            assert row.retry_count == expected, (from_status, to_status)


def test_updated_at_changes(database: Database) -> None:
    """遷移ごとに `updated_at` が進むこと（§15.2 の自動再試行がこれを基準にする）。"""
    recording_id = database.insert_recording(make_recording())
    later = NOW + timedelta(hours=1)
    database.record_transition(
        EntityType.RECORDING,
        recording_id,
        from_status="DISCOVERED",
        to_status="NORMALIZING",
        now=later,
    )
    row = database.get_recording(recording_id)
    assert row is not None
    assert row.updated_at == later.isoformat(timespec="seconds")


# --- 切り詰め（§8.4 / §16.3） ------------------------------------------


def test_truncate_keeps_short_text() -> None:
    assert truncate(None) is None
    assert truncate("x" * MAX_VALUE_CHARS) == "x" * MAX_VALUE_CHARS


def test_truncate_cuts_long_text() -> None:
    cut = truncate("x" * 300)
    assert cut is not None
    assert len(cut) == MAX_VALUE_CHARS
    assert cut.endswith("…")


def test_detail_is_truncated(database: Database) -> None:
    """`events.detail` が 200 文字で切り詰められること。**例外を投げない。**

    `events` は削除も圧縮もしない永続ストアであり、ログより漏洩の影響が長く残る（§8.4）。
    """
    recording_id = database.insert_recording(make_recording())
    database.record_transition(
        EntityType.RECORDING,
        recording_id,
        from_status="DISCOVERED",
        to_status="NORMALIZING",
        detail="秘密" * 300,
    )
    events = database.events_for(EntityType.RECORDING, recording_id)
    detail = events[-1].detail
    assert detail is not None
    assert len(detail) == MAX_VALUE_CHARS
    assert detail.endswith("…")


def test_error_message_is_truncated(database: Database) -> None:
    """`error_message` も同じ（ffmpeg / whisper の stderr が入る経路）。"""
    recording_id = database.insert_recording(make_recording())
    database.record_transition(
        EntityType.RECORDING,
        recording_id,
        from_status="DISCOVERED",
        to_status=FAILED_STATUS,
        error_message="e" * 500,
    )
    row = database.get_recording(recording_id)
    assert row is not None
    assert row.error_message is not None
    assert len(row.error_message) == MAX_VALUE_CHARS


def test_error_message_is_truncated_on_insert(database: Database) -> None:
    recording_id = database.insert_recording(make_recording(error_message="e" * 500))
    row = database.get_recording(recording_id)
    assert row is not None
    assert row.error_message is not None
    assert len(row.error_message) == MAX_VALUE_CHARS


# --- バックアップと件数 --------------------------------------------------


def test_backup_and_restore(database: Database, tmp_path: Path) -> None:
    """`Connection.backup()` で退避し、復元先から読めること（§8.5）。"""
    database.insert_recording(make_recording())
    database.insert_session(make_session())
    dest = tmp_path / "backup" / "copy.db"
    database.backup(dest)
    restored = sqlite3.connect(dest)
    assert restored.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 1
    assert restored.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    restored.close()


def test_counts(database: Database) -> None:
    assert database.counts() == db.Counts(0, 0, 0, database.counts().size_bytes)
    database.insert_recording(make_recording())
    database.insert_session(make_session())
    counts = database.counts()
    assert (counts.recordings, counts.sessions, counts.events) == (1, 1, 2)
    assert counts.size_bytes > 0


def test_file_size_includes_the_wal(database: Database, db_path: Path) -> None:
    """サイズは本体 + `-wal`。`-wal` を無視すると未チェックポイント分が見えない。"""
    database.insert_recording(make_recording())
    wal = db_path.with_name(f"{db_path.name}-wal")
    assert wal.exists()
    assert db.file_size(db_path) >= db_path.stat().st_size + wal.stat().st_size


def test_file_size_of_a_missing_db(tmp_path: Path) -> None:
    assert db.file_size(tmp_path / "nope.db") == 0


# --- 「status を直接 UPDATE しない」の静的検査 --------------------------


SRC_DIR = REPO_ROOT / "src" / "voicedock"
STATUS_UPDATE = re.compile(r"\bUPDATE\b.*?\bSET\b.*?\bstatus\b", re.S | re.I)


def test_no_direct_status_update() -> None:
    """`status` を UPDATE する SQL が `db.py::record_transition` 以外に無いこと（§8.4）。

    **すべての状態遷移は `events` への INSERT と同一トランザクションで行う。**遷移箇所が
    20 か所に散った後では全数修正できないため、増えた時点で落とす。
    """
    offenders: list[str] = []
    for module in sorted(SRC_DIR.glob("*.py")):
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            # **ソース断片を見る。**f-string は ast.Constant にならないため、
            # 文字列定数だけを走査すると `f"UPDATE {table} SET status = ?"` を取りこぼす
            segment = ast.get_source_segment(source, node) or ""
            if STATUS_UPDATE.search(segment):
                offenders.append(f"{module.name}::{node.name}")
    assert offenders == ["db.py::record_transition"], (
        f"status を UPDATE するのは record_transition だけにする（§8.4）: {offenders}"
    )


def test_the_static_check_actually_matches() -> None:
    """検査そのものが効くこと。"""
    assert STATUS_UPDATE.search("UPDATE recordings SET status = ? WHERE id = ?")
    assert STATUS_UPDATE.search("update sessions set status=?, x=?")
    assert not STATUS_UPDATE.search("SELECT status FROM recordings WHERE id = ?")
