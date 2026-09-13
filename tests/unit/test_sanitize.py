"""`voicedock.notes.sanitize_filename` が SPEC §13.5（S-1〜S-9）と一致することを固定する。"""

from __future__ import annotations

import unicodedata

import pytest

from tests.spec_sync import spec_rule_ids
from voicedock.notes import FALLBACK_NAME, RESERVED_NAMES, sanitize_filename

MAX_BYTES = 180
"""`obsidian.max_title_bytes` の既定値（§7.2）。"""


def clean(name: str, *, max_bytes: int = MAX_BYTES) -> str:
    return sanitize_filename(name, max_bytes=max_bytes)


# --- SPEC 整合 -----------------------------------------------------------


def test_spec_has_nine_rules() -> None:
    """§13.5 が S-1〜S-9 の 9 件であること。

    **件数のずれが最も危ない**（#55 の振り返り）。SPEC に S-10 を足して実装を忘れると、
    **sanitize が足りないまま「完了」になる。**このテストがそこで落ちる。
    """
    rules = spec_rule_ids("13.5", "S")
    assert rules == [f"S-{n}" for n in range(1, 10)]


# --- S-1 NFC -------------------------------------------------------------


def test_s1_normalizes_to_nfc() -> None:
    """濁点を分解した形（NFD）を合成形（NFC）へ揃える。

    macOS のファイルシステムは NFD を返すため、**揃えないと「がぎぐ」と「がぎぐ」が
    別のファイル名になる。**
    """
    nfd = unicodedata.normalize("NFD", "がぎぐ")
    assert nfd != "がぎぐ"
    assert clean(nfd) == "がぎぐ"
    assert unicodedata.is_normalized("NFC", clean(nfd))


# --- S-2 制御文字 --------------------------------------------------------


@pytest.mark.parametrize("char", ["\x00", "\x01", "\x1f", "\x7f"])
def test_s2_removes_control_characters(char: str) -> None:
    assert clean(f"a{char}b") == "ab"


def test_s2_keeps_printable_characters() -> None:
    assert clean("a b") != "ab", "U+00A0 は制御文字ではない"


# --- S-3 置換 ------------------------------------------------------------


@pytest.mark.parametrize("char", list('/\\:*?"<>|'))
def test_s3_replaces_path_and_windows_characters(char: str) -> None:
    assert clean(f"a{char}b") == "a-b"


def test_s3_replaces_all_at_once() -> None:
    assert clean('a/b:c*d?e"f<g>h|i') == "a-b-c-d-e-f-g-h-i"


# --- S-4 除去 ------------------------------------------------------------


@pytest.mark.parametrize("char", ["#", "^", "[", "]"])
def test_s4_removes_obsidian_syntax_characters(char: str) -> None:
    """Obsidian のリンク・ブロック参照記法と衝突する文字を**除去**する（置換ではない）。"""
    assert clean(f"a{char}b") == "ab"


def test_s4_removes_wikilink_brackets() -> None:
    assert clean("[[Note]]") == "Note"


# --- S-5 空白 ------------------------------------------------------------


def test_s5_collapses_and_strips_whitespace() -> None:
    assert clean("  a   b  ") == "a b"


def test_s2_removes_tabs_before_s5_sees_them() -> None:
    """**タブと改行は S-2 が消す。**S-5 が見るのは空白（U+0020）だけである。

    タブは U+0009 で、S-2 の「制御文字（U+0000–U+001F）」に含まれる。したがって
    `a\tb` は `a b` ではなく **`ab`** になる（語が繋がる）。SPEC の規則をこの順で
    適用した結果であり、**タイトルに制御文字が入ること自体が異常**なので許容する。
    """
    assert clean("a\t\tb") == "ab"
    assert clean("a\nb") == "ab"
    assert clean("a  b") == "a b", "空白（U+0020）は S-5 が 1 個へ畳む"


def test_s3_runs_before_s5() -> None:
    """**順序に意味がある。**S-3（`/` → `-`）が S-5（空白の正規化）より先である。

    逆順だと `a / b` が `a - b` ではなく `a  -  b` のような形で残りうる。
    """
    assert clean("a / b") == "a - b"


# --- S-6 前後の `.` ------------------------------------------------------


def test_s6_strips_leading_and_trailing_dots() -> None:
    """前後の `.` を除去する。

    先頭の `.` は **macOS で隠しファイルになり Obsidian が拾わない**。
    末尾の `.` は拡張子の誤認を招く。
    """
    assert clean(".hidden.") == "hidden"
    assert clean("...a...") == "a"


def test_s6_keeps_inner_dots() -> None:
    assert clean("a.b.c") == "a.b.c"


# --- S-7 切り詰め（バイト基準） -----------------------------------------


def test_s7_truncates_by_utf8_bytes_not_characters() -> None:
    """**バイト数で切る。**日本語 80 文字 = 240 バイトは 180 を超える（§13.5）。"""
    assert len("あ" * 80) == 80
    assert len(("あ" * 80).encode("utf-8")) == 240

    result = clean("あ" * 80)
    assert len(result.encode("utf-8")) <= MAX_BYTES
    assert len(result) == 60, "3 バイト文字なので 60 文字に収まる"


def test_s7_keeps_short_names_intact() -> None:
    assert clean("short") == "short"


def test_s7_does_not_split_a_multibyte_character() -> None:
    """切り詰めた結果が UTF-8 として妥当であること。"""
    result = clean("あ" * 200, max_bytes=10)
    result.encode("utf-8").decode("utf-8")
    assert len(result.encode("utf-8")) <= 10


def test_s7_drops_an_orphaned_combining_mark() -> None:
    """**結合文字が末尾に残ったらさらに 1 つ削る**（§13.5）。

    `か` + U+3099（濁点）を途中で切ると**濁点だけが孤立する**。書記素クラスタ単位の
    分割は標準ライブラリでは実装できないため採用しない（§11.4）ので、
    「末尾が結合文字なら削る」で対処する。
    """
    # NFC で合成されない組み合わせ（ひらがな + 結合鋭アクセント）を使う
    text = "あ" * 3 + "́"
    assert unicodedata.combining(text[-1]) != 0
    result = clean(text, max_bytes=11)  # 'あ' 3 文字 = 9 バイト + 結合文字 2 バイト
    assert result == "あああ" or not unicodedata.combining(result[-1])


def test_s7_result_never_ends_with_a_combining_mark() -> None:
    for limit in range(1, 20):
        result = clean("が" * 5, max_bytes=limit)
        if result and result != FALLBACK_NAME:
            assert not unicodedata.combining(result[-1]), (limit, result)


# --- S-8 空 --------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "   ", "...", "###", "\x00\x01", "[[]]"])
def test_s8_falls_back_when_empty(name: str) -> None:
    assert clean(name) == FALLBACK_NAME


# --- S-9 Windows 予約名 --------------------------------------------------


@pytest.mark.parametrize("name", sorted(RESERVED_NAMES))
def test_s9_suffixes_reserved_names(name: str) -> None:
    assert clean(name) == f"{name}_"


@pytest.mark.parametrize("name", ["con", "Con", "cOm1", "lpt9"])
def test_s9_is_case_insensitive(name: str) -> None:
    """**大小文字を区別しない。**Windows の予約は大小を問わない。"""
    assert clean(name) == f"{name}_"


def test_s9_leaves_non_reserved_names() -> None:
    assert clean("CONSOLE") == "CONSOLE"
    assert clean("COM10") == "COM10", "COM10 は予約ではない（COM1〜COM9 のみ）"


def test_s9_runs_after_s7() -> None:
    """**S-9 は S-7 の後である。**先に `_` を付けると切り詰めで落ちうる。"""
    assert clean("CON", max_bytes=3) == "CON_"
    assert len(clean("CON", max_bytes=3)) == 4, "上限 3 でも `_` が残る"


def test_reserved_names_match_spec() -> None:
    """§13.5 が列挙する予約名と一致すること。"""
    expected = (
        {"CON", "PRN", "AUX", "NUL"}
        | {f"COM{n}" for n in range(1, 10)}
        | {f"LPT{n}" for n in range(1, 10)}
    )
    assert expected == RESERVED_NAMES
    assert len(RESERVED_NAMES) == 22


# --- 組み合わせ ---------------------------------------------------------


def test_emoji_survives() -> None:
    """絵文字で壊れないこと（LLM 生成タグに入りうる）。"""
    assert clean("会議 🎤 メモ") == "会議 🎤 メモ"


def test_a_realistic_llm_tag() -> None:
    """LLM が返しうる崩れたタグでも使える名前になること。

    **ここが崩れると W-5 / W-6 が落ち、LLM は同じタグを再生成するのでリトライも
    効かず、その日の Daily ノートが恒久的に作れなくなる**（§1.3 優先順位 2）。
    """
    result = clean(' #開発/設計: "VoiceDock" [メモ] ')
    assert result == "開発-設計- -VoiceDock- メモ"
    assert not set(result) & set('/\\:*?"<>|#^[]')
