"""`voicedock.raw` が SPEC §13.2 / §13.3 / §10.7 と一致することを固定する。

**このノートが「記録が Vault に残る」ことの実体である。**§1.3 の優先順位 1
（記録の保護）がここで満たされる。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import example_document, merge
from voicedock import notes, paths
from voicedock.config import RawConfig, parse_config
from voicedock.notes import NoteKind
from voicedock.raw import (
    RawPart,
    RawSegment,
    raw_filename,
    raw_folder,
    render_raw_note,
    render_template,
    write_raw_note,
)

JST = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 8, 29)
SESSION_KEY = "DJIMIC3:20260829"
KEY_A = "DJIMIC3/TX_MIC001_20260829_071201/TX01_MIC002_20260829_071204_orig.wav"
KEY_B = "DJIMIC3/TX_MIC001_20260829_074201/TX01_MIC002_20260829_074210_orig.wav"
MAX_BYTES = 180


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 29, hour, minute, second, tzinfo=JST)


def segment(start: datetime, text: str, *, seconds: int = 10) -> RawSegment:
    return RawSegment(at=start, end_at=start + timedelta(seconds=seconds), text=text)


def part_a() -> RawPart:
    return RawPart(
        partkey=KEY_A,
        started_at=at(7, 12, 4),
        ended_at=at(7, 42, 4),
        segments=(
            segment(at(7, 12, 4), "おはようございます。"),
            segment(at(7, 17, 4), "削除条件を整理します。"),
        ),
    )


def part_b() -> RawPart:
    return RawPart(
        partkey=KEY_B,
        started_at=at(7, 42, 10),
        ended_at=at(8, 12, 10),
        segments=(segment(at(7, 42, 10), "続きです。"),),
    )


def raw_config(**overrides: object) -> RawConfig:
    document = merge(example_document(), {"obsidian": {"raw": overrides}} if overrides else {})
    cfg, violations = parse_config(document)
    assert cfg is not None, violations
    return cfg.obsidian.raw


def render(parts: list[RawPart] | None = None, **overrides: object) -> str:
    return render_raw_note(
        parts if parts is not None else [part_a(), part_b()],
        day=DAY,
        session_key=SESSION_KEY,
        cfg=raw_config(**overrides),
    )


# --- frontmatter（§13.3 / §13.7） ---------------------------------------


def test_frontmatter_has_the_spec_fields() -> None:
    document = notes.parse_frontmatter(render())
    assert document is not None
    assert document["type"] == "voice-raw"
    assert document[notes.SESSION_KEY_FIELD] == SESSION_KEY
    assert document[notes.RECORDING_KEYS_FIELD] == [KEY_A, KEY_B]
    assert document["date"] == DAY.isoformat()
    assert document["parts"] == 2
    assert document["source"] == "DJI Mic 3"


def test_recording_keys_contain_every_part() -> None:
    """**R-6 の土台。**§14.1 はこの鍵の包含を削除の根拠にする。

    **この frontmatter を最初から必須出力にしないと、それ以前に作られたノートは
    削除判定で恒久的に偽になる**（安全側だが Phase 7 で backlog を消せない）。
    """
    document = notes.parse_frontmatter(render())
    assert document is not None
    assert set(document[notes.RECORDING_KEYS_FIELD]) == {KEY_A, KEY_B}


def test_does_not_emit_a_session_id() -> None:
    """**`voicedock_session_id` を出さない**（v5.1 で削除された。M-8）。

    Session に整数 ID は無い。出すと「どちらが正か」が曖昧になる。
    """
    assert "voicedock_session_id" not in render()


def test_keys_are_quoted_and_block_style() -> None:
    """`partkey` は `"` で囲み、1 行 1 要素にする（§13.3 の書式）。"""
    text = render()
    assert f'  - "{KEY_A}"' in text
    assert "[" not in text.split(notes.RECORDING_KEYS_FIELD)[1].split("date:")[0]


# --- 本文（§13.3） ------------------------------------------------------


def test_part_boundary_headings() -> None:
    text = render()
    assert "## 07:12–07:42" in text
    assert "## 07:42–08:12" in text


def test_part_boundary_can_be_disabled() -> None:
    text = render(part_boundary_heading=False)
    # `### ` は `## ` を含むので、**行頭の `## ` だけ**を見る
    assert re.search(r"^## ", text, re.M) is None
    assert re.search(r"^### ", text, re.M) is not None, "時刻見出しまで消えている"


def test_a_part_without_an_end_shows_only_the_start() -> None:
    """`duration_seconds` は ffprobe 失敗時に `NULL` のまま続行する（§10.5）。

    **終了時刻が無い Part は実際に存在する。**そこで落ちてはならない。
    """
    part = RawPart(
        partkey=KEY_A,
        started_at=at(7, 12, 4),
        ended_at=None,
        segments=(segment(at(7, 12, 4), "x"),),
    )
    assert "## 07:12–\n" in render([part])


def test_timestamp_headings_are_absolute() -> None:
    """`###` は**絶対時刻**（§13.3）。

    終日録音では「録音開始からの経過秒」より実時刻のほうが振り返りに使える。
    """
    text = render()
    assert "### 07:12:04" in text
    assert "### 07:17:04" in text


def test_timestamp_interval_is_respected() -> None:
    """`timestamp_interval_seconds` ごとに 1 つだけ見出しを出す。"""
    part = RawPart(
        partkey=KEY_A,
        started_at=at(7, 0),
        ended_at=at(7, 20),
        segments=tuple(segment(at(7, minute), f"{minute} 分") for minute in range(0, 20, 2)),
    )
    headings = re.findall(r"^### (\d\d:\d\d:\d\d)$", render([part]), re.M)
    # 300 秒 = 5 分ごと → 07:00 / 07:06 / 07:12 / 07:18（2 分刻みの最初の超過点）
    assert headings == ["07:00:00", "07:06:00", "07:12:00", "07:18:00"]


def test_timestamp_headings_can_be_disabled() -> None:
    """**`0` で `###` が消える**（§13.3）。"""
    text = render(timestamp_interval_seconds=0)
    assert "### " not in text
    assert "おはようございます。" in text


def test_body_is_not_processed_by_an_llm() -> None:
    """**本文は whisper の出力そのまま**（§13.3）。

    ここで手を入れると「生データ」でなくなり、**あとから元の発言を確かめる術が
    無くなる。**整形・要約は Daily ノート（§13.4）の仕事である。
    """
    odd = "  えーと、あの… 「テスト」だ。  "
    part = RawPart(
        partkey=KEY_A,
        started_at=at(7, 0),
        ended_at=at(7, 1),
        segments=(segment(at(7, 0), odd),),
    )
    assert odd.strip() in render([part])


def test_empty_segments_are_dropped() -> None:
    part = RawPart(
        partkey=KEY_A,
        started_at=at(7, 0),
        ended_at=at(7, 1),
        segments=(segment(at(7, 0), "   "), segment(at(7, 0, 30), "本文")),
    )
    text = render([part])
    assert "本文" in text
    assert text.count("### ") == 1, "空の区間で見出しを出している"


def test_a_body_with_a_leading_dashes_line_is_escaped() -> None:
    """**本文の行頭 `---` を退避する**（§13.4）。

    退避しないと境界判定が本文の途中で終端を見つけ、**frontmatter の範囲を誤認する。**
    whisper が `---` だけの行を返すことは実際にある。
    """
    part = RawPart(
        partkey=KEY_A,
        started_at=at(7, 0),
        ended_at=at(7, 1),
        segments=(segment(at(7, 0), "---"),),
    )
    text = render([part])
    assert "\\---" in text
    document = notes.parse_frontmatter(text)
    assert document is not None, "frontmatter の範囲が壊れている"
    assert document[notes.SESSION_KEY_FIELD] == SESSION_KEY


# --- 決定性（§10.7） ----------------------------------------------------


def test_rendering_is_deterministic() -> None:
    """**同じ入力から必ず同じ出力になる。**

    1 日 32 回書き直されるので（§10.7）、揺れると Obsidian の同期が毎回差分を運ぶ。
    """
    assert render() == render()


def test_parts_are_sorted_by_start_time() -> None:
    """**呼び手の順序に依存しない。**依存すると再生成のたびに本文が入れ替わる。"""
    assert render([part_a(), part_b()]) == render([part_b(), part_a()])
    text = render([part_b(), part_a()])
    assert text.index("07:12") < text.index("07:42")


def test_no_parts_still_renders() -> None:
    """Part が 0 件でも壊れないこと（`session_empty` の経路。§16.4）。"""
    text = render([])
    document = notes.parse_frontmatter(text)
    assert document is not None
    assert document["parts"] == 0
    assert document[notes.RECORDING_KEYS_FIELD] == []


# --- パスとファイル名（§13.2 / §13.5） ---------------------------------


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("Daily/Voice/Raw/{yyyymmdd}", "Daily/Voice/Raw/20260829"),
        ("{date}", "2026-08-29"),
        ("x/{time}", "x/000000"),
        ("a/{yyyymmdd}/{date}", "a/20260829/2026-08-29"),
    ],
)
def test_template_placeholders(template: str, expected: str) -> None:
    assert render_template(template, DAY) == expected


def test_an_unknown_placeholder_is_left_alone(tmp_path: Path) -> None:
    """**`render_template()` は埋めるだけである。**未知のものは V-13 が起動時に弾く。

    `{part}` は v5.48 で許可リストから外した（変更 BJ-1）。
    **空文字へ展開する実装を残さない** —— 残すと `{date} raw {part}` が
    「末尾に空白の付いた 1 本のノート」を静かに作る。
    """
    assert render_template("{date} raw {part}", DAY) == "2026-08-29 raw {part}"


def test_folder_follows_the_spec_layout(tmp_path: Path) -> None:
    """§13.2 のレイアウトになること。"""
    folder = raw_folder(tmp_path, raw_config(), DAY)
    assert Path(folder) == tmp_path / "Daily" / "Voice" / "Raw" / "20260829"


def test_filename_is_sanitized() -> None:
    """ファイル名は **`notes.sanitize_filename()` を必ず通す**（§13.5）。"""
    assert raw_filename(raw_config(), DAY, max_bytes=MAX_BYTES) == "2026-08-29 raw"


def test_filename_sanitizes_a_hostile_template() -> None:
    cfg = raw_config(filename_template="{date}:raw")
    assert raw_filename(cfg, DAY, max_bytes=MAX_BYTES) == "2026-08-29-raw"


# --- 保存（§13.6 → §13.7） ---------------------------------------------


def write(tmp_path: Path, parts: list[RawPart] | None = None, **overrides: object):  # type: ignore[no-untyped-def]
    return write_raw_note(
        parts if parts is not None else [part_a(), part_b()],
        day=DAY,
        session_key=SESSION_KEY,
        cfg=raw_config(**overrides),
        vault_root=tmp_path,
        max_title_bytes=MAX_BYTES,
    )


@pytest.fixture(autouse=True)
def _vault_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`tmp_path` を Vault のマウント点として扱う（`safe_unlink_tmp` が要求する）。"""
    monkeypatch.setattr(paths, "VAULT_ROOT", tmp_path)


def test_write_creates_the_note_and_passes_verification(tmp_path: Path) -> None:
    """**R-1〜R-6 をすべて満たすこと。**

    **これが真になって初めて、その Part の音声を削除してよい状態になる**（§14.1）。
    """
    result = write(tmp_path)
    assert result.ok, result.failed_rules
    assert Path(result.path).is_file()
    assert Path(result.path).name == "2026-08-29 raw.md"
    assert Path(result.path).parent == tmp_path / "Daily" / "Voice" / "Raw" / "20260829"


def test_write_returns_the_sha_of_what_was_written(tmp_path: Path) -> None:
    result = write(tmp_path)
    assert result.sha256 == notes.sha256_bytes(Path(result.path).read_bytes())


def test_regenerating_updates_the_same_file(tmp_path: Path) -> None:
    """**2 本目の Part が終わっても同じファイルを更新し、ファイルが増えない**（§10.7）。"""
    first = write(tmp_path, [part_a()])
    second = write(tmp_path, [part_a(), part_b()])
    assert Path(first.path) == Path(second.path)
    folder = tmp_path / "Daily" / "Voice" / "Raw" / "20260829"
    assert [p.name for p in sorted(folder.iterdir())] == ["2026-08-29 raw.md"]
    assert KEY_B in Path(second.path).read_text(encoding="utf-8")


def test_no_temporary_file_remains(tmp_path: Path) -> None:
    """**`.2026-08-29 raw.md.tmp` が残っていないこと**（§13.6）。"""
    result = write(tmp_path)
    folder = Path(result.path).parent
    assert not any(p.name.startswith(".") for p in folder.iterdir())


def test_a_different_session_does_not_overwrite(tmp_path: Path) -> None:
    """別 Session の同名ノートを上書きしない（§13.5 の重複時）。"""
    write(tmp_path)
    other = write_raw_note(
        [part_a()],
        day=DAY,
        session_key="OTHER:20260829",
        cfg=raw_config(),
        vault_root=tmp_path,
        max_title_bytes=MAX_BYTES,
    )
    assert Path(other.path).name == "2026-08-29 raw (2).md"
    assert other.ok, other.failed_rules


def test_verification_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """検証の失敗は**例外ではなく結果で返す**（呼び手が `OBSIDIAN_RAW_VERIFY_FAILED` へ写す）。"""
    real = notes.verify_note

    def tampered(path: Path, **kwargs: object):  # type: ignore[no-untyped-def]
        kwargs["expected_sha"] = "0" * 64
        return real(path, **kwargs)  # type: ignore[arg-type]

    # `raw.py` は `from voicedock import notes` なので、`notes` を差し替えれば届く
    monkeypatch.setattr(notes, "verify_note", tampered)
    result = write(tmp_path)
    assert not result.ok
    assert "R-4" in result.failed_rules


def test_written_note_is_verifiable_by_cleaner(tmp_path: Path) -> None:
    """**#36 の `cleaner.py` が読む経路**で検証できること（§14.1）。

    `frontmatter_keys()` が `partkey` を返し、`verify_note()` が R-1〜R-6 を通る。
    **この 2 つが削除の根拠そのものである。**
    """
    result = write(tmp_path)
    assert notes.frontmatter_keys(Path(result.path)) == (KEY_A, KEY_B)
    again = notes.verify_note(
        Path(result.path),
        kind=NoteKind.RAW,
        session_key=SESSION_KEY,
        expected_sha=result.sha256,
        expected_keys=[KEY_A],
    )
    assert notes.all_passed(again), "R-6 は包含なので 1 件でも通る"


# --- granularity は v5.48 で廃止（変更 BJ-1） ---------------------------


def test_the_part_placeholder_is_rejected(tmp_path: Path) -> None:
    """**`{part}` は未知のプレースホルダである**（V-13 / 変更 BJ-1）。

    v5.47 まで `granularity: part` 用に受理していたが、
    **`pipeline.ensure_raw_note()` は Part ごとの書き手を一度も呼んでいなかった。**
    設定は V-15 / V-27 で検証までされたうえで**黙って無視され**、
    `{part}` は空文字に展開されて**末尾に空白の付いた 1 本のノート**になっていた。

    設定ごと廃止したので、`{part}` を書いたら**起動時に落ちる。**
    """
    patch = {"obsidian": {"raw": {"filename_template": "{date} raw {part}"}}}
    document = merge(example_document(), patch)
    cfg, violations = parse_config(document)
    assert cfg is None, "{part} が通ってしまった"
    assert any(v.rule == "V-13" for v in violations), violations


def test_granularity_is_no_longer_a_key(tmp_path: Path) -> None:
    """**設定キーごと消えている**（V-1 が未知キーを弾く。変更 BJ-1）。"""
    document = merge(example_document(), {"obsidian": {"raw": {"granularity": "part"}}})
    cfg, violations = parse_config(document)
    assert cfg is None, "granularity がまだ受理されている"
    assert any(v.rule == "V-1" for v in violations), violations


# --- 段落のまとまり（§13.3 / 変更 BM-1） --------------------------------


def dense_part() -> RawPart:
    """**同じ見出しの下に入る 3 セグメント**。whisper は発話の切れ目で切る。"""
    return RawPart(
        partkey=KEY_A,
        started_at=at(7, 12, 4),
        ended_at=at(7, 42, 4),
        segments=(
            segment(at(7, 12, 4), "まあ、そこをどこまで個人商店に近づけるかなって、"),
            segment(at(7, 12, 9), "やれば、まだ大丈夫っていう感じです。"),
            segment(at(7, 13, 2), "ハーネス整備が追いついていないかなって。"),
        ),
    )


def test_segments_under_one_heading_form_a_single_paragraph() -> None:
    """**`###` の見出しごとに 1 段落にまとめる**（§13.3 / 変更 BM-1）。

    v5.50 までは `lines += [text, ""]` で**セグメント 1 つごとに空行**を入れていた。
    whisper は発話の切れ目で切るので、**1 発話 = 1 段落**になり、
    Obsidian で読むと 1 文ずつ離れて**文脈が切れる**（実機で指摘された）。

    **セグメント境界は失われない。**`/data/transcripts/parts/*.json` に
    `retain_transcript_days: 0`（無期限）で残る。
    """
    body = render([dense_part()], timestamp_interval_seconds=300)

    assert (
        "まあ、そこをどこまで個人商店に近づけるかなって、 "
        "やれば、まだ大丈夫っていう感じです。 "
        "ハーネス整備が追いついていないかなって。"
    ) in body, body

    # **空行で割れていないこと。**割れていると 1 発話 1 段落に戻っている
    assert "やれば、まだ大丈夫っていう感じです。\n\n" not in body, body


def test_a_new_timestamp_heading_starts_a_new_paragraph() -> None:
    """**見出しをまたいだら段落を切る。**全部を 1 段落にしてしまわないこと。"""
    body = render([part_a()], timestamp_interval_seconds=60)

    expected = "### 07:12:04\n\nおはようございます。\n\n### 07:17:04\n\n削除条件を整理します。"
    assert expected in body, body


def test_the_whole_part_is_one_paragraph_without_headings() -> None:
    """**`timestamp_interval_seconds: 0` なら Part 全体が 1 段落**（§13.3 の `0` で無効）。"""
    body = render([dense_part()], timestamp_interval_seconds=0)

    assert "###" not in body
    assert body.count("ハーネス整備") == 1
    assert "近づけるかなって、 やれば、" in body, body
