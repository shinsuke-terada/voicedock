"""SQLite への接続・スキーマ・状態遷移（SPEC §8）。

**SQLite が全状態の単一の真実である**（§8.1）。ファイルの有無は補助的な確認材料に過ぎない。

このモジュールの要点は 2 つある。

1. **状態遷移の API は `record_transition()` だけである。**§8.4 は「すべての状態遷移は
   `events` への INSERT と同一トランザクション」と規定する。`status` を直接 UPDATE する
   関数を**作らない** — 遷移箇所が 20 か所に散った後では全数修正できない。
   `tests/unit/test_db.py::test_no_direct_status_update` が静的に固定している。
2. **スキーマは v1 完全形である。**§8.5 は破壊的変更を禁じている。§14.1 の削除条件は
   `recordings.id` の同一性に削除可否を賭けており、AUTOINCREMENT が振り直されると
   **ノートの ID 42 と DB の ID 42 が別の Part を指しうる**。

`config` を import しない。`busy_timeout_ms` と `tz` は引数で受ける（`paths.py` と同じ方針）。
**状態名の文字列は持たない。**`states.py` が唯一の出所である（§9）。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime, tzinfo
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, Final, Self

from voicedock.log import MAX_VALUE_CHARS
from voicedock.states import FAILED_STATUS, RETRY_RESET_STATUSES

SCHEMA_VERSION: Final = 1
MIGRATIONS_PACKAGE: Final = "voicedock.migrations"
MIGRATION_PATTERN: Final = re.compile(r"^(?P<version>\d{4})_[a-z0-9_]+\.sql$")
DEFAULT_BUSY_TIMEOUT_MS: Final = 10000

TRUNCATION_MARKER: Final = "…"
"""切り詰めた印。`MAX_VALUE_CHARS` を含めた長さに収める。"""


class EntityType(StrEnum):
    """`events.entity_type`（§8.4）。"""

    RECORDING = "recording"
    SESSION = "session"

    @property
    def table(self) -> str:
        return "recordings" if self is EntityType.RECORDING else "sessions"


class TransitionConflict(RuntimeError):
    """現在の `status` が `from_status` と違った。

    同時実行の競合か、呼び出し側が現在の状態を誤認している。**どちらも進めてはならない。**
    """


# --- 行（§8.2 / §8.3 / §8.4 の列と 1 対 1） ------------------------------


@dataclass(frozen=True)
class Recording:
    """`recordings` の 1 行（§8.2）。フィールドは列と 1 対 1 に保つこと。"""

    device_id: str
    source_folder: str
    transmitter_id: str
    mic_index: int
    started_at: str
    status: str
    updated_at: str
    id: int | None = None
    duration_seconds: float | None = None
    ended_at: str | None = None
    source_path_denoised: str | None = None
    source_path_orig: str | None = None
    source_size_denoised: int | None = None
    source_size_orig: int | None = None
    source_mtime_denoised: float | None = None
    source_mtime_orig: float | None = None
    sha256_denoised: str | None = None
    sha256_orig: str | None = None
    sha256_helper: str | None = None
    primary_variant: str | None = None
    inbox_path: str | None = None
    staging_dir: str | None = None
    normalized_path: str | None = None
    transcript_path: str | None = None
    session_id: int | None = None
    retry_count: int = 0
    auto_retry_rounds: int = 0
    error_code: str | None = None
    error_message: str | None = None
    source_deleted_at: str | None = None


@dataclass(frozen=True)
class Session:
    """`sessions` の 1 行（§8.3）。"""

    session_key: str
    day_date: str
    device_id: str
    status: str
    updated_at: str
    id: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    recorded_seconds: float | None = None
    part_count: int = 0
    failed_part_count: int = 0
    title: str | None = None
    analysis_path: str | None = None
    raw_output_path: str | None = None
    raw_output_sha256: str | None = None
    output_path: str | None = None
    output_sha256: str | None = None
    retry_count: int = 0
    auto_retry_rounds: int = 0
    regenerated_count: int = 0
    delete_attempts: int = 0
    error_code: str | None = None
    error_message: str | None = None
    source_deleted_at: str | None = None


@dataclass(frozen=True)
class Event:
    """`events` の 1 行（§8.4）。"""

    entity_type: str
    entity_id: int
    to_status: str
    created_at: str
    id: int | None = None
    from_status: str | None = None
    error_code: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class Counts:
    """doctor D-2 が表示する件数とサイズ。"""

    recordings: int
    sessions: int
    events: int
    size_bytes: int


def truncate(text: str | None) -> str | None:
    """§8.4 / §16.3: 200 文字で切り詰める。**例外を投げない。**

    ここはエラー経路である。予期しない長い `stderr` で状態遷移そのものが記録できなく
    なるのが最悪であり、切り詰めであって拒否ではない（§1.3）。
    """
    if text is None or len(text) <= MAX_VALUE_CHARS:
        return text
    return text[: MAX_VALUE_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _column_names(row_type: type) -> tuple[str, ...]:
    return tuple(field.name for field in fields(row_type))


def _from_row(row_type: type[Any], row: sqlite3.Row) -> Any:
    """列名で dataclass へ詰める（位置に依存しない）。"""
    # sqlite3.Row を直接 iterate すると**値**が返る。列名が要るので .keys() を使う
    # （SIM118 の「key in dict」への書き換えはここでは誤りである）
    return row_type(**{key: row[key] for key in row.keys()})  # noqa: SIM118


# --- マイグレーション ----------------------------------------------------


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    """`migrations/NNNN_*.sql` を**版の昇順**で返す（§8.5）。"""
    found: list[Migration] = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        matched = MIGRATION_PATTERN.match(entry.name)
        if matched is None:
            continue
        found.append(
            Migration(
                version=int(matched.group("version")),
                name=entry.name,
                sql=entry.read_text(encoding="utf-8"),
            )
        )
    if not found:
        raise RuntimeError(f"マイグレーションが見つかりません: {MIGRATIONS_PACKAGE}")
    return sorted(found, key=lambda m: m.version)


# --- 接続 ----------------------------------------------------------------


class Database:
    """接続 1 本と、その上の操作。"""

    def __init__(self, conn: sqlite3.Connection, path: Path, *, tz: tzinfo = UTC) -> None:
        self.conn = conn
        self.path = path
        self.tz = tz

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # --- スキーマ --------------------------------------------------------

    def schema_version(self) -> int | None:
        """適用済みの最大の版。`schema_version` テーブルが無ければ `None`。"""
        if not self._has_table("schema_version"):
            return None
        row = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        value = row[0]
        return int(value) if value is not None else None

    def _has_table(self, name: str) -> bool:
        found = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return found is not None

    def migrate(self, *, now: datetime | None = None) -> list[int]:
        """未適用の版を昇順に適用し、適用した版の一覧を返す（§8.5）。

        **適用する版が無ければ何も書かない。**`docker compose restart` で
        `schema_version` の行が増えないのはこのためである。
        """
        current = self.schema_version()
        pending = [m for m in load_migrations() if current is None or m.version > current]
        if not pending:
            return []

        if current is not None:
            # 既存のデータがある状態でスキーマを変えるので、先に退避する。
            # **単純なファイルコピーはしない**（WAL の未チェックポイント分が失われる。§8.5）
            self.backup(self._backup_path(current, now=now))

        moment = self._now(now)
        applied: list[int] = []
        for migration in pending:
            with self.conn:
                self.conn.executescript(migration.sql)
                self.conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (migration.version, moment),
                )
            applied.append(migration.version)
        return applied

    def _backup_path(self, current: int, *, now: datetime | None) -> Path:
        stamp = self._now(now).replace(":", "").replace("-", "")
        return self.path.with_name(f"{self.path.name}.backup-v{current}-{stamp}")

    def backup(self, dest: Path) -> None:
        """`Connection.backup()` で退避する（§8.5）。

        **WAL のため単純なファイルコピーは禁止**である。`-wal` の未チェックポイント分が
        失われ、「保存済みなのに未保存」と誤認する事故をバックアップ手順自体が作る。
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(dest) as target:
            self.conn.backup(target)
        target.close()

    # --- 状態遷移（唯一の API） ------------------------------------------

    def record_transition(
        self,
        entity: EntityType,
        entity_id: int,
        *,
        from_status: str,
        to_status: str,
        error_code: str | None = None,
        error_message: str | None = None,
        detail: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """状態を進め、`events` へ 1 行残す（§8.4）。

        **これが状態を変える唯一の API である。**UPDATE と INSERT を同一トランザクションで
        行うので、「`status` だけ進んで `events` が無い」状態にならない。

        `WHERE id = ? AND status = ?` で**現在の状態を確かめる**。合わなければ
        `TransitionConflict` を投げ、`with` 文が巻き戻す。

        `retry_count` は §15.2 に従う。工程通過（`RETRY_RESET_STATUSES`）で 0、
        `FAILED` へ入るときに +1、それ以外はそのまま。

        **どの遷移が許されるかはここで検査しない**（#11 の `states.py` と #19 が担う）。
        """
        moment = self._now(now)
        if to_status in RETRY_RESET_STATUSES:
            retry_expr = "0"
        elif to_status == FAILED_STATUS:
            retry_expr = "retry_count + 1"
        else:
            retry_expr = "retry_count"

        with self.conn:
            cursor = self.conn.execute(
                f"UPDATE {entity.table} SET status = ?, retry_count = {retry_expr}, "  # noqa: S608
                "error_code = ?, error_message = ?, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    to_status,
                    error_code,
                    truncate(error_message),
                    moment,
                    entity_id,
                    from_status,
                ),
            )
            if cursor.rowcount != 1:
                raise TransitionConflict(
                    f"{entity.table} id={entity_id} の status が {from_status!r} ではありません"
                    f"（{to_status!r} へ進めません）"
                )
            self._insert_event(
                entity, entity_id, from_status, to_status, error_code, detail, moment
            )

    def _insert_event(
        self,
        entity: EntityType,
        entity_id: int,
        from_status: str | None,
        to_status: str,
        error_code: str | None,
        detail: str | None,
        created_at: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events "
            "(entity_type, entity_id, from_status, to_status, error_code, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entity, entity_id, from_status, to_status, error_code, truncate(detail), created_at),
        )

    # --- 行の作成と読み取り ----------------------------------------------

    def insert_recording(self, row: Recording, *, now: datetime | None = None) -> int:
        """`recordings` へ 1 行入れ、初回の `events`（`from_status` は NULL）も残す。

        **行の誕生も状態遷移である**（§8.4 の「すべての状態遷移」）。
        """
        return self._insert(EntityType.RECORDING, Recording, row, now=now)

    def insert_session(self, row: Session, *, now: datetime | None = None) -> int:
        return self._insert(EntityType.SESSION, Session, row, now=now)

    def _insert(
        self,
        entity: EntityType,
        row_type: type[Any],
        row: Any,
        *,
        now: datetime | None,
    ) -> int:
        columns = [name for name in _column_names(row_type) if name != "id"]
        values = [getattr(row, name) for name in columns]
        if "error_message" in columns:
            values[columns.index("error_message")] = truncate(row.error_message)
        placeholders = ", ".join("?" for _ in columns)
        with self.conn:
            cursor = self.conn.execute(
                f"INSERT INTO {entity.table} ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608
                values,
            )
            entity_id = int(cursor.lastrowid or 0)
            self._insert_event(entity, entity_id, None, row.status, None, None, self._now(now))
        return entity_id

    def get_recording(self, recording_id: int) -> Recording | None:
        row = self.conn.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
        return None if row is None else _from_row(Recording, row)

    def get_session(self, session_id: int) -> Session | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return None if row is None else _from_row(Session, row)

    def events_for(self, entity: EntityType, entity_id: int) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE entity_type = ? AND entity_id = ? ORDER BY id",
            (entity, entity_id),
        ).fetchall()
        return [_from_row(Event, row) for row in rows]

    # --- doctor 用 -------------------------------------------------------

    def counts(self) -> Counts:
        """件数とファイルサイズ（§19.2 D-2）。

        サイズは**本体 + `-wal` の合計**にする。`-wal` を無視すると、まだ
        チェックポイントされていない分が見えない。`-shm` は再生成される作業ファイル
        なので含めない。
        """
        return Counts(
            recordings=self._count("recordings"),
            sessions=self._count("sessions"),
            events=self._count("events"),
            size_bytes=file_size(self.path),
        )

    def _count(self, table: str) -> int:
        row = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
        return int(row[0])

    # --- 内部 ------------------------------------------------------------

    def _now(self, now: datetime | None) -> str:
        moment = now if now is not None else datetime.now(self.tz)
        return moment.isoformat(timespec="seconds")


def file_size(path: Path) -> int:
    """DB 本体と `-wal` の合計バイト数。"""
    total = 0
    for candidate in (path, path.with_name(f"{path.name}-wal")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def connect(
    path: Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    tz: tzinfo = UTC,
    migrate: bool = True,
    now: datetime | None = None,
) -> Database:
    """DB へ接続し、§8.1 の PRAGMA を設定する。

    `migrate=False` にすると**スキーマを作らない**。doctor が「DB が無い」ことを
    「DB が無い」と報告できるようにするため、診断は副作用を持ってはならない（§19.2 D-2）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        # 黙って delete のままになると耐久性の前提が崩れる（§8.1）
        conn.close()
        raise RuntimeError(f"journal_mode を WAL にできませんでした: {mode!r}（{path}）")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA synchronous = FULL")

    database = Database(conn, path, tz=tz)
    if migrate:
        database.migrate(now=now)
    return database


def structure(conn: sqlite3.Connection) -> Mapping[str, object]:
    """スキーマの構造を書式に依存しない形で返す（テストの照合用）。

    列（名前・型・NOT NULL・既定値）と索引（対象表・UNIQUE・部分索引・列）を見る。
    `sqlite_autoindex_*`（`UNIQUE` 制約が作る暗黙の索引）も含める。
    """
    out: dict[str, object] = {}
    tables = [
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for table in tables:
        out[f"table:{table}"] = [
            (row[1], row[2], bool(row[3]), row[4])
            for row in conn.execute(f"PRAGMA table_info({table})")
        ]
        for index in conn.execute(f"PRAGMA index_list({table})"):
            name, unique, partial = index[1], bool(index[2]), bool(index[4])
            columns = [
                row[2] for row in conn.execute(f"PRAGMA index_xinfo({name})") if row[2] is not None
            ]
            out[f"index:{name}"] = (table, unique, partial, columns)
    return out


def apply_sql(conn: sqlite3.Connection, statements: Iterable[str]) -> None:
    """SQL をまとめて流す（テストが SPEC の DDL を流すのに使う）。"""
    for sql in statements:
        conn.executescript(sql)
