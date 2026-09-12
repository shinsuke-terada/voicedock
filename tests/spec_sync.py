"""`docs/SPEC.md` から表・一覧を取り出すヘルパ。

**SPEC と実装の整合をテストで固定するためにある。**§15.1 にエラーコードを 1 行足したのに
enum を忘れた、という事故を即座に落とす。

`make test` は `docs/` を読み取り専用でマウントする（SPEC §17.4）。SPEC が見つからない
場合は **skip せず fail する**。見つからないのは設定の壊れであり、黙って通すと
整合テストが存在しないのと同じになる。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
import yaml

SPEC_PATH = Path(__file__).parents[1] / "docs" / "SPEC.md"


def spec_text() -> str:
    """`docs/SPEC.md` の全文。見つからなければ fail する。"""
    if not SPEC_PATH.is_file():
        pytest.fail(
            f"docs/SPEC.md が見つかりません: {SPEC_PATH}。"
            "make test は docs/ を ro マウントする（SPEC §17.4）。マウント設定を確認すること"
        )
    return SPEC_PATH.read_text(encoding="utf-8")


# 次の見出し。`#` の後を「数字」か「付録」に限る。
# `^#+ ` だけで切ると、§7.2 の yaml ブロック中の `# ==========` をコメントではなく
# 見出しと見なしてしまい、節が途中で切れる。
_NEXT_HEADING = r"(?=^#{1,6} (?:\d|付録)|\Z)"


def _section(heading: str) -> str:
    """`### <heading>` から次の見出しまでの本文を返す。"""
    text = spec_text()
    m = re.search(
        rf"^#+ {re.escape(heading)}[^\n]*\n(.*?)" + _NEXT_HEADING,
        text,
        re.S | re.M,
    )
    if m is None:
        pytest.fail(f"SPEC に見出し {heading!r} が見つかりません")
    return m.group(1)


def spec_error_codes() -> dict[str, str]:
    """§15.1 の表から「エラーコード → リトライ列の原文」を返す。"""
    rows = re.findall(
        r"^\| `([A-Z][A-Z_]*)` \| ([^|]*?) \| ([^|]*?) \| ([^|]*?) \| ([^|]*?) \|",
        _section("15.1"),
        re.M,
    )
    codes = {code: retry.strip() for code, _cat, _where, retry, _next in rows}
    if not codes:
        pytest.fail("SPEC §15.1 の表を読み取れませんでした")
    return codes


def spec_error_categories() -> dict[str, str]:
    """§15.1 の表から「エラーコード → 分類列の原文」を返す。"""
    rows = re.findall(
        r"^\| `([A-Z][A-Z_]*)` \| ([^|]*?) \|",
        _section("15.1"),
        re.M,
    )
    return {code: category.strip() for code, category in rows}


def spec_startup_aborting_codes() -> set[str]:
    """§15.1 の表で遷移先が「起動中止」であるエラーコード。"""
    rows = re.findall(
        r"^\| `([A-Z][A-Z_]*)` \| [^|]*? \| [^|]*? \| [^|]*? \| ([^|]*?) \|",
        _section("15.1"),
        re.M,
    )
    return {code for code, next_state in rows if "起動中止" in next_state}


def spec_event_names() -> list[str]:
    """§16.4 のコードブロックからイベント名を出現順に返す。"""
    m = re.search(r"```text\n(.*?)\n```", _section("16.4"), re.S)
    if m is None:
        pytest.fail("SPEC §16.4 のコードブロックを読み取れませんでした")
    return [name.strip() for name in re.split(r"[/\n]", m.group(1)) if name.strip()]


def spec_exit_codes() -> dict[int, str]:
    """§17.3 の表から「終了コード → 意味」を返す。"""
    rows = re.findall(r"^\| `(\d+)` \| ([^|]*?) \|", _section("17.3"), re.M)
    if not rows:
        pytest.fail("SPEC §17.3 の表を読み取れませんでした")
    return {int(code): meaning.strip() for code, meaning in rows}


def spec_log_text_examples() -> dict[str, str]:
    """§16.2 の text 例から「イベント名 → タイムスタンプ以降の全文」を返す。

    タイムスタンプは実行時刻になるため落とす。残り（レベル・イベント名・全フィールド）は
    実装の出力とバイト単位で一致しなければならない。
    """
    m = re.search(r"```text\n(.*?)\n```", _section("16.2"), re.S)
    if m is None:
        pytest.fail("SPEC §16.2 の text 例を読み取れませんでした")
    examples: dict[str, str] = {}
    for line in m.group(1).splitlines():
        _ts, rest = line.split(" ", 1)
        examples[rest.split()[1]] = rest
    return examples


def spec_log_json_example() -> str:
    """§16.2 の json 例の 1 行。"""
    m = re.search(r"```json\n(.*?)\n```", _section("16.2"), re.S)
    if m is None:
        pytest.fail("SPEC §16.2 の json 例を読み取れませんでした")
    return m.group(1).strip()


def spec_config_example() -> dict[str, object]:
    """§7.2 の yaml ブロックを safe_load して返す（`config.example.yaml` の正）。"""
    m = re.search(r"```yaml\n(.*?)\n```", _section("7.2"), re.S)
    if m is None:
        pytest.fail("SPEC §7.2 の yaml ブロックを読み取れませんでした")
    document = yaml.safe_load(m.group(1))
    if not isinstance(document, dict):
        pytest.fail("SPEC §7.2 の yaml ブロックがマッピングになっていません")
    return document


def spec_validation_rules() -> dict[str, str]:
    """§7.3 の表から「規則 ID → 規則本文」を出現順に返す。

    **打ち消し（`~~`）の行は除く。**v4.1 で V-5 / V-6、v4.2 で V-28 を廃止しており、
    廃止された規則を実装することはない（番号は詰めない）。
    """
    rows = re.findall(r"^\| (V-\d+) \| ([^|]*?) \| ([^|]*?) \|", _section("7.3"), re.M)
    rules = {rule: body.strip() for rule, body, _error in rows if not body.strip().startswith("~~")}
    if not rules:
        pytest.fail("SPEC §7.3 の表を読み取れませんでした")
    return rules


def spec_schema_sql() -> list[str]:
    """§8.2 / §8.3 / §8.4 / §8.5 の ```sql ブロックを出現順に返す。

    これを空の DB へ流したものが**スキーマの正**である。マイグレーションで作った構造と
    `PRAGMA` レベルで突き合わせることで、SPEC に列を足して写し忘れる事故が落ちる。
    """
    blocks: list[str] = []
    for section in ("8.2", "8.3", "8.4", "8.5"):
        found = re.findall(r"```sql\n(.*?)\n```", _section(section), re.S)
        if not found:
            pytest.fail(f"SPEC §{section} の sql ブロックを読み取れませんでした")
        blocks += found
    return blocks


def spec_retry_reset_statuses() -> set[str]:
    """§15.2 の「工程を通過したら 0 にリセットする」で名指しされた状態。"""
    matched = re.search(
        r"`retry_count` は「現在の工程での連続失敗回数」を意味する.*?（(.*?)への遷移時）",
        _section("15.2"),
        re.S,
    )
    if matched is None:
        pytest.fail("SPEC §15.2 の retry_count リセット規則を読み取れませんでした")
    return set(re.findall(r"`([A-Z_]+)`", matched.group(1)))


# --- §9 の状態機械 -------------------------------------------------------

_TRANSITION_SKIP_SOURCES: Final = frozenset({"現状態", "---", "—", "各工程通過"})


def spec_states(section: str) -> list[str]:
    """§9.1 / §9.2 の状態表から状態名を出現順に返す。"""
    found = re.findall(r"^\| `([A-Z_]+)` \|", _section(section), re.M)
    if not found:
        pytest.fail(f"SPEC §{section} の状態表を読み取れませんでした")
    return found


def spec_mermaid_edges(section: str) -> set[tuple[str, str]]:
    """§9.1 / §9.2 の mermaid 図の辺。`[*] -->`（初期状態）は含めない。"""
    m = re.search(r"```mermaid\n(.*?)\n```", _section(section), re.S)
    if m is None:
        pytest.fail(f"SPEC §{section} の mermaid を読み取れませんでした")
    return {
        (source, target)
        for source, target in re.findall(
            r"^\s*(\[\*\]|[A-Z_]+)\s*-->\s*([A-Z_]+)", m.group(1), re.M
        )
        if source != "[*]"
    }


def spec_initial_state(section: str) -> str:
    """`[*] --> X` の X。"""
    m = re.search(r"```mermaid\n(.*?)\n```", _section(section), re.S)
    if m is None:
        pytest.fail(f"SPEC §{section} の mermaid を読み取れませんでした")
    found: list[str] = re.findall(r"^\s*\[\*\]\s*-->\s*([A-Z_]+)", m.group(1), re.M)
    if len(found) != 1:
        pytest.fail(f"SPEC §{section} の初期状態が 1 つに定まりません: {found}")
    return found[0]


def spec_transition_edges(entity: str) -> set[tuple[str, str]]:
    """§9.3 の遷移表を辺集合へ正規化する。

    `entity` は `"part"` または `"session"`。次の行は辺にしない。

    - 現状態が `—`（行の作成）または `各工程通過`（`retry_count` の注記）
    - 次状態が `（遷移なし）`
    - 次状態が `X のまま`（§9.3 の注記どおり**遷移ではない**）

    現状態の `` `A` / `B` `` は両方へ展開し、次状態の `直前の進行中状態（...）` は
    括弧内の状態名へ展開する。
    """
    body = _section("9.3")
    if "**Session:**" not in body:
        pytest.fail("SPEC §9.3 の Part / Session の境目が見つかりません")
    part_chunk, session_chunk = body.split("**Session:**", 1)
    chunk = {"part": part_chunk, "session": session_chunk}[entity]

    edges: set[tuple[str, str]] = set()
    for source, _event, _guard, target, _effect in re.findall(
        r"^\| (.+?) \| (.+?) \| (.+?) \| (.+?) \| (.+?) \|$", chunk, re.M
    ):
        if source in _TRANSITION_SKIP_SOURCES or target == "（遷移なし）" or "のまま" in target:
            continue
        for start in re.findall(r"`([A-Z_]+)`", source):
            for end in re.findall(r"`([A-Z_]+)`", target):
                edges.add((start, end))
    if not edges:
        pytest.fail(f"SPEC §9.3 の {entity} 表を読み取れませんでした")
    return edges


def spec_recovery_map() -> list[tuple[str, str]]:
    """§9.4 の巻き戻し表（`A -> B` のコードブロック）。"""
    m = re.search(r"```text\n((?:\w+\s+->\s+\w+.*\n)+)```", _section("9.4"))
    if m is None:
        pytest.fail("SPEC §9.4 の巻き戻し表を読み取れませんでした")
    return re.findall(r"^(\w+)\s+->\s+(\w+)", m.group(1), re.M)


def spec_status_tuple(name: str) -> list[str]:
    """§14.1 のコードブロックにある `NAME = (...)` の中身。"""
    m = re.search(rf"^{re.escape(name)} = \((.*?)\)$", _section("14.1"), re.S | re.M)
    if m is None:
        pytest.fail(f"SPEC §14.1 に {name} が見つかりません")
    return re.findall(r'"([A-Z_]+)"', m.group(1))


def spec_part_terminal_paragraph() -> set[str]:
    """§9.1 の「**終端状態**（Session の進行判定に使う）」の段落から状態名を取る。"""
    m = re.search(r"\*\*終端状態\*\*[^:：]*[:：]\s*(.+)", _section("9.1"))
    if m is None:
        pytest.fail("SPEC §9.1 の終端状態の段落を読み取れませんでした")
    return set(re.findall(r"`([A-Z_]+)`", m.group(1)))
