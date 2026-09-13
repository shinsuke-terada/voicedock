"""Raw ノート（文字起こし生データ）のレンダリングと保存（SPEC §13.2, §13.3, §10.7）。

**このノートが「記録が Vault に残る」ことの実体である。**§1.3 の優先順位 1（記録の保護）は
ここで満たされ、LLM も削除もまだ要らない。

**削除の必要条件が 1 つここで揃う。**§14.1 は Raw ノートの `voicedock_recording_keys` に
当該 Part の `partkey` が載っていることを要求している。**この frontmatter を最初から
必須出力にしないと、それ以前に作られたノートは削除判定で恒久的に偽になる**
（安全側だが Phase 7 で backlog を消せない）。

**本文は whisper の出力そのままで、LLM を一切通さない**（§13.3）。整形・要約は
Daily ノート（§13.4）の仕事である。ここで手を入れると「生データ」でなくなり、
**あとから元の発言を確かめる術が無くなる。**

書き込みと検証は `notes.py` を再利用する（§13.6 / §13.7）。**検証ロジックを 2 本に
分けない** — #36 の `cleaner.py` も同じ `verify_note()` を呼ぶ。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

from voicedock import notes
from voicedock.config import RawConfig
from voicedock.notes import CheckResult, NoteKind
from voicedock.paths import VaultPath

HEADING_PART: Final = "##"
HEADING_TIMESTAMP: Final = "###"
NOTE_TYPE: Final = "voice-raw"
SOURCE_LABEL: Final = "DJI Mic 3"
SUFFIX: Final = ".md"

_INTRO: Final = "> 自動文字起こしの生データ。未編集。"


@dataclass(frozen=True)
class RawSegment:
    """絶対時刻を持つ 1 区間（§11.1 の `AbsoluteSegment`）。"""

    at: datetime
    end_at: datetime
    text: str


@dataclass(frozen=True)
class RawPart:
    """Raw ノートに載せる 1 Part。

    `partkey` が **`voicedock_recording_keys` に載る値**であり、§14.1 の削除条件が
    これを見る。**`paths.partkey_for()` で作った値をそのまま運ぶ**（文字列を組み立てない）。
    """

    partkey: str
    started_at: datetime
    ended_at: datetime | None
    segments: tuple[RawSegment, ...]


@dataclass(frozen=True)
class RawNoteResult:
    """`write_raw_note()` の結果。**状態遷移と DB 更新は呼び手が行う**（#21）。"""

    path: VaultPath
    sha256: str
    verification: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        """R-1〜R-6 をすべて満たしたか（§13.7）。

        **これが真になって初めて、その Part の音声を削除してよい状態になる**（§14.1）。
        偽なら `OBSIDIAN_RAW_VERIFY_FAILED` である。
        """
        return notes.all_passed(self.verification)

    @property
    def failed_rules(self) -> tuple[str, ...]:
        return notes.failed_rules(self.verification)


# --- パスとファイル名（§13.2 / §13.5） ---------------------------------


def render_template(template: str, day: date, *, part: str | None = None) -> str:
    """テンプレートのプレースホルダを埋める（V-13 が許すのは 4 つだけ）。

    | プレースホルダ | 値 |
    |---|---|
    | `{yyyymmdd}` | `20260829` |
    | `{date}` | `2026-08-29` |
    | `{time}` | `000000`（Raw は日単位なので固定） |
    | `{part}` | `granularity: part` のときの Part 識別子 |

    **未知のプレースホルダは設定検証（V-13）が起動時に弾く。**ここでは埋めるだけである。
    """
    filled = (
        template.replace("{yyyymmdd}", day.strftime("%Y%m%d"))
        .replace("{date}", day.isoformat())
        .replace("{time}", "000000")
    )
    return filled.replace("{part}", part or "")


def raw_folder(vault_root: Path, cfg: RawConfig, day: date) -> VaultPath:
    """出力先のフォルダ。**作成はしない**（呼び手が `mkdir` する）。

    `folder_template` が相対パスで `..` を含まないことは V-11 が起動時に保証する。
    """
    return VaultPath(vault_root / render_template(cfg.folder_template, day))


def raw_filename(cfg: RawConfig, day: date, *, part: str | None = None, max_bytes: int) -> str:
    """`.md` を除いたファイル名。**sanitize を必ず通す**（§13.5）。

    `granularity: part` のときは `{part}` が必須である（V-27）。含まないと
    **Part ごとのファイルが同名衝突して上書きし合う。**
    """
    return notes.sanitize_filename(
        render_template(cfg.filename_template, day, part=part), max_bytes=max_bytes
    )


# --- レンダリング（§13.3） ---------------------------------------------


def render_raw_note(
    parts: Sequence[RawPart],
    *,
    day: date,
    session_key: str,
    cfg: RawConfig,
) -> str:
    """Raw ノートの Markdown を返す（§13.3）。

    **`parts` は `started_at` 昇順で渡すこと。**並べ替えはここで行う（呼び手の順序に
    依存すると、再生成のたびに本文が入れ替わって差分が読めなくなる）。

    **同じ入力から必ず同じ出力になる。**1 日 32 回書き直されるので（§10.7）、
    揺れると Obsidian の同期が毎回差分を運ぶ。
    """
    ordered = sorted(parts, key=lambda p: (p.started_at, p.partkey))

    frontmatter = notes.render_frontmatter(
        {
            "type": NOTE_TYPE,
            notes.SESSION_KEY_FIELD: session_key,
            notes.RECORDING_KEYS_FIELD: [part.partkey for part in ordered],
            "date": day.isoformat(),
            "parts": len(ordered),
            "source": SOURCE_LABEL,
        }
    )

    lines: list[str] = ["", f"# {day.isoformat()} の文字起こし（生データ）", "", _INTRO, ""]
    for part in ordered:
        if cfg.part_boundary_heading:
            lines += [f"{HEADING_PART} {_part_range(part)}", ""]
        lines += _render_segments(part, cfg.timestamp_interval_seconds)

    body = "\n".join(lines).rstrip("\n") + "\n"
    # **本文の行頭 `---` を退避する。**退避しないと W-5 相当の境界判定が
    # 本文の途中で終端を見つけ、frontmatter の範囲を誤認する（§13.4）
    return frontmatter + notes.escape_body(body)


def _part_range(part: RawPart) -> str:
    """`07:12–07:42`。終了時刻が不明なら開始だけを出す。

    `duration_seconds` は ffprobe が失敗すると `NULL` のまま続行する（§10.5）ので、
    **終了時刻が無い Part は実際に存在する。**
    """
    start = part.started_at.strftime("%H:%M")
    if part.ended_at is None:
        return f"{start}–"
    return f"{start}–{part.ended_at.strftime('%H:%M')}"


def _render_segments(part: RawPart, interval_seconds: int) -> list[str]:
    """1 Part の本文。`interval_seconds` ごとに `###` の絶対時刻を差し込む。

    **`0` で見出しを入れない**（§13.3）。終日録音では「録音開始からの経過秒」より
    実時刻のほうが振り返りに使えるため、**絶対時刻**を使う。
    """
    lines: list[str] = []
    next_mark: datetime | None = None
    for segment in part.segments:
        text = segment.text.strip()
        if not text:
            continue
        if interval_seconds > 0 and (next_mark is None or segment.at >= next_mark):
            lines += [f"{HEADING_TIMESTAMP} {segment.at.strftime('%H:%M:%S')}", ""]
            next_mark = segment.at + timedelta(seconds=interval_seconds)
        lines += [text, ""]
    return lines


# --- 保存（§13.6 → §13.7） ---------------------------------------------


def write_raw_note(
    parts: Sequence[RawPart],
    *,
    day: date,
    session_key: str,
    cfg: RawConfig,
    vault_root: Path,
    max_title_bytes: int,
    part_label: str | None = None,
) -> RawNoteResult:
    """レンダリング → atomic write → 保存検証（§13.6 手順 1〜7）。

    **DB 更新と状態遷移は行わない**（#21 の `ensure_raw_note()` が呼び手になる）。
    §13.7 は「`raw_output_path` / `raw_output_sha256` の DB 更新が成功するまで
    `RAW_SAVED` とみなさない」と規定しており、**その判断は呼び手の責務**である。

    Raises:
        OSError: フォルダを作れない（`OBSIDIAN_NOT_FOUND` へ写す）。
        ValueError: 書き込み後の SHA-256 が一致しない（`OBSIDIAN_RAW_WRITE_FAILED`）。
    """
    folder = raw_folder(vault_root, cfg, day)
    Path(folder).mkdir(parents=True, exist_ok=True)

    basename = raw_filename(cfg, day, part=part_label, max_bytes=max_title_bytes)
    path = notes.resolve_output_path(folder, basename, session_key)

    content = render_raw_note(parts, day=day, session_key=session_key, cfg=cfg).encode("utf-8")
    sha256 = notes.atomic_write(path, content)

    verification = notes.verify_note(
        Path(path),
        kind=NoteKind.RAW,
        session_key=session_key,
        expected_sha=sha256,
        expected_keys=[part.partkey for part in parts],
    )
    return RawNoteResult(path=path, sha256=sha256, verification=verification)


def write_raw_notes_per_part(
    parts: Sequence[RawPart],
    *,
    day: date,
    session_key: str,
    cfg: RawConfig,
    vault_root: Path,
    max_title_bytes: int,
) -> list[RawNoteResult]:
    """`granularity: part` のとき、Part ごとに 1 ファイルを書く（§13.3）。

    **ファイル名に `{part}` が要る**（V-27）。含まないと全 Part が同名になり、
    **最後の 1 本だけが残る。**設定検証が起動時に弾くので、ここでは信頼してよい。

    1 年で 11,680 枚になり、Obsidian の検索・起動・同期と §13.8 の Vault インデックス
    構築が重くなる（§13.2）。**既定は `day` である。**
    """
    return [
        write_raw_note(
            [part],
            day=day,
            session_key=session_key,
            cfg=cfg,
            vault_root=vault_root,
            max_title_bytes=max_title_bytes,
            part_label=_part_label(part),
        )
        for part in sorted(parts, key=lambda p: (p.started_at, p.partkey))
    ]


def _part_label(part: RawPart) -> str:
    """ファイル名に入れる Part 識別子。

    **`partkey` をそのまま使わない。**`/` を含むのでパスが割れる（sanitize が `-` へ
    置換するが、70 文字になりファイル名として読めない）。開始時刻で十分に一意である
    （同じ Session 内で同じ秒に始まる Part は物理的に存在しない）。
    """
    return part.started_at.strftime("%H%M%S")
