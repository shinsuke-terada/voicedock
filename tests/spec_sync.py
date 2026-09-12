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
