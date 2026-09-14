"""`voicedock.llm` のスキーマ生成とプロンプトが SPEC §12.2 / §12.5 と一致することを固定する。

**`sections` の on/off がプロンプト・スキーマ・ノートの 3 箇所に一貫して反映される**のが
§12.2 の要点である。どれか 1 つだけ追従しないと、「設定で切ったのにプロンプトには残って
いる」状態になり、**モデルが出した項目が `extra="forbid"` で弾かれて `LLM_INVALID_JSON`
になる。**
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.helpers import PROMPTS_ROOT
from tests.spec_sync import spec_section_code, spec_section_text, spec_text
from voicedock import llm
from voicedock.config import Config
from voicedock.llm import (
    LIST_SECTIONS,
    NOT_A_SECTION,
    PromptKind,
    build_schema,
    enabled_sections,
    render_schema_block,
    system_prompt,
)

"""プロンプト置き場は `tests.helpers.PROMPTS_ROOT` を使う。

**絶対パスを書いてはならない。**コンテナでは `/app/prompts`、CI ではリポジトリ直下で、
`Path("/app/prompts")` と書くと **`make test` は通るのに CI だけが落ちる**（実際に落ちた）。
`REPO_ROOT` から導けばどちらでも解決する。
"""


@pytest.fixture
def cfg(make_config: Callable[..., Config]) -> Config:
    return make_config()


def valid_payload() -> dict[str, object]:
    return {
        "title": "開発と打ち合わせの一日",
        "summary": "削除条件を整理した。",
        "key_points": ["整理した"],
        "tasks": [{"text": "確認する", "due": None}],
        "decisions": ["GUI は作らない"],
        "ideas": ["話者識別"],
        "tags": ["VoiceDock"],
    }


# --- 既定の形（§12.2） --------------------------------------------------


def test_the_default_schema_matches_the_spec(cfg: Config) -> None:
    """既定の `sections` で §12.2 の `AnalysisResult` と同じ項目が生成されること。"""
    fields = set(build_schema(cfg).model_fields)
    assert fields == {
        "title",
        "summary",
        "key_points",
        "tasks",
        "decisions",
        "ideas",
        "tags",
    }


def test_the_partial_schema_has_no_title_or_tags(cfg: Config) -> None:
    """`PartialAnalysis` は `title` / `tags` を持たない（§12.2）。"""
    fields = set(build_schema(cfg, partial=True).model_fields)
    assert "title" not in fields
    assert "tags" not in fields
    assert fields == {"summary", "key_points", "tasks", "decisions", "ideas"}


def test_the_spec_class_list_matches_the_implementation(cfg: Config) -> None:
    """§12.2 のコードブロックに並ぶフィールドと生成結果が一致すること。

    **SPEC に 1 行足したらここが落ちる。**スキーマが設定から生成される以上、
    SPEC の例と食い違っていたら設定の既定値か生成規則のどちらかが間違っている。
    """
    block = spec_section_code("12.2", "python")
    body = block[block.index("class AnalysisResult") :]
    body = body[: body.index("class PartialAnalysis")]
    listed = re.findall(r"^    (\w+)\s*:", body, re.M)
    assert set(listed) - {"model_config"} == set(build_schema(cfg).model_fields)


def test_timeline_is_not_a_schema_field(cfg: Config) -> None:
    """**`sections.timeline` はスキーマ生成の対象外**（§12.2）。

    LLM の出力項目ではなく、Map 中間結果からレンダラが組み立てる（§13.4）。
    """
    assert "timeline" in cfg.llm.analysis.sections
    assert cfg.llm.analysis.sections["timeline"].enabled
    assert "timeline" not in build_schema(cfg).model_fields
    assert "timeline" not in enabled_sections(cfg.llm.analysis)
    assert {"timeline"} == NOT_A_SECTION


def test_title_exists_whenever_summary_does(cfg: Config) -> None:
    """`title` は `summary` が有効なら常に生成する（§12.2）。

    `sections` に `title` という項目は無い。V-18 が `summary.enabled: true` を
    強制しているので実質「常に在る」。
    """
    assert "title" not in cfg.llm.analysis.sections
    assert "title" in build_schema(cfg).model_fields


# --- enabled: false の追従（§12.2 の要点） -------------------------------


@pytest.mark.parametrize("name", ["key_points", "tasks", "decisions", "ideas", "tags"])
def test_a_disabled_section_leaves_the_schema(
    make_config: Callable[..., Config], name: str
) -> None:
    cfg = make_config({"llm": {"analysis": {"sections": {name: {"enabled": False}}}}})
    assert name not in build_schema(cfg).model_fields


@pytest.mark.parametrize("name", ["key_points", "tasks", "decisions", "ideas", "tags"])
def test_a_disabled_section_leaves_the_prompt(
    make_config: Callable[..., Config], name: str
) -> None:
    """**プロンプトからも消えること。**

    残ると、モデルがその項目を出して `extra="forbid"` で弾かれる。
    **設定を切ったことが `LLM_INVALID_JSON` の原因になる**のが一番避けたい形である。
    """
    cfg = make_config({"llm": {"analysis": {"sections": {name: {"enabled": False}}}}})
    assert name not in render_schema_block(cfg)


def test_a_disabled_section_is_rejected_by_the_schema(
    make_config: Callable[..., Config],
) -> None:
    """切った項目をモデルが出してきたら弾く（`extra="forbid"`。§12.2）。"""
    cfg = make_config({"llm": {"analysis": {"sections": {"ideas": {"enabled": False}}}}})
    model = build_schema(cfg)
    with pytest.raises(ValidationError):
        model.model_validate(valid_payload())


# --- max_items → max_length（§12.2） ------------------------------------


@pytest.mark.parametrize("name", ["key_points", "tasks", "decisions", "ideas", "tags"])
def test_max_items_becomes_max_length(make_config: Callable[..., Config], name: str) -> None:
    cfg = make_config({"llm": {"analysis": {"sections": {name: {"max_items": 2}}}}})
    model = build_schema(cfg)
    payload = valid_payload()
    payload[name] = [{"text": "a"}] * 3 if name == "tasks" else ["a", "b", "c"]
    with pytest.raises(ValidationError, match="at most 2"):
        model.model_validate(payload)


def test_a_list_section_without_max_items_has_no_limit(
    make_config: Callable[..., Config],
) -> None:
    """**`max_items` が無い項目に `max_length` を付けない**（§7.2）。

    既定値を勝手に入れると、**設定に書いていない上限が生まれる。**`summary` /
    `timeline` は §7.2 でそもそも `max_items` を持たない。
    """
    cfg = make_config({"llm": {"analysis": {"sections": {"ideas": {"max_items": None}}}}})
    assert cfg.llm.analysis.sections["ideas"].max_items is None
    payload = valid_payload() | {"ideas": [f"思いつき {n}" for n in range(200)]}
    result = build_schema(cfg).model_validate(payload)
    assert len(result.ideas) == 200  # type: ignore[attr-defined]


def test_summary_has_no_item_limit(cfg: Config) -> None:
    """`summary` は文字列なので件数の上限を持たない（§7.2 / §12.2）。"""
    assert cfg.llm.analysis.sections["summary"].max_items is None


def test_the_item_limit_is_not_shown_in_the_prompt(make_config: Callable[..., Config]) -> None:
    """**リストの件数の上限をモデルへ見せない**（#98）。

    v5.14 までは「（最大 N 件）」をプロンプトへ出しており、docstring には
    **「出さないと守られない」**と書いてあった。**実機の A/B がそれを否定した。**
    モデルは上限を**埋めるべき目標**として解釈し、**録音に無い内容をでっち上げて
    数を合わせる。**79 文字の文字起こしに対し `key_points` が 19 件（上限 20）出た。

    | | `key_points` | `ideas` | `tags` |
    |---|---|---|---|
    | 見せる | 19 | 15 | 15 |
    | 見せない | **5** | **3** | **7** |

    §1.3 の優先順位 1 は記録の保護である。**要約に言っていないことが混ざるのは
    記録の破壊と同じ**であり、しかも Daily は Raw より読まれる。
    """
    cfg = make_config({"llm": {"analysis": {"sections": {"ideas": {"max_items": 7}}}}})
    block = render_schema_block(cfg)
    assert "最大 7 件" not in block, block
    assert "7" not in block.split('"ideas"')[1].split("\n")[0], block


def test_the_spec_says_not_to_show_the_item_limit() -> None:
    """**§12.2 がそう規定していること。**

    実装だけ直しても、SPEC が元のままなら**次に実装する人が戻す。**
    v5.14 まで実装の註記は「上限を明示する（モデルが守りやすくなる）」だった。
    """
    text = spec_section_text("12.2")
    assert "リストの件数の上限をモデルへ見せてはならない" in text
    assert "プロンプトには出さない" in text


def test_the_character_limit_is_still_shown(make_config: Callable[..., Config]) -> None:
    """**文字数の上限は見せたまま。**1 つの文字列の長さであって「埋めるべき個数」ではない。

    ここまで消すと、**要約が際限なく伸びる**恐れがある（未測定の領域を
    まとめて変えない）。
    """
    cfg = make_config({})
    block = render_schema_block(cfg)
    assert "文字以内" in block, block


def test_the_limit_still_validates(make_config: Callable[..., Config]) -> None:
    """**上限そのものは捨てていない**（§12.2）。

    プロンプトから消したのは「見せ方」だけで、`max_items` は `max_length` として
    pydantic が検証する。超過したら §12.3 の修復が走り、**修復プロンプトの
    `{errors}` で初めて件数の上限が伝わる。**
    """
    cfg = make_config({"llm": {"analysis": {"sections": {"ideas": {"max_items": 2}}}}})
    model = build_schema(cfg)
    with pytest.raises(ValidationError, match="at most 2 items"):
        model.model_validate(valid_payload() | {"ideas": ["a", "b", "c"]})


# --- Pydantic 検証（§12.3 の 2） ----------------------------------------


def test_the_spec_example_validates(cfg: Config) -> None:
    """§12.2 の「期待する出力例」がそのまま通ること。"""
    import json

    example = json.loads(spec_section_code("12.2", "json"))
    assert build_schema(cfg).model_validate(example)


def test_extra_keys_are_rejected(cfg: Config) -> None:
    payload = valid_payload() | {"mood": "よい"}
    with pytest.raises(ValidationError, match="Extra inputs"):
        build_schema(cfg).model_validate(payload)


def test_a_missing_required_field_is_rejected(cfg: Config) -> None:
    payload = valid_payload()
    del payload["summary"]
    with pytest.raises(ValidationError, match="summary"):
        build_schema(cfg).model_validate(payload)


def test_an_empty_summary_is_rejected(cfg: Config) -> None:
    """`min_length=1`（§12.2）。**空の要約でノートを書かせない。**"""
    with pytest.raises(ValidationError):
        build_schema(cfg).model_validate(valid_payload() | {"summary": ""})


def test_a_wrong_type_is_rejected(cfg: Config) -> None:
    with pytest.raises(ValidationError):
        build_schema(cfg).model_validate(valid_payload() | {"key_points": "文字列"})


def test_a_long_title_is_rejected(cfg: Config) -> None:
    with pytest.raises(ValidationError):
        build_schema(cfg).model_validate(valid_payload() | {"title": "あ" * 200})


def test_a_task_requires_text(cfg: Config) -> None:
    with pytest.raises(ValidationError):
        build_schema(cfg).model_validate(valid_payload() | {"tasks": [{"due": None}]})


def test_a_task_due_may_be_null(cfg: Config) -> None:
    """期限が明示されていなければ `null`（§12.5 の制約）。"""
    result = build_schema(cfg).model_validate(valid_payload())
    assert result.tasks[0].due is None  # type: ignore[attr-defined]


def test_list_fields_default_to_empty(cfg: Config) -> None:
    """**欠けていたら空配列。**§12.5 が「判断できない項目は空配列に」と指示している。"""
    result = build_schema(cfg).model_validate({"title": "t", "summary": "s"})
    for name in LIST_SECTIONS:
        assert getattr(result, name) == []


# --- プロンプト（§12.5） ------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "section_index"),
    [("analyze_ja.txt", 0), ("reduce_ja.txt", 1), ("repair_json.txt", 2)],
)
def test_the_prompt_files_match_the_spec_verbatim(filename: str, section_index: int) -> None:
    """**§12.5 の本文と逐語一致すること。**

    プロンプトは出力の質を直接決めるので、SPEC を唯一の出所にする。片方だけ直すと
    「SPEC に書いた制約が効いていない」ことに誰も気づかない。
    """
    blocks = re.findall(r"```text\n(.*?)\n```", _section_12_5(), re.S)
    expected = blocks[section_index].rstrip("\n") + "\n"
    actual = (PROMPTS_ROOT / filename).read_text(encoding="utf-8")
    assert actual == expected


def _section_12_5() -> str:
    text = spec_text()
    start = text.index("### 12.5")
    return text[start : text.index("## 13. ", start)]


def test_the_map_prompt_is_the_analyze_prompt_without_title_or_tags() -> None:
    """§12.5: 「上記から `title` / `tags` を除き、
    『これは 1 日の記録の一部である』旨を加えたもの」。

    **逐語の写しが無い唯一のプロンプト**なので、構造で縛る。
    """
    analyze = (PROMPTS_ROOT / "analyze_ja.txt").read_text(encoding="utf-8")
    mapped = (PROMPTS_ROOT / "map_ja.txt").read_text(encoding="utf-8")

    assert "title" not in mapped, "title の指示が残っている"
    assert "1 日の記録の一部" in mapped
    for line in analyze.splitlines():
        if line.startswith("- ") and "title" not in line:
            assert line in mapped, f"analyze の制約が抜けている: {line}"


@pytest.mark.parametrize(
    "filename", ["analyze_ja.txt", "map_ja.txt", "reduce_ja.txt", "repair_json.txt"]
)
def test_no_prompt_asks_for_wikilinks(filename: str) -> None:
    """**LLM に `[[ ]]` を生成させてはならない**（§14.4 N-15）。

    存在しないノートへのリンクを幻覚する（§13.8）。analyze / map / reduce は
    禁止を明記し、repair は本文を生成しないので言及しない。
    """
    text = (PROMPTS_ROOT / filename).read_text(encoding="utf-8")
    if filename == "repair_json.txt":
        assert "[[" not in text
        return
    assert "本文に [[ ]] 形式のリンクを書かないでください。" in text


@pytest.mark.parametrize("kind", [PromptKind.ANALYZE, PromptKind.MAP, PromptKind.REDUCE])
def test_the_placeholders_are_filled(cfg: Config, kind: PromptKind) -> None:
    rendered = system_prompt(cfg, kind)
    assert "{schema_block}" not in rendered
    assert "{custom_instructions}" not in rendered
    assert "summary" in rendered


def test_custom_instructions_are_appended(make_config: Callable[..., Config]) -> None:
    cfg = make_config({"llm": {"analysis": {"custom_instructions": "健康の話題は要約しない"}}})
    assert "健康の話題は要約しない" in system_prompt(cfg, PromptKind.ANALYZE)


def test_the_map_prompt_uses_the_partial_schema(cfg: Config) -> None:
    rendered = system_prompt(cfg, PromptKind.MAP)
    assert '"title"' not in rendered
    assert '"tags"' not in rendered


def test_the_prompt_loader_tolerates_json_braces(tmp_path: Path) -> None:
    """**`str.format()` を使わない。**

    プロンプト本文には `{"text": ...}` のような JSON の例が入るので、`format()` は
    `KeyError` で落ちる。
    """
    template = tmp_path / "p.txt"
    template.write_text('例: {"text": "a"}\n{schema_block}\n', encoding="utf-8")
    assert llm.load_prompt(template, schema_block="X") == '例: {"text": "a"}\nX\n'
