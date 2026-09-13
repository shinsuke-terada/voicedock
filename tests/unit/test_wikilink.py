"""`voicedock.wiki` が SPEC §13.8 の WikiLink 生成規則に従うことを固定する。

**LLM に `[[]]` を生成させてはならない**（§14.4 N-15）。存在しないノートへのリンクを
幻覚する。リンクはレンダラが機械的に付与する。

**「既存ノートだけリンクする」のが §13.8 の核心である。**未解決リンクを毎日大量に作ると
Obsidian のグラフが空ノードで埋まり実用性が落ちる。1 日 1 ノート × 15 タグで、
年 5,475 個の空ノードになる。
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from voicedock import wiki
from voicedock.config import Config
from voicedock.log import EVENTS
from voicedock.wiki import (
    FORBIDDEN_CHARS,
    VaultIndex,
    build_index,
    date_note,
    index_for,
    is_linkable,
    normalize_name,
    plan_links,
    raw_folder_prefix,
)

DAY = date(2026, 9, 12)
SELF_NAME = "2026-09-12 Voice"


def name_for_day(day: date) -> str:
    """既定の `wiki.filename_template`（`"{date} Voice"`）相当。"""
    return f"{day.isoformat()} Voice"


@pytest.fixture
def cfg(make_config: Callable[..., Config]) -> Config:
    return make_config()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "obsidian"
    root.mkdir()
    return root


def note(vault: Path, relative: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# x\n", encoding="utf-8")
    return path


def plan(cfg: Config, **kwargs: object) -> wiki.LinkPlan:
    defaults: dict[str, object] = {
        "cfg": cfg,
        "day": DAY,
        "tags": (),
        "index": None,
        "self_name": SELF_NAME,
        "name_for_day": name_for_day,
    }
    return plan_links(**(defaults | kwargs))  # type: ignore[arg-type]


# --- §16.4: 専用イベントを作らない --------------------------------------


def test_no_wikilink_event_name_exists() -> None:
    """**`wikilink_*` というイベント名を作らない**（§16.4）。

    v5.0 で `wikilink_index_built` / `wikilink_skipped` を削除し、「DEBUG で足りる。
    §13.8 は本文の処理であり、失敗しても保存は続く」と定めた。`log.py` は未登録の
    名前に `ValueError` を投げるので、**書けば必ず落ちるコードになる。**
    """
    assert not [name for name in EVENTS if name.startswith("wikilink")]


# --- Vault インデックス（§13.8） ----------------------------------------


def test_the_index_collects_markdown_basenames(vault: Path, cfg: Config) -> None:
    note(vault, "VoiceDock.md")
    note(vault, "Projects/DJI.md")
    note(vault, "Projects/Deep/Nested.md")
    note(vault, "notes.txt")

    index = build_index(vault)
    assert index.contains("VoiceDock")
    assert index.contains("DJI")
    assert index.contains("Nested")
    assert not index.contains("notes")


@pytest.mark.parametrize("folder", [".obsidian", ".trash"])
def test_dot_directories_are_excluded(vault: Path, folder: str) -> None:
    """§13.8 が名指しする除外。`.` 始まりは §10.2 と同じ方針で黙って無視する。"""
    note(vault, f"{folder}/Template.md")
    assert not build_index(vault).contains("Template")


def test_the_raw_folder_is_excluded(vault: Path, cfg: Config) -> None:
    """**Raw フォルダを除外する**（§13.8）。

    除外しないと、タグが Raw ノート（生データ）へリンクされる。
    """
    note(vault, "Daily/Voice/Raw/20260912/2026-09-12 raw.md")
    note(vault, "VoiceDock.md")
    index = build_index(vault, exclude_prefixes=(raw_folder_prefix(cfg),))
    assert not index.contains("2026-09-12 raw")
    assert index.contains("VoiceDock")


def test_every_past_day_of_raw_is_excluded(vault: Path, cfg: Config) -> None:
    """**過去日の Raw フォルダも除外される。**

    `folder_template` の `{` より前（`Daily/Voice/Raw`）を接頭辞にしているため、
    日付ごとのフォルダを列挙しなくても全部落ちる。1 年で 365 フォルダになる。
    """
    for day in ("20260101", "20260612", "20261231"):
        note(vault, f"Daily/Voice/Raw/{day}/{day} raw.md")
    index = build_index(vault, exclude_prefixes=(raw_folder_prefix(cfg),))
    assert index.names == frozenset()


def test_raw_folder_prefix_stops_at_the_placeholder(cfg: Config) -> None:
    assert raw_folder_prefix(cfg) == "Daily/Voice/Raw"


def test_raw_folder_prefix_without_a_placeholder(
    make_config: Callable[..., Config],
) -> None:
    cfg = make_config({"obsidian": {"raw": {"folder_template": "Voice/Raw"}}})
    assert raw_folder_prefix(cfg) == "Voice/Raw"


def test_the_wiki_folder_is_not_excluded(vault: Path, cfg: Config) -> None:
    """**Daily ノートのフォルダは除外しない。**隣接日のリンク先がそこに在る。"""
    note(vault, "Daily/Voice/Wiki/20260911/2026-09-11 Voice.md")
    index = build_index(vault, exclude_prefixes=(raw_folder_prefix(cfg),))
    assert index.contains("2026-09-11 Voice")


def test_an_unreadable_directory_is_skipped(vault: Path) -> None:
    """**例外を投げない**（§13.8 / §1.3）。

    1 個の壊れたディレクトリで Daily ノートの保存を止めない。
    """
    if os.geteuid() == 0:
        pytest.skip("root ではディレクトリの読み取り拒否が成立しない")
    note(vault, "Readable.md")
    locked = vault / "locked"
    locked.mkdir()
    note(vault, "locked/Hidden.md")
    locked.chmod(0o000)
    try:
        index = build_index(vault)
    finally:
        locked.chmod(0o755)
    assert index.contains("Readable")
    assert not index.contains("Hidden")


def test_a_missing_vault_is_an_empty_index(tmp_path: Path) -> None:
    assert build_index(tmp_path / "absent").names == frozenset()


def test_symlinked_directories_are_not_followed(vault: Path) -> None:
    """シンボリックリンクを辿らない。**Vault 外を走査しない / 循環で止まらない。**"""
    outside = vault.parent / "outside"
    outside.mkdir()
    (outside / "Outside.md").write_text("x", encoding="utf-8")
    (vault / "link").symlink_to(outside)
    assert not build_index(vault).contains("Outside")


# --- 正規化（§13.8） ----------------------------------------------------


def test_matching_is_nfc_normalized(vault: Path) -> None:
    """**NFC 正規化して突き合わせる**（§13.8）。

    macOS のファイルシステムは NFD を返すので、揃えないと「がぎぐ」と「がぎぐ」が
    別のノートになる（§13.5 S-1 と同じ理由）。
    """
    note(vault, f"{unicodedata.normalize('NFD', 'がぎぐ')}.md")
    assert build_index(vault).contains("がぎぐ")


def test_matching_ignores_case(vault: Path) -> None:
    note(vault, "VoiceDock.md")
    index = build_index(vault)
    assert index.contains("voicedock")
    assert index.contains("VOICEDOCK")


def test_matching_is_exact_only(vault: Path) -> None:
    """**完全一致のみ。部分一致・曖昧一致はしない**（§13.8）。

    部分一致にすると「DJI」が「DJI Mic 3 の設定」へ張られ、**意図しないノートが
    グラフの中心になる。**
    """
    note(vault, "VoiceDock の設計.md")
    index = build_index(vault)
    assert not index.contains("VoiceDock")
    assert index.contains("VoiceDock の設計")


def test_normalize_name_folds_hard_cases() -> None:
    assert normalize_name("Straße") == normalize_name("STRASSE")


# --- キャッシュ（§13.8） ------------------------------------------------


def test_the_index_is_cached_until_the_ttl(vault: Path, cfg: Config) -> None:
    note(vault, "First.md")
    first = index_for(cfg, vault, now=0.0)
    assert first is not None

    note(vault, "Second.md")
    ttl = cfg.obsidian.wiki.vault_index_cache_seconds
    cached = index_for(cfg, vault, cached=first, now=float(ttl - 1))
    assert cached is first, "TTL 内で作り直している"
    assert not cached.contains("Second")


def test_the_index_is_rebuilt_after_the_ttl(vault: Path, cfg: Config) -> None:
    note(vault, "First.md")
    first = index_for(cfg, vault, now=0.0)
    note(vault, "Second.md")
    ttl = cfg.obsidian.wiki.vault_index_cache_seconds
    rebuilt = index_for(cfg, vault, cached=first, now=float(ttl))
    assert rebuilt is not None
    assert rebuilt.contains("Second")


def test_the_cache_uses_a_monotonic_clock(vault: Path, cfg: Config) -> None:
    """**壁時計を使わない。**時刻の巻き戻し（NTP 補正）でキャッシュが永久化する。"""
    index = build_index(vault)
    assert index.built_at < 10**9, "UNIX 時刻が入っている"


def test_link_tags_false_builds_no_index(
    vault: Path, make_config: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**`link_tags: false` ならインデックスを構築しない**（§13.8）。

    日付リンクと Raw リンクだけならファイル一覧は要らない。Vault が 1 万ノートあると
    `scandir` が無駄に走る。
    """
    calls: list[Path] = []

    def spy(root: Path, **kwargs: object) -> VaultIndex:
        calls.append(root)
        return VaultIndex(names=frozenset(), built_at=0.0)

    monkeypatch.setattr(wiki, "build_index", spy)

    assert index_for(make_config({"obsidian": {"wiki": {"link_tags": False}}}), vault) is None
    assert calls == [], "インデックスを作ってしまっている"

    # 逆向きも見る: `link_tags: true` なら構築する
    assert index_for(make_config(), vault) is not None
    assert calls == [vault]


def test_build_index_walks_the_vault_with_scandir(vault: Path) -> None:
    """§13.8 は `os.scandir` を指定している（`rglob` より安い）。

    動作で確かめる: 入れ子のディレクトリまで降りていること。
    """
    note(vault, "a/b/c/Deep.md")
    index = build_index(vault)
    assert index.contains("Deep")
    assert index.scanned_dirs >= 4


# --- 候補の除外（§13.8） ------------------------------------------------


@pytest.mark.parametrize("char", sorted(FORBIDDEN_CHARS))
def test_candidates_with_forbidden_characters_are_excluded(
    cfg: Config, vault: Path, char: str
) -> None:
    """**`[` `]` `|` `#` `^` を含む候補は除外**（§13.8）。

    `|` は別名記法、`#` はタグとブロック参照、`^` はブロック参照である。
    **含んだまま囲むと別の構文になる。**
    """
    candidate = f"a{char}b"
    note(vault, f"{candidate}.md")
    result = plan(cfg, tags=(candidate,), index=build_index(vault))
    assert result.tags == ()
    assert candidate in result.dropped


@pytest.mark.parametrize("candidate", ["", "   ", "\n"])
def test_blank_candidates_are_excluded(cfg: Config, candidate: str) -> None:
    assert not is_linkable(candidate)
    assert plan(cfg, tags=(candidate,)).tags == ()


def test_a_self_reference_is_excluded(cfg: Config, vault: Path) -> None:
    """**自己参照リンクは除外**（§13.8）。グラフで自分への辺ができる。"""
    note(vault, f"{SELF_NAME}.md")
    result = plan(cfg, tags=(SELF_NAME,), index=build_index(vault))
    assert result.tags == ()
    assert SELF_NAME in result.dropped


def test_a_self_reference_is_excluded_case_insensitively(cfg: Config, vault: Path) -> None:
    note(vault, f"{SELF_NAME}.md")
    result = plan(cfg, tags=(SELF_NAME.upper(),), index=build_index(vault))
    assert result.tags == ()


# --- タグ（§13.8 の中核） -----------------------------------------------


def test_an_existing_note_becomes_a_link(cfg: Config, vault: Path) -> None:
    note(vault, "VoiceDock.md")
    result = plan(cfg, tags=("VoiceDock",), index=build_index(vault))
    assert result.tags == ("[[VoiceDock]]",)


def test_a_missing_note_stays_a_tag(cfg: Config, vault: Path) -> None:
    """**Vault に無いものは `#タグ` のまま**（§13.8）。

    未解決リンクを毎日大量に作るとグラフが空ノードで埋まる。
    """
    result = plan(cfg, tags=("存在しない",), index=build_index(vault))
    assert result.tags == ("#存在しない",)


def test_mixed_tags_keep_their_order(cfg: Config, vault: Path) -> None:
    """**順序は入力のまま**（LLM が重要な順に並べる）。"""
    note(vault, "DJI.md")
    result = plan(cfg, tags=("存在しない", "DJI", "これも無い"), index=build_index(vault))
    assert result.tags == ("#存在しない", "[[DJI]]", "#これも無い")


def test_link_only_existing_false_links_everything(
    make_config: Callable[..., Config], vault: Path
) -> None:
    cfg = make_config({"obsidian": {"wiki": {"link_only_existing": False}}})
    result = plan(cfg, tags=("存在しない",), index=build_index(vault))
    assert result.tags == ("[[存在しない]]",)


def test_link_tags_false_leaves_plain_tags(make_config: Callable[..., Config], vault: Path) -> None:
    note(vault, "VoiceDock.md")
    cfg = make_config({"obsidian": {"wiki": {"link_tags": False}}})
    result = plan(cfg, tags=("VoiceDock",), index=None)
    assert result.tags == ("#VoiceDock",)


# --- 日付と隣接日（§13.8） ----------------------------------------------


def test_the_date_note_is_linked_once(cfg: Config) -> None:
    """`[[YYYY-MM-DD]]` を 1 個（§13.8）。"""
    result = plan(cfg)
    assert result.daily_note == "[[2026-09-12]]"
    assert date_note(DAY) == "[[2026-09-12]]"


def test_the_adjacent_days_are_linked(cfg: Config) -> None:
    result = plan(cfg)
    assert result.adjacent == ("[[2026-09-11 Voice]]", "[[2026-09-13 Voice]]")


def test_link_daily_note_false(make_config: Callable[..., Config]) -> None:
    cfg = make_config({"obsidian": {"wiki": {"link_daily_note": False}}})
    assert plan(cfg).daily_note is None


def test_link_adjacent_days_false(make_config: Callable[..., Config]) -> None:
    cfg = make_config({"obsidian": {"wiki": {"link_adjacent_days": False}}})
    assert plan(cfg).adjacent == ()


def test_the_adjacent_names_come_from_the_caller(cfg: Config) -> None:
    """**`wiki.filename_template` の解釈は呼び手の仕事**（テンプレート解釈を 2 箇所に置かない）。"""
    result = plan(cfg, name_for_day=lambda day: f"Journal {day.isoformat()}")
    assert result.adjacent == ("[[Journal 2026-09-11]]", "[[Journal 2026-09-13]]")


def test_a_month_boundary(cfg: Config) -> None:
    result = plan(cfg, day=date(2026, 3, 1))
    assert result.adjacent == ("[[2026-02-28 Voice]]", "[[2026-03-02 Voice]]")


# --- Raw リンク（§13.8） ------------------------------------------------


def test_raw_notes_are_linked(cfg: Config) -> None:
    result = plan(cfg, raw_names=("2026-09-12 raw",))
    assert result.raw == ("[[2026-09-12 raw]]",)


def test_link_raw_false(make_config: Callable[..., Config]) -> None:
    cfg = make_config({"obsidian": {"wiki": {"link_raw": False}}})
    assert plan(cfg, raw_names=("2026-09-12 raw",)).raw == ()


# --- max_links（§13.8） -------------------------------------------------


def test_the_link_count_is_capped(make_config: Callable[..., Config], vault: Path) -> None:
    cfg = make_config({"obsidian": {"wiki": {"max_links": 5}}})
    for n in range(10):
        note(vault, f"Tag{n}.md")
    result = plan(cfg, tags=tuple(f"Tag{n}" for n in range(10)), index=build_index(vault))
    assert result.counted == 5
    # 日付 1 + 隣接 2 = 3 本を先に使い、タグは 2 本だけリンクになる
    assert [value for value in result.tags if value.startswith("[[")] == ["[[Tag0]]", "[[Tag1]]"]
    assert result.tags[2] == "#Tag2", "上限を超えた候補は #タグ へ落ちる"


def test_raw_links_are_not_counted(make_config: Callable[..., Config]) -> None:
    """**Raw リンクは上限の対象外**（§13.8）。

    Raw リンクは「この日の記録がどこにあるか」であり、**上限で落ちると §14.1 の
    削除根拠を辿れなくなる。**
    """
    cfg = make_config({"obsidian": {"wiki": {"max_links": 1}}})
    result = plan(cfg, raw_names=tuple(f"raw {n}" for n in range(32)))
    assert len(result.raw) == 32
    assert result.counted == 1


def test_the_cap_does_not_drop_tags_entirely(
    make_config: Callable[..., Config], vault: Path
) -> None:
    """**上限を超えた候補はリンクを外すだけで、本文から消さない。**

    消すと「LLM が抽出したタグ」が失われる。`#タグ` として残れば検索はできる。
    """
    cfg = make_config({"obsidian": {"wiki": {"max_links": 3}}})
    note(vault, "Kept.md")
    result = plan(cfg, tags=("Kept",), index=build_index(vault))
    assert result.tags == ("#Kept",)
    assert "Kept" in result.dropped


def test_zero_max_links_disables_every_counted_link(
    make_config: Callable[..., Config], vault: Path
) -> None:
    cfg = make_config({"obsidian": {"wiki": {"max_links": 0}}})
    note(vault, "Tag.md")
    result = plan(cfg, tags=("Tag",), index=build_index(vault), raw_names=("raw",))
    assert result.daily_note is None
    assert result.adjacent == ()
    assert result.tags == ("#Tag",)
    assert result.raw == ("[[raw]]",), "Raw は上限の対象外"


def test_the_default_cap_is_twenty(cfg: Config) -> None:
    assert cfg.obsidian.wiki.max_links == 20


# --- VaultIndex 単体 ----------------------------------------------------


def test_is_stale_at_the_boundary() -> None:
    index = VaultIndex(names=frozenset(), built_at=100.0)
    assert not index.is_stale(ttl_seconds=300, now=399.0)
    assert index.is_stale(ttl_seconds=300, now=400.0)
