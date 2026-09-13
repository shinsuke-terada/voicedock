"""`voicedock.notes` の frontmatter と保存検証が SPEC §13.7 と一致することを固定する。

**この検証が §14.1 の削除条件の土台である。**「テキストが Vault に確実に残っていること」
だけが元音声を消してよい根拠であり、**ここが緩いと削除事故になる。**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.spec_sync import spec_rule_ids
from voicedock.notes import (
    RECORDING_KEYS_FIELD,
    SESSION_KEY_FIELD,
    CheckResult,
    NoteKind,
    all_passed,
    escape_body,
    failed_rules,
    frontmatter_keys,
    parse_frontmatter,
    render_frontmatter,
    resolve_output_path,
    sha256_bytes,
    split_frontmatter,
    verify_note,
    yaml_quote,
)
from voicedock.paths import VaultPath

SESSION_KEY = "DJIMIC3:20260829"
KEY_A = "DJIMIC3/TX_MIC001_20260829_071201/TX01_MIC002_20260829_071204_orig.wav"
KEY_B = "DJIMIC3/TX_MIC001_20260829_074201/TX01_MIC002_20260829_074210_orig.wav"


def build_note(
    *,
    session_key: str = SESSION_KEY,
    keys: tuple[str, ...] = (KEY_A, KEY_B),
    body: str = "\n# 2026-08-29\n\n## Summary\n\n打ち合わせをした。\n\n[[2026-08-29 raw]]\n",
    extra: dict[str, object] | None = None,
) -> str:
    fields: dict[str, object] = {
        "type": "voice-wiki",
        SESSION_KEY_FIELD: session_key,
        RECORDING_KEYS_FIELD: list(keys),
    }
    fields.update(extra or {})
    return render_frontmatter(fields) + body


def write(tmp_path: Path, text: str, name: str = "note.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def check(path: Path, kind: NoteKind, **overrides: object) -> dict[str, CheckResult]:
    text = path.read_bytes() if path.exists() else b""
    kwargs: dict[str, object] = {
        "kind": kind,
        "session_key": SESSION_KEY,
        "expected_sha": sha256_bytes(text),
        "expected_keys": (KEY_A, KEY_B),
        "require_raw_link": kind is NoteKind.DAILY,
    }
    kwargs.update(overrides)
    return {r.rule: r for r in verify_note(path, **kwargs)}  # type: ignore[arg-type]


# --- SPEC 整合 -----------------------------------------------------------


def test_spec_rule_counts(tmp_path: Path) -> None:
    """§13.7 の R-* / W-* と実装が 1 対 1 であること。

    **件数のずれが最も危ない**（#55 の振り返り）。SPEC に W-10 を足して実装を忘れると、
    **検証が足りないまま `SAVED` になり、Phase 7 で削除の入力になる。**
    """
    assert spec_rule_ids("13.7", "R") == [f"R-{n}" for n in range(1, 7)]
    assert spec_rule_ids("13.7", "W") == [f"W-{n}" for n in range(1, 10)]

    raw = write(tmp_path, build_note(), "raw.md")
    daily = write(tmp_path, build_note(), "daily.md")
    assert sorted(check(raw, NoteKind.RAW)) == sorted(spec_rule_ids("13.7", "R"))
    assert sorted(check(daily, NoteKind.DAILY)) == sorted(spec_rule_ids("13.7", "W"))


# --- frontmatter の書き出し ---------------------------------------------


def test_string_values_are_always_quoted() -> None:
    """**文字列値は必ず二重引用符で囲む**（§13.4）。

    `yaml.dump` は値によって引用を省く。`session_key` は `:` を含み、LLM 生成タグは
    `#` や `:` を含みうるので、**引用を省くと frontmatter が壊れる。**
    """
    text = render_frontmatter({"a": "plain", SESSION_KEY_FIELD: SESSION_KEY})
    assert 'a: "plain"' in text
    assert f'{SESSION_KEY_FIELD}: "{SESSION_KEY}"' in text


@pytest.mark.parametrize(
    "value",
    ['say "hi"', "back\\slash", "colon: here", "#hash", "- dash", "[bracket]", "{brace}", "@at"],
)
def test_tricky_values_round_trip(value: str) -> None:
    """LLM が返しうる値が**書いて読んで元に戻る**こと。

    **ここが崩れると W-5 / W-6 が落ち、LLM は同じタグを再生成するのでリトライも
    効かず、その日の Daily ノートが恒久的に作れなくなる**（§1.3 優先順位 2）。
    """
    text = render_frontmatter({"tag": value})
    document = parse_frontmatter(text + "body\n")
    assert document is not None
    assert document["tag"] == value


def test_control_characters_are_stripped_from_values() -> None:
    """制御文字は値から落とす（YAML を壊すため）。"""
    assert yaml_quote("a\x00b\x1fc") == '"abc"'


def test_lists_are_block_style() -> None:
    """リストは 1 行 1 要素にする（§13.3）。

    `voicedock_recording_keys` は 32 Part の日で約 2.4 KB になり、
    フロー形式（`[...]`）では editor で読めない。
    """
    text = render_frontmatter({RECORDING_KEYS_FIELD: [KEY_A, KEY_B]})
    assert f"{RECORDING_KEYS_FIELD}:\n" in text
    assert f'  - "{KEY_A}"\n' in text
    assert "[" not in text.split(RECORDING_KEYS_FIELD)[1]


def test_scalars_are_rendered_natively() -> None:
    text = render_frontmatter({"n": 3, "f": 1.5, "b": True, "x": None})
    assert "n: 3" in text
    assert "f: 1.5" in text
    assert "b: true" in text
    assert "x: null" in text


def test_round_trips_through_yaml() -> None:
    document = parse_frontmatter(build_note())
    assert document is not None
    assert document[SESSION_KEY_FIELD] == SESSION_KEY
    assert document[RECORDING_KEYS_FIELD] == [KEY_A, KEY_B]


# --- frontmatter の読み取り ---------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["", "no frontmatter\n", "---\nunterminated\n", "--\na: 1\n--\n", " ---\na: 1\n---\n"],
)
def test_malformed_frontmatter_returns_none(text: str) -> None:
    """**例外を投げない。**外部から改竄されたノートを読む可能性がある。"""
    assert parse_frontmatter(text) is None


def test_broken_yaml_returns_none() -> None:
    assert parse_frontmatter("---\na: [unclosed\n---\nbody\n") is None


def test_non_mapping_frontmatter_returns_none() -> None:
    assert parse_frontmatter("---\n- a\n- b\n---\nbody\n") is None


def test_frontmatter_keys_reads_the_field(tmp_path: Path) -> None:
    """**#36 の削除条件がこれを呼ぶ**（§14.1）。"""
    path = write(tmp_path, build_note())
    assert frontmatter_keys(path) == (KEY_A, KEY_B)


@pytest.mark.parametrize("text", ["broken", "---\na: 1\n---\nbody\n", "---\n{}\n---\n"])
def test_frontmatter_keys_is_empty_when_unreadable(tmp_path: Path, text: str) -> None:
    """**読めないときは空を返す。**空は「削除の根拠が無い」= 安全側である（§14.1）。"""
    assert frontmatter_keys(write(tmp_path, text)) == ()


def test_frontmatter_keys_on_a_missing_file(tmp_path: Path) -> None:
    assert frontmatter_keys(tmp_path / "nope.md") == ()


# --- 本文の退避 ---------------------------------------------------------


def test_escape_body_protects_the_frontmatter_boundary() -> None:
    """**行頭 `---` を `\\---` へ退避する**（§13.4）。

    退避しないと、**W-5 の「対応する終端 `---`」が本文の途中で見つかり**、
    frontmatter の範囲を誤認する。
    """
    assert escape_body("a\n---\nb\n") == "a\n\\---\nb\n"
    assert escape_body("---\n") == "\\---\n"


def test_escape_body_leaves_inline_dashes() -> None:
    assert escape_body("a --- b\n") == "a --- b\n"


def test_an_unescaped_body_breaks_the_boundary(tmp_path: Path) -> None:
    """退避しない本文が実際に W-6 を壊すことを示す。"""
    text = render_frontmatter({SESSION_KEY_FIELD: SESSION_KEY}) + "body\n---\nmore\n"
    parts = split_frontmatter(text)
    assert parts is not None
    assert "body" not in parts[0], "本文が frontmatter に入っていない"

    # 退避すれば本文の `---` は境界にならない
    safe = render_frontmatter({SESSION_KEY_FIELD: SESSION_KEY}) + escape_body("body\n---\nmore\n")
    document = parse_frontmatter(safe)
    assert document is not None
    assert document[SESSION_KEY_FIELD] == SESSION_KEY


# --- R-1〜R-6 / W-1〜W-4（共通） ----------------------------------------


@pytest.mark.parametrize("kind", [NoteKind.RAW, NoteKind.DAILY])
def test_a_valid_note_passes_everything(tmp_path: Path, kind: NoteKind) -> None:
    results = check(write(tmp_path, build_note()), kind)
    assert all_passed(results.values()), failed_rules(results.values())


@pytest.mark.parametrize("kind", [NoteKind.RAW, NoteKind.DAILY])
def test_rule_1_missing_file(tmp_path: Path, kind: NoteKind) -> None:
    results = check(tmp_path / "nope.md", kind)
    prefix = "R" if kind is NoteKind.RAW else "W"
    assert not results[f"{prefix}-1"].ok
    assert len(results) == 1, "存在しないなら以降を評価しない"


@pytest.mark.parametrize("kind", [NoteKind.RAW, NoteKind.DAILY])
def test_rule_1_symlink_is_rejected(tmp_path: Path, kind: NoteKind) -> None:
    """**symlink を通常ファイルとして受け付けない。**

    Vault 内の symlink 越しに「保存できたことにする」経路を作らない。
    """
    real = write(tmp_path, build_note(), "real.md")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    prefix = "R" if kind is NoteKind.RAW else "W"
    assert not check(link, kind)[f"{prefix}-1"].ok


@pytest.mark.parametrize("kind", [NoteKind.RAW, NoteKind.DAILY])
def test_rule_2_empty_file(tmp_path: Path, kind: NoteKind) -> None:
    results = check(write(tmp_path, ""), kind)
    prefix = "R" if kind is NoteKind.RAW else "W"
    assert not results[f"{prefix}-2"].ok
    assert len(results) == 2


@pytest.mark.parametrize("kind", [NoteKind.RAW, NoteKind.DAILY])
def test_rule_3_invalid_utf8(tmp_path: Path, kind: NoteKind) -> None:
    path = tmp_path / "note.md"
    path.write_bytes(b"\xff\xfe invalid")
    prefix = "R" if kind is NoteKind.RAW else "W"
    results = check(path, kind)
    assert not results[f"{prefix}-3"].ok
    assert len(results) == 3


@pytest.mark.parametrize("kind", [NoteKind.RAW, NoteKind.DAILY])
def test_rule_4_sha_mismatch(tmp_path: Path, kind: NoteKind) -> None:
    """**保存後に改竄されたら落ちる**（ND-16 の土台）。"""
    path = write(tmp_path, build_note())
    prefix = "R" if kind is NoteKind.RAW else "W"
    results = check(path, kind, expected_sha="0" * 64)
    assert not results[f"{prefix}-4"].ok
    assert results[f"{prefix}-3"].ok, "SHA が違っても残りは評価する"


# --- W-5（Daily のみ） ---------------------------------------------------


def test_w5_requires_a_frontmatter_block(tmp_path: Path) -> None:
    results = check(write(tmp_path, "no frontmatter\n"), NoteKind.DAILY)
    assert not results["W-5"].ok


def test_w5_requires_a_closing_delimiter(tmp_path: Path) -> None:
    results = check(write(tmp_path, "---\na: 1\nbody\n"), NoteKind.DAILY)
    assert not results["W-5"].ok


def test_raw_has_no_w5(tmp_path: Path) -> None:
    """§13.7 の R-* に W-5 相当は無い。**規則を勝手に増やさない。**"""
    assert "R-5" in check(write(tmp_path, build_note()), NoteKind.RAW)
    assert len(check(write(tmp_path, build_note()), NoteKind.RAW)) == 6


# --- R-5 / W-6 session_key ----------------------------------------------


@pytest.mark.parametrize(("kind", "rule"), [(NoteKind.RAW, "R-5"), (NoteKind.DAILY, "W-6")])
def test_session_key_mismatch(tmp_path: Path, kind: NoteKind, rule: str) -> None:
    path = write(tmp_path, build_note(session_key="DJIMIC3:20260830"))
    assert not check(path, kind)[rule].ok


@pytest.mark.parametrize(("kind", "rule"), [(NoteKind.RAW, "R-5"), (NoteKind.DAILY, "W-6")])
def test_session_key_missing(tmp_path: Path, kind: NoteKind, rule: str) -> None:
    text = render_frontmatter({"type": "voice-wiki"}) + "## Summary\n\nx\n[[raw]]\n"
    assert not check(write(tmp_path, text), kind)[rule].ok


@pytest.mark.parametrize("kind", [NoteKind.RAW, NoteKind.DAILY])
def test_unparseable_frontmatter_still_reports_every_rule(tmp_path: Path, kind: NoteKind) -> None:
    """frontmatter が壊れていても**規則の件数は変わらない**（何が欠けたか全部出す）。"""
    path = write(tmp_path, "---\na: [unclosed\n---\nbody\n")
    results = check(path, kind)
    expected = 6 if kind is NoteKind.RAW else 9
    assert len(results) == expected


# --- R-6（包含） / W-7（完全一致） --------------------------------------


def test_r6_requires_containment_only(tmp_path: Path) -> None:
    """**R-6 は包含である。**Raw ノートは Part ごとに再生成されるので、
    途中経過では期待より多く載りうる（§10.7）。
    """
    path = write(tmp_path, build_note(keys=(KEY_A, KEY_B, "DJIMIC3/extra/x_orig.wav")))
    assert check(path, NoteKind.RAW)["R-6"].ok


def test_r6_fails_when_a_key_is_missing(tmp_path: Path) -> None:
    path = write(tmp_path, build_note(keys=(KEY_A,)))
    result = check(path, NoteKind.RAW)["R-6"]
    assert not result.ok
    assert KEY_B in result.detail


def test_w7_requires_exact_equality(tmp_path: Path) -> None:
    """**W-7 は完全一致である。**Daily は統合対象そのものなので、余分が載ってはならない。

    **R-6 と条件が違う。混同すると「統合していない Part の音声」を消す根拠になる。**
    """
    path = write(tmp_path, build_note(keys=(KEY_A, KEY_B, "DJIMIC3/extra/x_orig.wav")))
    result = check(path, NoteKind.DAILY)["W-7"]
    assert not result.ok
    assert "extra" in result.detail


def test_w7_fails_when_a_key_is_missing(tmp_path: Path) -> None:
    path = write(tmp_path, build_note(keys=(KEY_A,)))
    assert not check(path, NoteKind.DAILY)["W-7"].ok


@pytest.mark.parametrize(("kind", "rule"), [(NoteKind.RAW, "R-6"), (NoteKind.DAILY, "W-7")])
def test_missing_keys_field(tmp_path: Path, kind: NoteKind, rule: str) -> None:
    text = render_frontmatter({SESSION_KEY_FIELD: SESSION_KEY}) + "## Summary\n\nx\n\n[[raw]]\n"
    assert not check(write(tmp_path, text), kind)[rule].ok


def test_key_order_does_not_matter(tmp_path: Path) -> None:
    """鍵は集合として比べる（並び順で落とさない）。"""
    path = write(tmp_path, build_note(keys=(KEY_B, KEY_A)))
    assert check(path, NoteKind.DAILY)["W-7"].ok


# --- W-8 Summary ---------------------------------------------------------


def test_w8_requires_content_under_the_heading(tmp_path: Path) -> None:
    """**空の `## Summary` を通さない。**

    通すと **LLM が何も返さなかった日のノートが「保存成功」になり**、
    その日の音声が削除対象になる。
    """
    body = "\n# 2026-08-29\n\n## Summary\n\n## Tasks\n\n- x\n\n[[raw]]\n"
    assert not check(write(tmp_path, build_note(body=body)), NoteKind.DAILY)["W-8"].ok


def test_w8_fails_without_the_heading(tmp_path: Path) -> None:
    body = "\n# 2026-08-29\n\n本文だけ\n\n[[raw]]\n"
    assert not check(write(tmp_path, build_note(body=body)), NoteKind.DAILY)["W-8"].ok


def test_w8_uses_the_configured_heading(tmp_path: Path) -> None:
    """見出しは `sections.summary.heading` から来る（§7.2）。"""
    body = "\n## まとめ\n\n打ち合わせをした。\n\n[[raw]]\n"
    results = check(
        write(tmp_path, build_note(body=body)), NoteKind.DAILY, summary_heading="## まとめ"
    )
    assert results["W-8"].ok


def test_w8_stops_at_the_next_heading(tmp_path: Path) -> None:
    body = "\n## Summary\n\n中身\n\n## Tasks\n\n- x\n\n[[raw]]\n"
    assert check(write(tmp_path, build_note(body=body)), NoteKind.DAILY)["W-8"].ok


# --- W-9 Raw へのリンク --------------------------------------------------


def test_w9_requires_a_link_when_transcript_is_excluded(tmp_path: Path) -> None:
    """`wiki.include_transcript` が false なら Raw へのリンクが要る（§13.7）。

    本文を Daily に入れないなら、**元の文字起こしへ辿れる経路が無いと記録が迷子になる。**
    """
    body = "\n## Summary\n\n打ち合わせをした。\n"
    results = check(write(tmp_path, build_note(body=body)), NoteKind.DAILY, require_raw_link=True)
    assert not results["W-9"].ok


def test_w9_is_satisfied_by_a_wikilink(tmp_path: Path) -> None:
    body = "\n## Summary\n\nx\n\n[[2026-08-29 raw]]\n"
    results = check(write(tmp_path, build_note(body=body)), NoteKind.DAILY, require_raw_link=True)
    assert results["W-9"].ok


def test_w9_passes_when_not_required(tmp_path: Path) -> None:
    """`include_transcript` が true ならリンクは要らない。"""
    body = "\n## Summary\n\nx\n"
    results = check(write(tmp_path, build_note(body=body)), NoteKind.DAILY, require_raw_link=False)
    assert results["W-9"].ok


# --- 補助 ----------------------------------------------------------------


def test_failed_rules_lists_the_ids(tmp_path: Path) -> None:
    """落ちた規則の ID を返すこと（`OBSIDIAN_VERIFY_FAILED` の detail に載せる）。"""
    path = write(tmp_path, build_note(session_key="other", keys=(KEY_A,)))
    results = check(path, NoteKind.DAILY)
    assert failed_rules(results.values()) == ("W-6", "W-7")
    assert not all_passed(results.values())


def test_every_rule_is_evaluated_after_a_failure(tmp_path: Path) -> None:
    """**1 つ落ちても残りを評価する。**最初の失敗で打ち切ると直す箇所が 1 件ずつしか出ない。"""
    path = write(tmp_path, build_note(session_key="other", keys=(KEY_A,)))
    assert len(check(path, NoteKind.DAILY)) == 9


# --- §13.5 の重複時 ------------------------------------------------------


def test_resolve_returns_the_plain_path_when_absent(tmp_path: Path) -> None:
    assert resolve_output_path(VaultPath(tmp_path), "2026-08-29 Voice", SESSION_KEY) == (
        tmp_path / "2026-08-29 Voice.md"
    )


def test_resolve_overwrites_the_same_session(tmp_path: Path) -> None:
    """**`voicedock_session_key` が一致すれば上書き対象**（再生成の正常動作。§13.5）。"""
    write(tmp_path, build_note(), "2026-08-29 Voice.md")
    assert resolve_output_path(VaultPath(tmp_path), "2026-08-29 Voice", SESSION_KEY) == (
        tmp_path / "2026-08-29 Voice.md"
    )


def test_resolve_numbers_a_different_session(tmp_path: Path) -> None:
    """別の Session のノートは**上書きしない**。"""
    write(tmp_path, build_note(session_key="OTHER:20260829"), "2026-08-29 Voice.md")
    assert resolve_output_path(VaultPath(tmp_path), "2026-08-29 Voice", SESSION_KEY) == (
        tmp_path / "2026-08-29 Voice (2).md"
    )


def test_resolve_skips_multiple_collisions(tmp_path: Path) -> None:
    for name in ("2026-08-29 Voice.md", "2026-08-29 Voice (2).md"):
        write(tmp_path, build_note(session_key="OTHER:20260829"), name)
    assert resolve_output_path(VaultPath(tmp_path), "2026-08-29 Voice", SESSION_KEY) == (
        tmp_path / "2026-08-29 Voice (3).md"
    )


def test_resolve_treats_an_unreadable_note_as_different(tmp_path: Path) -> None:
    """**読めないファイルは「別物」として扱う**（不明は安全側 = 上書きしない）。"""
    (tmp_path / "2026-08-29 Voice.md").write_bytes(b"\xff\xfe")
    assert resolve_output_path(VaultPath(tmp_path), "2026-08-29 Voice", SESSION_KEY) == (
        tmp_path / "2026-08-29 Voice (2).md"
    )


def test_an_empty_list_is_rendered_as_brackets() -> None:
    """**空リストは `[]` と書く。**

    `key:` だけだと YAML が `null` と読み、**「鍵が 0 件」と「鍵の欄が無い」が
    区別できなくなる。**Part を 0 件持つ Session は実在する（`session_empty`。§16.4）。
    """
    text = render_frontmatter({RECORDING_KEYS_FIELD: []})
    assert f"{RECORDING_KEYS_FIELD}: []" in text
    document = parse_frontmatter(text + "body\n")
    assert document is not None
    assert document[RECORDING_KEYS_FIELD] == []
