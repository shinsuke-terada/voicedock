"""WikiLink の生成規則と Vault インデックス（SPEC §13.8）。

**LLM に `[[]]` を生成させてはならない**（§14.4 N-15）。存在しないノートへのリンクを
幻覚する。リンクはレンダラが機械的に付与する。

**このモジュールは「リンクにするかどうか」だけを決める。**文字列をノートへ差し込むのは
#28 の Daily レンダラである。分けるのは、**Daily ノートのレンダラが無いと
この判断をテストできない**状態を避けるためである。

**リンクの生成失敗は保存検証の合否に影響させない**（§13.8）。だからこのモジュールは
例外を投げず、読めないものは黙って飛ばす。**リンク生成の不具合が Daily ノートの保存を
止めてはならない。**
"""

from __future__ import annotations

import os
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Final

from voicedock.config import Config

SUFFIX: Final = ".md"
DEFAULT_EXCLUDED_DIRS: Final[frozenset[str]] = frozenset({".obsidian", ".trash"})
"""§13.8 が名指しする除外。`.` 始まりは §10.2 と同じ方針で黙って無視する。"""

FORBIDDEN_CHARS: Final[frozenset[str]] = frozenset("[]|#^")
"""リンク候補に含められない文字（§13.8）。

`|` は Obsidian の別名記法（`[[a|b]]`）、`#` はタグとブロック参照、`^` はブロック参照、
`[` `]` はリンクの入れ子である。**含んだまま `[[]]` で囲むと別の構文になる。**
"""

DATE_FORMAT: Final = "%Y-%m-%d"
"""日付ノートの名前（§13.8 の `[[YYYY-MM-DD]]`）。"""


# --- Vault インデックス（§13.8） ----------------------------------------


def normalize_name(name: str) -> str:
    """突き合わせ用の正規形。**NFC 正規化 + `casefold()`**（§13.8）。

    macOS のファイルシステムは NFD を返すので、揃えないと「がぎぐ」と「がぎぐ」が
    別のノートになる（§13.5 S-1 と同じ理由）。`casefold()` は `lower()` より強く、
    `ß` → `ss` まで畳む。
    """
    return unicodedata.normalize("NFC", name).casefold()


@dataclass(frozen=True)
class VaultIndex:
    """Vault 内に実在する `.md` の basename 集合（§13.8）。"""

    names: frozenset[str]
    """`normalize_name()` を通した形で保持する。"""

    built_at: float
    """`time.monotonic()`。**壁時計を使わない** — 時刻の巻き戻しでキャッシュが永久化する。"""

    scanned_dirs: int = 0
    """走査したディレクトリ数。DEBUG ログ用（§13.8 は専用イベントを持たない）。"""

    def contains(self, name: str) -> bool:
        return normalize_name(name) in self.names

    def is_stale(self, *, ttl_seconds: int, now: float | None = None) -> bool:
        """`vault_index_cache_seconds` を過ぎたか（§13.8）。"""
        moment = time.monotonic() if now is None else now
        return moment - self.built_at >= ttl_seconds


def build_index(
    vault_root: Path,
    *,
    exclude_prefixes: Sequence[str] = (),
    now: float | None = None,
) -> VaultIndex:
    """Vault 全体を `os.scandir` して `.md` の basename 集合を作る（§13.8）。

    **例外を投げない。**読めないディレクトリは飛ばす。**1 個の壊れたディレクトリで
    Daily ノートの保存を止めない**（§1.3）。

    `exclude_prefixes` は Vault ルートからの相対パスの接頭辞（`"Daily/Voice/Raw"`）。
    """
    excluded = tuple(prefix.strip("/") for prefix in exclude_prefixes if prefix.strip("/"))
    names: set[str] = set()
    scanned = 0
    stack: list[Path] = [vault_root]

    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        scanned += 1
        for entry in entries:
            if entry.name.startswith("."):
                # `.obsidian` / `.trash` を含む。§10.2 と同じで黙って無視する
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if not _is_excluded(Path(entry.path), vault_root, excluded):
                    stack.append(Path(entry.path))
                continue
            if entry.name.endswith(SUFFIX):
                names.add(normalize_name(entry.name.removesuffix(SUFFIX)))

    return VaultIndex(
        names=frozenset(names),
        built_at=time.monotonic() if now is None else now,
        scanned_dirs=scanned,
    )


def _is_excluded(path: Path, vault_root: Path, excluded: tuple[str, ...]) -> bool:
    try:
        relative = path.relative_to(vault_root).as_posix()
    except ValueError:  # pragma: no cover - scandir は常に配下を返す
        return True
    return any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in excluded)


def raw_folder_prefix(cfg: Config) -> str:
    """Raw フォルダの除外接頭辞（§13.8）。

    **`folder_template` の `{` より前を使う。**既定は `Daily/Voice/Raw/{yyyymmdd}` なので
    `Daily/Voice/Raw` を丸ごと除外する。日付ごとに 1 つずつ除外する形にすると、
    **過去日の Raw ノートがインデックスに入り、タグが Raw ノートへリンクされる。**
    """
    template = cfg.obsidian.raw.folder_template
    head, marker, _rest = template.partition("{")
    if not marker:
        return template.strip("/")
    return head.strip("/")


def index_for(
    cfg: Config,
    vault_root: Path,
    *,
    cached: VaultIndex | None = None,
    now: float | None = None,
) -> VaultIndex | None:
    """必要ならインデックスを作り直す。`link_tags: false` なら `None`（§13.8）。

    **構築するのは `link_tags: true` のときだけである。**日付リンクと Raw リンクだけなら
    ファイル一覧は要らない。Vault が 1 万ノートあると `scandir` が無駄に走る。

    **キャッシュは呼び手が持つ**（`cached` を渡す）。モジュール変数に置くと、テストが
    実行順に依存し、`service` が Vault の変更を取り込めなくなる。
    """
    if not cfg.obsidian.wiki.link_tags:
        return None
    ttl = cfg.obsidian.wiki.vault_index_cache_seconds
    if cached is not None and not cached.is_stale(ttl_seconds=ttl, now=now):
        return cached
    return build_index(vault_root, exclude_prefixes=(raw_folder_prefix(cfg),), now=now)


# --- リンク候補の判定（§13.8） ------------------------------------------


def is_linkable(candidate: str) -> bool:
    """`[[]]` で囲める候補か（§13.8）。

    空・空白のみ・`FORBIDDEN_CHARS` を含むものを除く。**含んだまま囲むと別の構文になる。**
    """
    stripped = candidate.strip()
    if not stripped:
        return False
    return not (FORBIDDEN_CHARS & set(stripped))


def link(name: str) -> str:
    return f"[[{name}]]"


def tag(name: str) -> str:
    """`[[]]` にしないタグ。**Vault に無いものはここへ落ちる**（§13.8）。"""
    return f"#{name}"


def date_note(day: date) -> str:
    """日付ノート `[[YYYY-MM-DD]]`（§13.8）。"""
    return link(day.strftime(DATE_FORMAT))


# --- 計画（§13.8） ------------------------------------------------------


@dataclass(frozen=True)
class LinkPlan:
    """レンダラ（#28）へ渡す結果。**文字列の差し込みはしない。**"""

    daily_note: str | None = None
    """`[[YYYY-MM-DD]]`。`link_daily_note: false` なら `None`。"""

    adjacent: tuple[str, ...] = ()
    """前日・翌日の Daily ノートへのリンク。"""

    tags: tuple[str, ...] = field(default_factory=tuple)
    """`[[名前]]` か `#名前`。**順序は入力の `tags` のまま**（LLM が重要な順に並べる）。"""

    raw: tuple[str, ...] = ()
    """Raw ノートへのリンク。**`max_links` の対象外**（§13.8）。"""

    dropped: tuple[str, ...] = ()
    """上限や除外規則で落とした候補。DEBUG ログ用（専用イベントは作らない。§16.4）。"""

    @property
    def counted(self) -> int:
        """`max_links` に数えるリンクの本数（Raw を含めない）。"""
        return (1 if self.daily_note else 0) + len(self.adjacent) + _linked(self.tags)


def _linked(values: Iterable[str]) -> int:
    return sum(1 for value in values if value.startswith("[["))


def plan_links(
    *,
    cfg: Config,
    day: date,
    tags: Sequence[str],
    index: VaultIndex | None,
    self_name: str,
    name_for_day: Callable[[date], str],
    raw_names: Sequence[str] = (),
) -> LinkPlan:
    """その日の Daily ノートに付けるリンクを決める（§13.8）。

    Args:
        index: `index_for()` の結果。`None`（`link_tags: false`）ならタグを `[[]]` にしない。
        self_name: この Daily ノート自身の basename。**自己参照を除く**（§13.8）。
        name_for_day: 日付から Daily ノートの basename を作る関数。
            **`wiki.filename_template` の解釈は呼び手の仕事である** — ここが持つと
            テンプレート解釈が 2 箇所になる（`raw.py` にもある）。
        raw_names: Raw ノートの basename。**`max_links` に数えない**（§13.8）。
    """
    wiki = cfg.obsidian.wiki
    dropped: list[str] = []
    budget = wiki.max_links

    daily = None
    if wiki.link_daily_note:
        candidate = day.strftime(DATE_FORMAT)
        if _usable(candidate, self_name=self_name, dropped=dropped) and budget > 0:
            daily = link(candidate)
            budget -= 1

    adjacent: list[str] = []
    if wiki.link_adjacent_days:
        for offset in (-1, 1):
            candidate = name_for_day(day + timedelta(days=offset))
            if not _usable(candidate, self_name=self_name, dropped=dropped):
                continue
            if budget <= 0:
                dropped.append(candidate)
                continue
            adjacent.append(link(candidate))
            budget -= 1

    rendered: list[str] = []
    for candidate in tags:
        if not _usable(candidate, self_name=self_name, dropped=dropped):
            continue
        # **Vault に実在するものだけ `[[]]` 化する**（§13.8）。未解決リンクを毎日
        # 大量に作るとグラフが空ノードで埋まり実用性が落ちる
        existing = index is not None and index.contains(candidate)
        wanted = wiki.link_tags and (existing or not wiki.link_only_existing)
        if wanted and budget > 0:
            rendered.append(link(candidate))
            budget -= 1
            continue
        if wanted:
            dropped.append(candidate)
        rendered.append(tag(candidate))

    # **Raw へのリンクは必ず付ける**（変更 BN-1）。W-9 が常に要求するので、
    # 付けない選択肢は「その日の Daily ノートが二度と `SAVED` にならない」に等しい
    raw = tuple(link(name) for name in raw_names if is_linkable(name))
    return LinkPlan(
        daily_note=daily,
        adjacent=tuple(adjacent),
        tags=tuple(rendered),
        raw=raw,
        dropped=tuple(dropped),
    )


def _usable(candidate: str, *, self_name: str, dropped: list[str]) -> bool:
    """除外規則（§13.8）。落としたものは `dropped` に残す。"""
    if not is_linkable(candidate):
        dropped.append(candidate)
        return False
    if normalize_name(candidate) == normalize_name(self_name):
        # **自己参照リンクは除外**（§13.8）。Obsidian のグラフで自分への辺ができる
        dropped.append(candidate)
        return False
    return True
