"""マイグレーションが SPEC §8.5 の禁則を守っていることを静的に検査する。

§8.5 は破壊的変更を禁じている。理由は §14.1 の削除条件が **`recordings.id` の同一性に
削除可否を賭けている**ことで、SQLite 定番の「新テーブル → INSERT SELECT → RENAME」で
スキーマを変えると AUTOINCREMENT が振り直され、**ノートの ID 42 と DB の ID 42 が別の
Part を指しうる。これは ND-01〜ND-28 がどれも検出できない削除事故の経路である。**
"""

from __future__ import annotations

import re

import pytest

from voicedock.db import SCHEMA_VERSION, Migration, load_migrations

FORBIDDEN: dict[str, re.Pattern[str]] = {
    "DROP TABLE": re.compile(r"\bDROP\s+TABLE\b", re.I),
    "DROP COLUMN": re.compile(r"\bDROP\s+COLUMN\b", re.I),
    "ALTER TABLE ... RENAME": re.compile(r"\bALTER\s+TABLE\b.*?\bRENAME\b", re.I | re.S),
    "CREATE TABLE ... AS": re.compile(r"\bCREATE\s+TABLE\b.*?\bAS\s+SELECT\b", re.I | re.S),
}
"""どの版にも現れてはならない構文。いずれもテーブルの作り直しに直結する。"""

CREATE_TABLE = re.compile(r"\bCREATE\s+TABLE\b", re.I)
INSERT_SCHEMA_VERSION = re.compile(r"\bINSERT\s+INTO\s+schema_version\b", re.I)


def migrations() -> list[Migration]:
    found = load_migrations()
    assert found, "マイグレーションが 1 件も無い"
    return found


def strip_comments(sql: str) -> str:
    """`--` 行コメントを落とす（コメント中の語で誤検出しないため）。"""
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


def test_migration_files_are_numbered() -> None:
    versions = [m.version for m in migrations()]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions)), f"版が重複している: {versions}"
    assert versions == list(range(1, len(versions) + 1)), f"版に飛びがある: {versions}"


def test_schema_version_is_the_max_file() -> None:
    assert max(m.version for m in migrations()) == SCHEMA_VERSION


@pytest.mark.parametrize("label", sorted(FORBIDDEN))
def test_no_destructive_statements(label: str) -> None:
    """破壊的変更がどの版にも無いこと（§8.5）。"""
    pattern = FORBIDDEN[label]
    offenders = [m.name for m in migrations() if pattern.search(strip_comments(m.sql))]
    assert offenders == [], f"{label} は使えない（§8.5）: {offenders}"


def test_only_the_first_migration_creates_tables() -> None:
    """v2 以降は `ALTER TABLE ADD COLUMN` に限る（§8.5）。"""
    offenders = [
        m.name for m in migrations() if m.version > 1 and CREATE_TABLE.search(strip_comments(m.sql))
    ]
    assert offenders == [], f"v2 以降で CREATE TABLE は使えない（§8.5）: {offenders}"


def test_migrations_do_not_insert_schema_version() -> None:
    """`schema_version` への INSERT は `db.py` が行う（SQL に書かない）。"""
    offenders = [
        m.name for m in migrations() if INSERT_SCHEMA_VERSION.search(strip_comments(m.sql))
    ]
    assert offenders == [], f"schema_version への INSERT は db.py が行う: {offenders}"


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("DROP TABLE", "DROP TABLE events;"),
        ("DROP COLUMN", "ALTER TABLE recordings DROP COLUMN status;"),
        ("ALTER TABLE ... RENAME", "ALTER TABLE recordings RENAME TO recordings_old;"),
        ("CREATE TABLE ... AS", "CREATE TABLE t AS SELECT * FROM recordings;"),
    ],
)
def test_the_checks_actually_detect_violations(label: str, sql: str) -> None:
    """**検査そのものが効くこと。**文字列を直接与えて確認する。"""
    assert FORBIDDEN[label].search(sql), f"{label} を検出できていない"


def test_comments_do_not_trigger_false_positives() -> None:
    """コメント中の語で誤検出しないこと（`0001_initial.sql` が実際に言及している）。"""
    sql = "-- DROP TABLE や RENAME は禁止（§8.5）\nCREATE TABLE t (id INTEGER);"
    for pattern in FORBIDDEN.values():
        assert not pattern.search(strip_comments(sql))
