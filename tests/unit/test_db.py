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
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from tests.helpers import REPO_ROOT
from tests.spec_sync import spec_retry_reset_statuses, spec_schema_sql
from voicedock import db
from voicedock.db import (
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
from voicedock.paths import DevicePath, partkey_for
from voicedock.states import FAILED_STATUS, RETRY_RESET_STATUSES, PartStatus

NOW = datetime(2026, 9, 13, 9, 0, 0)


DEVICE_ID = "DJIMIC3"
FOLDER = "TX_MIC001_20260912_120950"
FILENAME = "TX00_MIC001_20260912_120950_orig.wav"


def relpath(folder: str = FOLDER, filename: str = FILENAME) -> DevicePath:
    return DevicePath(PurePosixPath(folder) / filename)


def pkey(device_id: str = DEVICE_ID, folder: str = FOLDER, filename: str = FILENAME) -> str:
    """`partkey` は **必ず `paths.partkey_for()` で作る**（§8.1）。

    テストの中で文字列を連結してしまうと、算出規則を変えても DB のテストが落ちず、
    **§8.5 の唯一の禁則が守られていることを確かめられなくなる。**
    """
    return partkey_for(device_id, relpath(folder, filename))


def make_recording(**overrides: Any) -> Recording:
    base: dict[str, Any] = {
        "partkey": pkey(),
        "device_id": DEVICE_ID,
        "source_folder": FOLDER,
        "transmitter_id": "TX00",
        "mic_index": 1,
        "started_at": "2026-09-12T12:09:50+09:00",
        "source_path": str(relpath()),
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


def test_recordings_has_25_columns(database: Database) -> None:
    """列数の記録（v5.1 で 30 列 → 24 列、v5.46 で 25 列、v5.56 で 26 列）。

    v5.0 までは「v1 完全形」を保つための数だった（§8.5 が破壊的変更を禁じており、
    後から足せない列を先に入れてあった）。**v5.1 で `0001_initial.sql` を 1 回だけ
    書き直し、使っていない 6 列を落とした**（v5.0→v5.1 の変更 M-4）。

    いまこの数が意味するのは「不要な列が紛れ込んでいないこと」だけである。
    増やすのは `ALTER TABLE ADD COLUMN` で構わない（§8.5 の禁則は算出規則だけになった）。

    **v5.46 で `delete_request_id` を 1 列足した**（変更 BH-1）。
    `ALTER TABLE ADD COLUMN` である（`0002_delete_request_id.sql`）。

    **v5.56 で `duplicate_of` を 1 列足した**（`0003_duplicate_of.sql`）。重複の双子を
    `error_message` の文字列から解析しないための列である（§14.1 根拠 B）。
    """
    columns = structure(database.conn)["table:recordings"]
    assert isinstance(columns, list)
    assert len(columns) == 26


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
        "idx_recordings_sha",
        "idx_recordings_status",
        "idx_recordings_session",
        "idx_recordings_started",
        "idx_sessions_status",
        "idx_sessions_day",
        "idx_events_entity",
    }, "idx_recordings_partkey は PRIMARY KEY に包含されたので v5.1 で削除した（§8.2）"


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
    """**適用した版が 1 行ずつ残る。**`schema_version()` はその最大値を返す。"""
    assert database.schema_version() == SCHEMA_VERSION
    rows = database.conn.execute("SELECT version, applied_at FROM schema_version").fetchall()
    assert [row[0] for row in rows] == list(range(1, SCHEMA_VERSION + 1))


def test_migration_is_idempotent(db_path: Path) -> None:
    """2 回 `connect()` しても `schema_version` の行が増えないこと（`restart` 相当）。"""
    with connect(db_path) as first:
        assert first.schema_version() == SCHEMA_VERSION
    with connect(db_path) as second:
        rows = second.conn.execute("SELECT version FROM schema_version").fetchall()
    assert [row[0] for row in rows] == list(range(1, SCHEMA_VERSION + 1))


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
    # **いまの最新より 1 つ新しい版**を足す。番号を決め打ちすると版を上げるたびに落ちる
    nxt = SCHEMA_VERSION + 1
    extra = db.Migration(
        version=nxt, name=f"{nxt:04d}_extra.sql", sql="ALTER TABLE events ADD COLUMN x;"
    )
    monkeypatch.setattr(db, "load_migrations", lambda: [*real, extra])

    with connect(db_path) as opened:
        assert opened.schema_version() == nxt
    backups = sorted(db_path.parent.glob(f"{db_path.name}.backup-v{SCHEMA_VERSION}-*"))
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
    with pytest.raises(sqlite3.IntegrityError, match="partkey"):
        database.insert_recording(make_recording())


def test_partkey_allows_a_different_file(database: Database) -> None:
    """同じフォルダの別ファイルは別 Part である。"""
    database.insert_recording(make_recording())
    other = "TX00_MIC002_20260912_120950_orig.wav"
    database.insert_recording(
        make_recording(
            partkey=pkey(filename=other),
            mic_index=2,
            source_path=str(relpath(filename=other)),
        )
    )
    assert database.counts().recordings == 2


def test_partkey_is_not_null(database: Database) -> None:
    """**`TEXT PRIMARY KEY` は NOT NULL を含意しない**（SQLite の既知の逸脱）。

    `INTEGER PRIMARY KEY` だけが rowid の別名として NULL を弾く。`NOT NULL` の明記を
    忘れると `partkey` が NULL の行を作れてしまい、§14.1 が参照する識別子が消える。
    """
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        database.conn.execute(
            "INSERT INTO recordings"
            " (partkey, device_id, source_folder, transmitter_id, mic_index,"
            "  started_at, status, updated_at)"
            " VALUES (NULL, 'd', 'f', 'TX00', 1, 'x', 'DISCOVERED', 'x')"
        )


def test_session_key_is_not_null(database: Database) -> None:
    """`sessions.session_key` も同じ（§8.3）。"""
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        database.conn.execute(
            "INSERT INTO sessions (session_key, day_date, device_id, status, updated_at)"
            " VALUES (NULL, '2026-09-12', 'd', 'OPEN', 'x')"
        )


def test_sha_partial_unique(database: Database) -> None:
    database.insert_recording(make_recording(sha256="deadbeef"))
    other = "TX00_MIC002_20260912_120950_orig.wav"
    with pytest.raises(sqlite3.IntegrityError, match="sha256"):
        database.insert_recording(
            make_recording(
                partkey=pkey(filename=other),
                source_path=str(relpath(filename=other)),
                sha256="deadbeef",
            )
        )


def test_sha_null_does_not_collide(database: Database) -> None:
    """**未ハッシュ行（NULL）は何行でも通る**（§8.2 の部分 UNIQUE の狙い）。"""
    for index in range(3):
        name = f"TX00_MIC00{index}_20260912_120950_orig.wav"
        database.insert_recording(
            make_recording(partkey=pkey(filename=name), source_path=str(relpath(filename=name)))
        )
    assert database.counts().recordings == 3


def test_session_key_unique(database: Database) -> None:
    database.insert_session(make_session())
    with pytest.raises(sqlite3.IntegrityError, match="session_key"):
        database.insert_session(make_session())


def test_partkey_survives_a_table_rebuild(database: Database) -> None:
    """**v5.0 までの §8.5 の禁則が不要になったことを実際に確かめる。**

    v5.0 までは「新テーブル → INSERT SELECT → RENAME」を禁じていた。`AUTOINCREMENT` が
    振り直されると、§14.1 の `part.id in frontmatter_ids(note)` が
    **ノートの 42 と DB の 42 が別の Part を指す**状態を作りえたからである。

    識別子が自然キーになった v5.1 では、**同じ操作をしても鍵は変わらない。**
    ここでその禁則そのものを破ってみて、`partkey` が保たれることを示す
    （v5.0→v5.1 の変更 M-1 / M-2、`docs/STORE.md` §7）。
    """
    original = database.insert_recording(make_recording())
    database.conn.executescript(
        "PRAGMA foreign_keys=OFF;"
        " CREATE TABLE recordings_new AS SELECT * FROM recordings;"
        " DROP TABLE recordings;"
        " ALTER TABLE recordings_new RENAME TO recordings;"
        " PRAGMA foreign_keys=ON;"
    )
    rebuilt = database.conn.execute("SELECT partkey FROM recordings").fetchall()
    assert [row[0] for row in rebuilt] == [original]


def test_foreign_key_set_null(database: Database) -> None:
    session_key = database.insert_session(make_session())
    partkey = database.insert_recording(make_recording(session_key=session_key))
    database.conn.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))
    database.conn.commit()
    row = database.get_recording(partkey)
    assert row is not None
    assert row.session_key is None


# --- record_transition（§8.4） ------------------------------------------


def test_record_transition_writes_both(database: Database) -> None:
    partkey = database.insert_recording(make_recording())
    database.record_transition(
        EntityType.RECORDING,
        partkey,
        from_status="DISCOVERED",
        to_status="NORMALIZING",
        now=NOW,
    )
    row = database.get_recording(partkey)
    assert row is not None
    assert row.status == "NORMALIZING"
    events = database.events_for(EntityType.RECORDING, partkey)
    assert [(e.from_status, e.to_status) for e in events] == [
        (None, "DISCOVERED"),
        ("DISCOVERED", "NORMALIZING"),
    ]


def test_record_transition_is_atomic(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """`events` の INSERT が失敗したら `status` も巻き戻ること（§8.4）。

    **「status だけ進んで events が無い」状態を作らない。**
    """
    partkey = database.insert_recording(make_recording())

    def boom(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.IntegrityError("events が書けない")

    monkeypatch.setattr(database, "_insert_event", boom)
    with pytest.raises(sqlite3.IntegrityError):
        database.record_transition(
            EntityType.RECORDING,
            partkey,
            from_status="DISCOVERED",
            to_status="NORMALIZING",
        )
    row = database.get_recording(partkey)
    assert row is not None
    assert row.status == "DISCOVERED", "status だけ進んでしまった"
    assert len(database.events_for(EntityType.RECORDING, partkey)) == 1


def test_record_transition_detects_conflict(database: Database) -> None:
    """`from_status` が現在値と違えば `TransitionConflict`。何も変わらないこと。"""
    partkey = database.insert_recording(make_recording())
    with pytest.raises(TransitionConflict, match="status が 'TRANSCRIBING' ではありません"):
        database.record_transition(
            EntityType.RECORDING,
            partkey,
            from_status="TRANSCRIBING",
            to_status="TRANSCRIBED",
        )
    row = database.get_recording(partkey)
    assert row is not None
    assert row.status == "DISCOVERED"
    assert len(database.events_for(EntityType.RECORDING, partkey)) == 1


def test_record_transition_on_a_missing_row(database: Database) -> None:
    with pytest.raises(TransitionConflict):
        database.record_transition(
            EntityType.RECORDING,
            pkey(filename="TX99_MIC009_20260101_000000_orig.wav"),
            from_status="DISCOVERED",
            to_status="NORMALIZING",
        )


def test_session_transition(database: Database) -> None:
    session_key = database.insert_session(make_session())
    database.record_transition(
        EntityType.SESSION, session_key, from_status="OPEN", to_status="READY"
    )
    row = database.get_session(session_key)
    assert row is not None
    assert row.status == "READY"
    assert len(database.events_for(EntityType.SESSION, session_key)) == 2


def test_events_are_separated_by_entity_type(database: Database) -> None:
    """`entity_type` が違えば同じ `entity_key` でも混ざらないこと。

    `events.entity_key` には FK が無く（Part と Session の両方を受けるため）、
    照会は `WHERE entity_type = ? AND entity_key = ?` の 2 列で行う。**その分離を
    直接確かめる**ため、同じ鍵で 2 種類のイベントを書いてみる。
    """
    shared = "same-key"
    for entity in (EntityType.RECORDING, EntityType.SESSION):
        database.conn.execute(
            "INSERT INTO events (entity_type, entity_key, to_status, created_at)"
            " VALUES (?, ?, 'X', 'x')",
            (entity, shared),
        )
    database.conn.commit()
    assert len(database.events_for(EntityType.RECORDING, shared)) == 1
    assert len(database.events_for(EntityType.SESSION, shared)) == 1


def test_recording_and_session_keys_cannot_collide(database: Database) -> None:
    """**v5.1 では実際の鍵が衝突しえない。**

    `partkey` は `<device_id>/...` で必ず `/` を含み（§8.1）、`session_key` は
    `<device_id>:<YYYYMMDD>` で `/` を含まない（§8.3）。v5.0 までは両方が 1 から始まる
    連番だったので、`entity_type` を付け忘れると Part 42 と Session 42 が混ざりえた。
    """
    partkey = database.insert_recording(make_recording())
    session_key = database.insert_session(make_session())
    assert "/" in partkey
    assert "/" not in session_key
    assert partkey != session_key


def test_insert_writes_the_first_event(database: Database) -> None:
    """行の誕生も状態遷移である（§8.4 の「すべての状態遷移」）。"""
    partkey = database.insert_recording(make_recording())
    events = database.events_for(EntityType.RECORDING, partkey)
    assert len(events) == 1
    assert events[0].from_status is None
    assert events[0].to_status == "DISCOVERED"


# --- retry_count（§15.2） ----------------------------------------------


def test_retry_count_reset_statuses_match_spec() -> None:
    """`RETRY_RESET_STATUSES` が §15.2 の名指しと一致すること。

    定数の出所は `states.py` である（#11 で `db.py` から移した）。
    """
    assert spec_retry_reset_statuses() == set(RETRY_RESET_STATUSES)


def test_retry_count_increments_on_failed(database: Database) -> None:
    partkey = database.insert_recording(make_recording())
    database.record_transition(
        EntityType.RECORDING, partkey, from_status="DISCOVERED", to_status="NORMALIZING"
    )
    database.record_transition(
        EntityType.RECORDING,
        partkey,
        from_status="NORMALIZING",
        to_status=FAILED_STATUS,
        error_code="IMPORT_FAILED",
    )
    row = database.get_recording(partkey)
    assert row is not None
    assert row.retry_count == 1
    assert row.error_code == "IMPORT_FAILED"


@pytest.mark.parametrize("to_status", sorted(RETRY_RESET_STATUSES))
def test_retry_count_resets_on_stage_pass(database: Database, to_status: str) -> None:
    """工程を通過したら 0 に戻ること（§15.2）。"""
    partkey = database.insert_recording(make_recording(retry_count=2))
    database.record_transition(
        EntityType.RECORDING, partkey, from_status="DISCOVERED", to_status=to_status
    )
    row = database.get_recording(partkey)
    assert row is not None
    assert row.retry_count == 0


def test_retry_count_sequence(database: Database) -> None:
    """失敗 → 再試行 → 失敗 → 通過 で 1 → 2 → 0 になること。"""
    partkey = database.insert_recording(make_recording())
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
            EntityType.RECORDING, partkey, from_status=from_status, to_status=to_status
        )
        if expected is not None:
            row = database.get_recording(partkey)
            assert row is not None
            assert row.retry_count == expected, (from_status, to_status)


def test_updated_at_changes(database: Database) -> None:
    """遷移ごとに `updated_at` が進むこと（§15.2 の自動再試行がこれを基準にする）。"""
    partkey = database.insert_recording(make_recording())
    later = NOW + timedelta(hours=1)
    database.record_transition(
        EntityType.RECORDING,
        partkey,
        from_status="DISCOVERED",
        to_status="NORMALIZING",
        now=later,
    )
    row = database.get_recording(partkey)
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
    partkey = database.insert_recording(make_recording())
    database.record_transition(
        EntityType.RECORDING,
        partkey,
        from_status="DISCOVERED",
        to_status="NORMALIZING",
        detail="秘密" * 300,
    )
    events = database.events_for(EntityType.RECORDING, partkey)
    detail = events[-1].detail
    assert detail is not None
    assert len(detail) == MAX_VALUE_CHARS
    assert detail.endswith("…")


def test_error_message_is_truncated(database: Database) -> None:
    """`error_message` も同じ（ffmpeg / whisper の stderr が入る経路）。"""
    partkey = database.insert_recording(make_recording())
    database.record_transition(
        EntityType.RECORDING,
        partkey,
        from_status="DISCOVERED",
        to_status=FAILED_STATUS,
        error_message="e" * 500,
    )
    row = database.get_recording(partkey)
    assert row is not None
    assert row.error_message is not None
    assert len(row.error_message) == MAX_VALUE_CHARS


def test_error_message_is_truncated_on_insert(database: Database) -> None:
    partkey = database.insert_recording(make_recording(error_message="e" * 500))
    row = database.get_recording(partkey)
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


def test_recordings_with_status_is_ordered_by_started_at(database: Database) -> None:
    """**§10.0 の処理順（古い録音から）で返す**（変更 BK-4）。

    v5.48 まで同じ問い合わせが 2 本あり、**順序だけ違った** ——
    `db` の側は `partkey` 昇順、`pipeline` 側の写しは `started_at` 昇順である。
    写しのほうは `db._from_row`（非公開）を関数内 import で掴んでもいた。

    **`partkey` 順に戻すとこのテストが落ちる**（`partkey` は
    `<device>/<folder>/<file>` なので、時刻の逆順になる木を作ってある）。
    """
    for hour, folder in ((15, "TX_MIC001_20260912_090000"), (9, "TX_MIC001_20260912_150000")):
        database.insert_recording(
            make_recording(
                partkey=f"DJIMIC3/{folder}/TX00_MIC001_{hour:02d}_orig.wav",
                source_folder=folder,
                started_at=f"2026-09-12T{hour:02d}:00:00+09:00",
                status=PartStatus.SOURCE_DELETE_PENDING,
            )
        )

    found = database.recordings_with_status(PartStatus.SOURCE_DELETE_PENDING)

    assert [row.started_at[11:13] for row in found] == ["09", "15"], "古い録音から返していない"
