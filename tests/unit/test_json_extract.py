"""`voicedock.llm.extract_json` が SPEC §12.3 の 3 段抽出を満たすことを固定する。

**`response_format` に非対応のバックエンドでも動かすために独自に抽出する**（§12.1）。
ローカルモデルは「JSON だけを返せ」と言っても説明文・コードフェンス・思考過程を
付けてくる。ここが弱いと **`LLM_INVALID_JSON` が日常的に出る。**
"""

from __future__ import annotations

import json

import pytest

from voicedock.llm import extract_json, strip_think

VALID = {"title": "一日", "summary": "まとめ", "tasks": []}


# --- §12.3 の 5 パターン -------------------------------------------------


def test_bare_json() -> None:
    assert extract_json(json.dumps(VALID)) == VALID


def test_fenced_json() -> None:
    assert extract_json(f"```json\n{json.dumps(VALID)}\n```") == VALID


def test_fence_without_a_language() -> None:
    assert extract_json(f"```\n{json.dumps(VALID)}\n```") == VALID


def test_prose_around_the_json() -> None:
    text = f"はい、整理しました。\n\n{json.dumps(VALID)}\n\n以上です。"
    assert extract_json(text) == VALID


def test_thinking_tags_are_removed() -> None:
    text = f"<think>まず何を出すか考える</think>\n{json.dumps(VALID)}"
    assert extract_json(text) == VALID


@pytest.mark.parametrize(
    "text",
    ["", "ただの文章です", "{", "{'single': 'quotes'}", "[1, 2, 3]", '"文字列"', "null"],
)
def test_unusable_output_returns_none(text: str) -> None:
    """**例外を投げない。**取れなければ `None`（呼び手が repair へ回す。§12.3）。"""
    assert extract_json(text) is None


def test_a_top_level_array_yields_its_first_object() -> None:
    """配列で包まれて返ってきたら**中の最初のオブジェクトを救済する。**

    `json.loads` は配列を返すので `None`（オブジェクトでない）になるが、3 段目の
    括弧の釣り合いが中のオブジェクトを拾う。**reduce は中間結果の JSON 配列を入力に
    するので、モデルが入力につられて配列で包むことが実際に起きる。**救済して検証へ
    回すほうがよく、形が違えば Pydantic が弾く。
    """
    document = {"title": "t", "summary": "s"}
    assert extract_json(f"[{json.dumps(document)}]") == document


# --- `<think>`（§12.3） -------------------------------------------------


def test_json_inside_think_is_not_taken() -> None:
    """**`<think>` 内の JSON を採らない。**

    除去を後回しにして括弧の釣り合いから始めると、**思考中に書いた下書きを採ってしまう。**
    """
    text = '<think>{"title": "下書き", "summary": "捨てる"}</think>' + json.dumps(VALID)
    assert extract_json(text) == VALID


def test_an_unclosed_think_swallows_the_rest() -> None:
    """閉じていない `<think>` は以降すべてを思考とみなす。

    `max_tokens` で切られたときにこうなる。**下書きを本物として採らない**ほうが安全である
    （repair へ回れば済む）。
    """
    assert extract_json('<think>{"title": "途中"') is None


def test_multiple_think_blocks_are_removed() -> None:
    text = "<think>A</think>前置き<think>B</think>" + json.dumps(VALID)
    assert extract_json(text) == VALID


def test_strip_think_leaves_other_text() -> None:
    assert strip_think("前<think>中</think>後") == "前後"


# --- 括弧の釣り合い（§12.3 の 3 段目） ----------------------------------


def test_a_brace_inside_a_string_is_not_the_end() -> None:
    """**文字列リテラルを跨がない。**

    `{"text": "}"}` の `}` を終端と誤認すると、**壊れた JSON を抽出して
    「抽出は成功したが検証で落ちる」**になる。原因が分かりにくい失敗である。
    """
    document = {"summary": "閉じ括弧 } を含む文", "tasks": []}
    text = f"説明\n{json.dumps(document, ensure_ascii=False)}\n以上"
    assert extract_json(text) == document


def test_an_escaped_quote_inside_a_string() -> None:
    document = {"summary": '引用 " を含む'}
    assert extract_json(f"前{json.dumps(document, ensure_ascii=False)}後") == document


def test_a_backslash_before_the_closing_quote() -> None:
    document = {"summary": "末尾が \\ で終わる"}
    assert extract_json(f"x{json.dumps(document, ensure_ascii=False)}y") == document


def test_nested_objects() -> None:
    document = {"tasks": [{"text": "a", "due": None}], "summary": "s"}
    assert extract_json(f"説明\n{json.dumps(document)}") == document


def test_trailing_text_after_the_object() -> None:
    assert extract_json(json.dumps(VALID) + "\n\nこれで完了です。") == VALID


def test_the_first_object_wins_when_two_are_present() -> None:
    """2 つ並んでいたら**最初**を採る（§12.3 は「最初の `{` から」と書いている）。"""
    first = {"summary": "1 本目"}
    second = {"summary": "2 本目"}
    text = f"{json.dumps(first, ensure_ascii=False)}\n{json.dumps(second, ensure_ascii=False)}"
    assert extract_json(text) == first


def test_a_fence_is_preferred_over_balanced_extraction() -> None:
    """フェンスが在ればそちらを先に試す（§12.3 の順序）。"""
    text = "前置きに { があります\n```json\n" + json.dumps(VALID) + "\n```"
    assert extract_json(text) == VALID
