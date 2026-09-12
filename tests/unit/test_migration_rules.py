"""マイグレーションが SPEC §8.5 の規則を守っていることを静的に検査する。

**v5.1 で禁則が 3 点から 1 点へ変わった**（v5.0→v5.1 の変更 M-2 / `docs/STORE.md` §7）。

v5.0 までの §8.5 は「テーブルを作り直すな / スキーマは v1 完全形 /
`ALTER TABLE ADD COLUMN` 以外禁止」だった。これは **`AUTOINCREMENT` を守るため**である。
§14.1 の削除条件が `part.id in frontmatter_ids(note)` という**整数の間接参照**で、
振り直されると「ノートの ID 42」と「DB の ID 42」が別の Part を指しえたからである。
だから当時は「振り直しに繋がる構文」をここで**代理で**禁止していた。

**v5.1 の識別子は自然キーなので、作り直しても鍵は変わらない。**したがって禁則は
「`partkey` / `session_key` の算出規則を変えるな」の 1 点だけになり、
**その不変量は `tests/unit/test_paths.py::test_partkey_is_pinned` が直接固定する。**
代理の構文検査はここから消えた。`test_db.py::test_partkey_survives_a_table_rebuild` が、
禁じていた操作を実際にやっても鍵が保たれることを示している。

ここに残るのは**版番号とファイル構成の規約**だけである。
"""

from __future__ import annotations

import re

from voicedock.db import SCHEMA_VERSION, Migration, load_migrations

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


def test_only_the_first_migration_creates_tables() -> None:
    """v2 以降は原則 `ALTER TABLE ADD COLUMN`（§8.5）。

    **これは安全性の規則ではなくデータ損失を避けるための規約である。**作り直しが本当に
    必要になったら、行数の一致を確かめるテストを添えてこの検査を緩めてよい。
    v5.0 までの「絶対禁止」とは意味が違う（モジュール docstring を参照）。
    """
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


def test_the_first_migration_creates_every_table() -> None:
    """v1 が 4 つのテーブルすべてを作ること。

    `test_db.py::test_schema_matches_spec` が SPEC との一致を見るので内容は見ない。
    ここでは「v1 だけを読めばスキーマの全体が分かる」ことを固定する。
    """
    first = next(m for m in migrations() if m.version == 1)
    created = set(re.findall(r"CREATE\s+TABLE\s+(\w+)", strip_comments(first.sql), re.I))
    assert created == {"sessions", "recordings", "events", "schema_version"}


def test_the_comment_records_the_one_time_rewrite() -> None:
    """v1 の冒頭コメントが「1 回だけ書き直した」ことを残していること（§8.5）。

    本番データが無い間だけ許される操作だったという事実が、ファイルを開いた人に
    見えなくなると、2 回目をやってよいと誤解されうる。
    """
    first = next(m for m in migrations() if m.version == 1)
    head = first.sql[:2000]
    assert "v5.1" in head
    assert "1 回だけ" in head
