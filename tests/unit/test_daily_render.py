"""Daily ノートのレンダリングを固定する（SPEC §13.4）。

**セクションの有無と順序は `llm.analysis.order` と `sections.<key>.enabled` に従う。**
#25 の「真実の出所を config に一本化する」がここまで貫かれる — **切ったセクションは
プロンプトにも載らず、スキーマにも入らず、ノートにも出ない。**

**欠落を隠さない**（§13.4）。処理できなかった Part は frontmatter に列挙し、本文へ
警告行を出す。**除外された Part の音声は削除されない** — §14.1 が
`voicedock_recording_keys` への収録を要求するため自動的に保証される。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from voicedock import daily, notes, wiki
from voicedock.config import Config
from voicedock.daily import TimelineBlock, build_timeline, render_daily_note
from voicedock.llm import Chunk, build_schema
from voicedock.paths import PartKey, SessionKey
from voicedock.session import AbsoluteSegment, SessionTranscript

FINGERPRINT = "FP"
JST = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 8, 29)
SESSION_KEY = SessionKey("DJIMIC3:20260829")
KEYS = [
    PartKey("DJIMIC3/TX_MIC001_20260829_071201/TX01_MIC002_20260829_071204_orig.wav"),
    PartKey("DJIMIC3/TX_MIC001_20260829_074201/TX01_MIC002_20260829_074210_orig.wav"),
]
ANALYSIS = {
    "title": "開発と打ち合わせの一日",
    "summary": "VoiceDock の削除条件を整理した。午後に MVP の範囲を確定した。",
    "key_points": ["削除の根拠をテキストの保全に置く"],
    "tasks": [
        {"text": "DJI Mic 3 のマウント構造を確認する", "due": None},
        {"text": "Whisper の速度を実測する", "due": "2026-09-05"},
    ],
    "decisions": ["MVP では GUI を作らない"],
    "ideas": ["将来的に話者識別を追加する"],
    "tags": ["VoiceDock", "DJI Mic"],
}


@pytest.fixture
def cfg(make_config: Callable[..., Config]) -> Config:
    return make_config()


def analysis(cfg: Config, **overrides: object):  # type: ignore[no-untyped-def]
    return build_schema(cfg).model_validate(ANALYSIS | overrides)


def links() -> wiki.LinkPlan:
    return wiki.LinkPlan(
        daily_note="[[2026-08-29]]",
        adjacent=("[[2026-08-28 Voice]]", "[[2026-08-30 Voice]]"),
        tags=("[[VoiceDock]]", "#DJI-Mic"),
        raw=("[[2026-08-29 raw]]",),
    )


def render(cfg: Config, **overrides: object) -> str:
    defaults: dict[str, object] = {
        "day": DAY,
        "session_key": SESSION_KEY,
        "recording_keys": KEYS,
        "failed_keys": [],
        "recorded_seconds": 34880.0,
        "block_count": 2,
        "timeline": [],
        "links": links(),
        "cfg": cfg,
    }
    target = overrides.pop("analysis", None) or analysis(cfg)
    return render_daily_note(target, **(defaults | overrides))  # type: ignore[arg-type]


def frontmatter(text: str) -> dict[str, object]:
    parsed = notes.parse_frontmatter(text)
    assert parsed is not None
    return parsed


# --- frontmatter（§13.4 / §13.7） ---------------------------------------


def test_the_required_keys_are_present(cfg: Config) -> None:
    """**`voicedock_session_key` と `voicedock_recording_keys` は必須**（§13.4）。

    保存検証（W-6 / W-7）と §14.1 の削除判定が見る。
    """
    parsed = frontmatter(render(cfg))
    assert parsed[notes.SESSION_KEY_FIELD] == SESSION_KEY
    assert parsed[notes.RECORDING_KEYS_FIELD] == KEYS
    assert parsed["type"] == "voice-daily"


def test_the_metadata_matches_the_spec_example(cfg: Config) -> None:
    parsed = frontmatter(render(cfg))
    assert parsed["date"] == "2026-08-29"
    assert parsed["parts"] == 2
    assert parsed["blocks"] == 2
    assert parsed["status"] == "processed"


def test_recorded_is_the_total_duration(cfg: Config) -> None:
    """`recorded` は実録音時間の合計（`HH:MM:SS`。§13.4）。"""
    assert frontmatter(render(cfg))["recorded"] == "09:41:20"


def test_recorded_tolerates_a_missing_total(cfg: Config) -> None:
    assert frontmatter(render(cfg, recorded_seconds=None))["recorded"] == "00:00:00"


def test_tags_combine_defaults_and_llm_output(cfg: Config) -> None:
    """`obsidian.default_tags` + LLM の `tags` を結合し重複除去（§13.4）。

    **既定タグが先に来る。**`VoiceDock` は既定の `voicedock` と大小違いなので
    畳まれる（最初に現れたほうが残る）。
    """
    tags = frontmatter(render(cfg))["tags"]
    assert isinstance(tags, list)
    assert tags[:2] == list(cfg.obsidian.default_tags)
    assert "DJI-Mic" in tags
    assert "VoiceDock" not in tags, "既定の voicedock と重複しているので畳まれる"


def test_tags_replace_spaces(cfg: Config) -> None:
    """**空白を `-` へ置換する**（§13.4）。

    Obsidian のタグは空白で切れるので、`DJI Mic` が 2 つのタグになる。
    """
    tags = frontmatter(render(cfg))["tags"]
    assert isinstance(tags, list)
    assert "DJI-Mic" in tags
    assert "DJI Mic" not in tags


def test_tags_are_deduped_case_insensitively(cfg: Config) -> None:
    """既定タグと LLM のタグが大小違いで重なっても 1 件にする。"""
    tags = frontmatter(render(cfg, analysis=analysis(cfg, tags=["Voice", "VOICEDOCK"])))["tags"]
    assert isinstance(tags, list)
    assert [value.casefold() for value in tags] == ["voice", "voicedock"]


# --- セクション（§13.4 / §7.2） -----------------------------------------


def test_the_section_order_follows_the_configuration(cfg: Config) -> None:
    text = render(cfg)
    positions = [
        text.index(cfg.llm.analysis.sections[name].heading or "")
        for name in cfg.llm.analysis.order
        if name != "timeline" and cfg.llm.analysis.sections[name].heading
    ]
    assert positions == sorted(positions)


def test_the_headings_come_from_the_configuration(make_config: Callable[..., Config]) -> None:
    cfg = make_config({"llm": {"analysis": {"sections": {"key_points": {"heading": "## 要点"}}}}})
    assert "## 要点" in render(cfg)
    assert "## Key Points" not in render(cfg)


@pytest.mark.parametrize("name", ["key_points", "tasks", "decisions", "ideas"])
def test_a_disabled_section_is_absent(make_config: Callable[..., Config], name: str) -> None:
    """**切ったセクションはノートにも出ない**（§7.2 / §13.4）。"""
    cfg = make_config({"llm": {"analysis": {"sections": {name: {"enabled": False}}}}})
    heading = cfg.llm.analysis.sections[name].heading
    assert heading is not None
    assert heading not in render(
        cfg,
        analysis=build_schema(cfg).model_validate({k: v for k, v in ANALYSIS.items() if k != name}),
    )


@pytest.mark.parametrize("name", ["key_points", "decisions", "ideas"])
def test_an_empty_section_is_omitted_entirely(cfg: Config, name: str) -> None:
    """**空なら見出しごと省略する**（§13.4）。

    見出しだけ残ると「LLM が何も抽出しなかった」のか「セクションを切った」のかが
    読み手に分からない。
    """
    text = render(cfg, analysis=analysis(cfg, **{name: []}))
    heading = cfg.llm.analysis.sections[name].heading
    assert heading is not None
    assert heading not in text


def test_an_empty_summary_omits_the_section(cfg: Config) -> None:
    text = render(cfg, analysis=analysis(cfg, summary="x"))
    assert "## Summary" in text


# --- Tasks（§13.4） -----------------------------------------------------


def test_tasks_are_checkboxes(cfg: Config) -> None:
    text = render(cfg)
    assert "- [ ] DJI Mic 3 のマウント構造を確認する" in text


def test_a_due_date_gets_the_calendar_mark(cfg: Config) -> None:
    """`due` があれば ` 📅 {due}` を付加（§13.4）。"""
    assert "- [ ] Whisper の速度を実測する 📅 2026-09-05" in render(cfg)


def test_a_task_without_a_due_has_no_mark(cfg: Config) -> None:
    text = render(cfg)
    line = next(row for row in text.splitlines() if "マウント構造" in row)
    assert "📅" not in line


# --- Timeline（§13.4 / §12.4） ------------------------------------------


def test_the_timeline_uses_map_intermediates(cfg: Config) -> None:
    """**Map 段階の中間結果を時刻順に並べたもの**（§13.4）。追加の LLM 呼び出しは不要。"""
    start = datetime(2026, 8, 29, 7, 12, tzinfo=JST)
    blocks = [
        TimelineBlock(start, start + timedelta(hours=4), ("朝の移動中に整理した",)),
        TimelineBlock(start + timedelta(hours=6), start + timedelta(hours=12), ("MVP を確定した",)),
    ]
    text = render(cfg, timeline=blocks)
    assert "### 07:12–11:12" in text
    assert "- 朝の移動中に整理した" in text
    assert "### 13:12–19:12" in text


def test_the_timeline_is_omitted_when_empty(cfg: Config) -> None:
    assert "## Timeline" not in render(cfg, timeline=[])


def test_build_timeline_prefers_partials(cfg: Config) -> None:
    model = build_schema(cfg, partial=True)
    partials = [
        model.model_validate({"summary": "朝", "key_points": ["朝の点"]}),
        model.model_validate({"summary": "夕", "key_points": ["夕の点"]}),
    ]
    start = datetime(2026, 8, 29, 7, 0, tzinfo=JST)
    chunks = [
        Chunk("a", start, start + timedelta(hours=1), ()),
        Chunk("b", start + timedelta(hours=5), start + timedelta(hours=6), ()),
    ]
    blocks = build_timeline(
        partials=partials, chunks=chunks, transcript=_transcript(), summary="使わない"
    )
    assert [block.lines for block in blocks] == [("朝の点",), ("夕の点",)]


def test_build_timeline_falls_back_to_the_summary(cfg: Config) -> None:
    """**単一パス処理で Map 中間結果が無い場合**（§12.4 / §13.4）。

    セッション全体を Block ごとに 1 ブロックとして扱い、本文は `summary` の各文を
    箇条書きにする。**Timeline のためだけに LLM を再度呼ばない。**
    """
    blocks = build_timeline(
        partials=(), chunks=(), transcript=_transcript(), summary="一文目。二文目。"
    )
    assert len(blocks) == 1
    assert blocks[0].lines == ("一文目。", "二文目。")


def test_build_timeline_is_empty_without_a_summary(cfg: Config) -> None:
    assert build_timeline(partials=(), chunks=(), transcript=_transcript(), summary="") == []


def _transcript() -> SessionTranscript:
    start = datetime(2026, 8, 29, 7, 12, tzinfo=JST)
    return SessionTranscript(
        day_date=DAY,
        segments=[AbsoluteSegment(start, start + timedelta(seconds=5), "x")],
        blocks=[(start, start + timedelta(hours=4))],
        excluded_partkeys=[],
    )


# --- 欠落を隠さない（§13.4。最重要） ------------------------------------


def test_failed_parts_are_listed_in_the_frontmatter(cfg: Config) -> None:
    """**欠落を隠さない**（§13.4）。

    除外された Part の音声は削除されない — §14.1 が `voicedock_recording_keys` への
    収録を要求するため自動的に保証される。**`voicedock_failed_parts` は別の欄である。**
    """
    failed = [PartKey("DJIMIC3/TX_MIC001_20260829_090000/x_orig.wav")]
    parsed = frontmatter(render(cfg, failed_keys=failed))
    assert parsed[daily.FAILED_PARTS_FIELD] == failed
    assert parsed[notes.RECORDING_KEYS_FIELD] == KEYS, "失敗した Part を含めてはならない"


def test_a_warning_line_follows_the_title(cfg: Config) -> None:
    failed = [PartKey("DJIMIC3/a/x_orig.wav"), PartKey("DJIMIC3/b/y_orig.wav")]
    lines = render(cfg, failed_keys=failed).splitlines()
    title_index = lines.index("# 開発と打ち合わせの一日")
    warning = lines[title_index + 2]
    assert warning.startswith("> ⚠")
    assert "2 本" in warning
    assert "自動で再試行" in warning


def test_no_warning_without_failures(cfg: Config) -> None:
    assert "⚠" not in render(cfg)


def test_an_empty_failed_list_is_rendered_as_brackets(cfg: Config) -> None:
    """**`[]` を明示する。**`key:` だと YAML で `null` に読まれ、「0 件」と「欄が無い」が
    区別できなくなる（#24 で見つけた同じ性質）。
    """
    assert f"{daily.FAILED_PARTS_FIELD}: []" in render(cfg)


# --- Sources / Links（§13.4 / §13.8） -----------------------------------


def test_sources_links_to_the_raw_note(cfg: Config) -> None:
    """**全文は載せない**（`wiki.include_transcript` 既定 false）。W-9 がこのリンクを見る。"""
    text = render(cfg)
    assert "## Sources" in text
    assert "- [[2026-08-29 raw]]" in text


def test_links_include_the_date_and_adjacent_days(cfg: Config) -> None:
    text = render(cfg)
    assert "- [[2026-08-29]]" in text
    assert "- [[2026-08-28 Voice]]" in text
    assert "- [[2026-08-30 Voice]]" in text


def test_plain_tags_are_not_in_links(cfg: Config) -> None:
    """`#タグ` は `## Links` に並べない（リンクではない）。"""
    text = render(cfg)
    section = text[text.index("## Links") :]
    assert "#DJI-Mic" not in section
    assert "[[VoiceDock]]" in section


def test_no_sources_section_without_raw_links(cfg: Config) -> None:
    text = render(cfg, links=wiki.LinkPlan(daily_note="[[2026-08-29]]"))
    assert "## Sources" not in text


# --- エスケープ（§13.4 / §13.6） ----------------------------------------


def test_a_body_line_starting_with_dashes_is_escaped(cfg: Config) -> None:
    """**行頭の `---` は `\\---` へ退避する**（§13.4）。

    退避しないと W-5（frontmatter の境界）が本文の途中で終端を見つける。
    """
    text = render(cfg, analysis=analysis(cfg, summary="前\n---\n後"))
    body = text.split("---\n", 2)[2]
    assert "\n---\n" not in body
    assert "\\---" in body


def test_the_frontmatter_is_parseable_with_hostile_tags(cfg: Config) -> None:
    """LLM が返しうる崩れたタグでも frontmatter が壊れないこと（§13.4 の YAML エスケープ）。"""
    text = render(cfg, analysis=analysis(cfg, tags=["a: b", 'c "d"', "e\\f"]))
    parsed = notes.parse_frontmatter(text)
    assert parsed is not None
    assert isinstance(parsed["tags"], list)


def test_a_title_with_a_colon_is_safe(cfg: Config) -> None:
    text = render(cfg, analysis=analysis(cfg, title="設計: 削除条件"))
    assert notes.parse_frontmatter(text) is not None
    assert "# 設計: 削除条件" in text


# --- ファイル名（§13.5 / V-14） -----------------------------------------


def test_the_filename_never_uses_the_title(cfg: Config) -> None:
    """**`{title}` を入れてはならない**（V-14）。

    LLM が生成した題をファイル名にすると、**再生成のたびに別のファイルができる。**
    """
    assert daily.daily_filename(cfg, DAY) == "2026-08-29 Voice"
    assert "{title}" not in cfg.obsidian.wiki.filename_template


def test_the_filename_is_sanitized(make_config: Callable[..., Config]) -> None:
    cfg = make_config({"obsidian": {"wiki": {"filename_template": "{date}/Voice"}}})
    assert "/" not in daily.daily_filename(cfg, DAY)


# --- Map 中間結果の保存（§10.9。v5.5→v5.6 の変更 R-1） ------------------


def test_the_timeline_is_saved_next_to_the_analysis(tmp_path: Path) -> None:
    """§10.9: `<analysis_path>` の隣へ `<slug>.timeline.json`。

    **再生成のために 18 回の LLM 呼び出しをやり直すわけにはいかない。**
    """
    analysis_path = tmp_path / "abc123.json"
    start = datetime(2026, 8, 29, 7, 12, tzinfo=JST)
    blocks = [TimelineBlock(start, start + timedelta(hours=1), ("点",))]
    daily.save_timeline(analysis_path, blocks, fingerprint=FINGERPRINT)
    assert daily.timeline_path(analysis_path).name == "abc123.timeline.json"
    assert daily.load_timeline(analysis_path, fingerprint=FINGERPRINT) == blocks


def test_a_timeline_of_another_transcript_is_ignored(tmp_path: Path) -> None:
    """**指紋が違えば「無い」と同じ扱い**（§9.4 / #108）。

    `save_timeline()` は書き込みの失敗を握りつぶすので、**解析だけが新しく
    Timeline が古い**状態が起こりうる。そのまま使うと**本文が古いノート**ができる。
    """
    analysis_path = tmp_path / "abc123.json"
    start = datetime(2026, 8, 29, 7, 12, tzinfo=JST)
    blocks = [TimelineBlock(start, start + timedelta(hours=1), ("点",))]
    daily.save_timeline(analysis_path, blocks, fingerprint=FINGERPRINT)
    assert daily.load_timeline(analysis_path, fingerprint="別の指紋") == []


def test_a_timeline_without_a_fingerprint_is_ignored(tmp_path: Path) -> None:
    """v5.18 以前の形式（指紋を持たない配列）も使わない。"""
    analysis_path = tmp_path / "abc123.json"
    daily.timeline_path(analysis_path).write_text(
        '[{"start_at": "2026-08-29T07:12:00+09:00", "end_at": "2026-08-29T08:12:00+09:00",'
        ' "lines": ["点"]}]',
        encoding="utf-8",
    )
    assert daily.load_timeline(analysis_path, fingerprint=FINGERPRINT) == []


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "{}",
        "[1,2]",
        '{"schema": 2}',
        '{"schema": 2, "transcript_sha256": "FP", "blocks": "x"}',
        '{"schema": 2, "transcript_sha256": "FP", "blocks": [{"start_at": "x"}]}',
        '{"schema": 2, "transcript_sha256": "FP", "blocks": [{"lines": []}]}',
    ],
)
def test_a_broken_timeline_falls_back(tmp_path: Path, content: str) -> None:
    """**読めなければ空**（§10.9 の代替経路へ落ちる）。**失敗させない。**"""
    analysis_path = tmp_path / "abc123.json"
    daily.timeline_path(analysis_path).write_text(content, encoding="utf-8")
    assert daily.load_timeline(analysis_path, fingerprint=FINGERPRINT) == []


def test_saving_a_timeline_never_raises(tmp_path: Path) -> None:
    blocked = tmp_path / "sub" / "abc.json"
    (tmp_path / "sub").write_text("not a directory", encoding="utf-8")
    daily.save_timeline(blocked, [], fingerprint=FINGERPRINT)  # 例外を投げない
