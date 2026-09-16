"""Daily ノート（整理済み）のレンダリングと保存（SPEC §13.4, §13.7, §10.10, §10.11）。

**セクションの有無と順序は `llm.analysis.order` と `sections.<key>.enabled` に従う**
（§7.2 / §13.4）。#25 の「真実の出所を config に一本化する」がここまで貫かれる —
**切ったセクションはプロンプトにも載らず、スキーマにも入らず、ノートにも出ない。**

**欠落を隠さない**（§13.4）。統合から外れた Part は frontmatter の
`voicedock_failed_parts`（`FAILED`）と `voicedock_skipped_parts`（`SKIPPED`）に**分けて**
列挙し、本文の見出し直下に警告行を出す。**2 つを混ぜてはならない** — `FAILED` は自動で
戻るが `SKIPPED` は戻らないので、**同じ欄に入れると読み手が区別できない**（#140）。

**除外された Part の音声は削除されない** — §14.1 が `voicedock_recording_keys` への
収録を要求するため自動的に保証される。

書き込みと検証は `notes.py` を再利用する（§13.6 / §13.7）。**検証ロジックを 2 本に
分けない** — #36 の `cleaner.py` も同じ `verify_note()` を呼ぶ。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from voicedock import notes, raw, wiki
from voicedock.config import Config
from voicedock.errors import ErrorCode
from voicedock.llm import Chunk
from voicedock.notes import CheckResult, NoteKind
from voicedock.paths import PartKey, SessionKey, VaultPath
from voicedock.session import SessionTranscript
from voicedock.states import PartStatus

NOTE_TYPE: Final = "voice-daily"
STATUS_PROCESSED: Final = "processed"
FAILED_PARTS_FIELD: Final = "voicedock_failed_parts"
SKIPPED_PARTS_FIELD: Final = "voicedock_skipped_parts"
TIMELINE_KEY: Final = "timeline"
SUMMARY_KEY: Final = "summary"
TASKS_KEY: Final = "tasks"
DUE_MARK: Final = "📅"

HEADING_BLOCK: Final = "###"
"""Timeline の Block 見出し（§13.4 の例が `### 07:12–11:30`）。"""


BENIGN_SKIP_REASONS: Final[frozenset[str]] = frozenset(
    {ErrorCode.NO_SPEECH_DETECTED, ErrorCode.DUPLICATE_CONTENT}
)
"""**利用者がすることが無い**除外理由（§13.4 / #140）。

§10.6 は `NO_SPEECH_DETECTED` を「**失敗ではない**」と明記しており、
`DUPLICATE_CONTENT` も同じく正常な動作である。この 2 つだけなら警告記号を付けない。

**許可リストである。**ここに無い理由は「利用者の操作が要る」側に倒す — 知らない理由を
正常扱いすると、**本当に欠けている記録が静かに見過ごされる**（#133 の設計判断 4 と同じ）。
"""

SKIP_REASON_LABELS: Final[dict[str, str]] = {
    str(ErrorCode.DUPLICATE_CONTENT): "重複",
    str(ErrorCode.SOURCE_MISSING): "元ファイルが見つかりません",
    str(ErrorCode.NORMALIZED_MISSING): "元ファイルが見つかりません",
    str(ErrorCode.NO_SPEECH_DETECTED): "無音",
}
"""除外理由の日本語。**知らないコードはコードのまま出す**（隠すより読めるほうがよい）。"""

RETRY_ACTION: Final = "デバイスから採り直してください。"
"""**次に何をすればよいかを書く**（§16.4 の方針）。`⚠` だけでは伝わらない。"""


@dataclass(frozen=True)
class ExcludedPart:
    """統合から外れた Part（§10.8 `EXCLUDED_FROM_MERGE`）。

    **`status` と `error_code` の両方を持つ。**`FAILED` か `SKIPPED` かで
    「自動で戻るか」が決まり、`error_code` で「利用者の操作が要るか」が決まる。
    """

    partkey: PartKey
    status: str
    error_code: str | None = None


@dataclass(frozen=True)
class TimelineBlock:
    """Timeline の 1 ブロック（§13.4）。

    **時刻は Map 中間結果とチャンクの時刻範囲から来る。LLM には出力させない**
    （§12.4 / §13.4。幻覚を避ける）。
    """

    start_at: datetime
    end_at: datetime
    lines: tuple[str, ...]

    def render(self) -> list[str]:
        span = f"{self.start_at.strftime('%H:%M')}–{self.end_at.strftime('%H:%M')}"
        return [f"{HEADING_BLOCK} {span}", "", *[f"- {line}" for line in self.lines], ""]


@dataclass(frozen=True)
class DailyNoteResult:
    """`write_daily_note()` の結果。**状態遷移と DB 更新は呼び手が行う**（#21 / §13.7）。"""

    path: VaultPath
    sha256: str
    verification: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        """W-1〜W-9 をすべて満たしたか（§13.7）。"""
        return notes.all_passed(self.verification)

    @property
    def failed_rules(self) -> tuple[str, ...]:
        return notes.failed_rules(self.verification)


# --- Timeline（§13.4 / §12.4） ------------------------------------------


def build_timeline(
    *,
    partials: Sequence[Any],
    chunks: Sequence[Chunk],
    transcript: SessionTranscript,
    summary: str,
) -> list[TimelineBlock]:
    """Timeline のブロックを組み立てる（§13.4）。**追加の LLM 呼び出しは不要。**

    Map 中間結果があれば、それを時刻順に並べる。**単一パス処理（§12.4）で Map 中間結果が
    無い場合は、セッション全体を Block ごとに 1 ブロックとして扱い、見出しはその Block の
    時刻範囲、本文は `summary` の各文を箇条書きにしたものとする**
    （**Timeline のためだけに LLM を再度呼ばない**）。
    """
    if partials and chunks:
        return [
            TimelineBlock(
                start_at=chunk.start_at,
                end_at=chunk.end_at,
                lines=tuple(_points(partial)),
            )
            for partial, chunk in zip(partials, chunks, strict=False)
            if _points(partial)
        ]

    sentences = tuple(_sentences(summary))
    if not sentences:
        return []
    blocks = transcript.blocks or _fallback_block(transcript)
    return [TimelineBlock(start_at=start, end_at=end, lines=sentences) for start, end in blocks]


def _fallback_block(transcript: SessionTranscript) -> list[tuple[datetime, datetime]]:
    """Block が空でも Timeline を出せるようにする（**見出しの時刻が無いと読めない**）。"""
    if not transcript.segments:
        return []
    return [(transcript.segments[0].at, max(s.end_at for s in transcript.segments))]


def _points(partial: object) -> list[str]:
    """1 つの `PartialAnalysis` から Timeline の行を作る。

    **`key_points` を優先し、無ければ `summary` を使う。**Map 段の `summary` は
    そのチャンクの要約なので、箇条書き 1 行として十分に意味を持つ。
    """
    points = getattr(partial, "key_points", None)
    if isinstance(points, list) and points:
        return [str(point) for point in points]
    summary = getattr(partial, "summary", "")
    return _sentences(str(summary))


def _sentences(text: str) -> list[str]:
    """`summary` を文へ割る（§13.4 の「`summary` の各文を箇条書きに」）。

    **句点で割る。**日本語の文末は `。` であり、`.` で割ると小数や英略語で切れる。
    """
    parts = [piece.strip() for piece in text.replace("。", "。\n").splitlines()]
    return [piece for piece in parts if piece]


# --- レンダリング（§13.4） ----------------------------------------------


def render_daily_note(
    analysis: object,
    *,
    day: date,
    session_key: SessionKey,
    recording_keys: Sequence[PartKey],
    excluded: Sequence[ExcludedPart],
    recorded_seconds: float | None,
    block_count: int,
    timeline: Sequence[TimelineBlock],
    links: wiki.LinkPlan,
    cfg: Config,
) -> str:
    """Daily ノートの Markdown（§13.4）。

    **`voicedock_session_key` と `voicedock_recording_keys` は必須である**（§13.7 の
    W-6 / W-7 と §14.1 の削除条件が見る）。
    """
    sections = cfg.llm.analysis
    tags = _tags(analysis, cfg)
    # **`FAILED` と `SKIPPED` を分ける**（#140）。前者は自動で戻り、後者は戻らない
    failed = [part for part in excluded if part.status == PartStatus.FAILED]
    skipped = [part for part in excluded if part.status != PartStatus.FAILED]

    frontmatter = notes.render_frontmatter(
        {
            "type": NOTE_TYPE,
            notes.SESSION_KEY_FIELD: session_key,
            notes.RECORDING_KEYS_FIELD: list(recording_keys),
            FAILED_PARTS_FIELD: [part.partkey for part in failed],
            SKIPPED_PARTS_FIELD: [part.partkey for part in skipped],
            "date": day.isoformat(),
            "recorded": _duration(recorded_seconds),
            "parts": len(recording_keys),
            "blocks": block_count,
            "status": STATUS_PROCESSED,
            "tags": tags,
        }
    )

    title = str(getattr(analysis, "title", "") or day.isoformat())
    lines: list[str] = ["", f"# {title}", ""]
    for line in _warnings(failed, skipped):
        lines += [line, ""]

    for name in sections.order:
        section = sections.sections.get(name)
        if section is None or not section.enabled:
            continue
        heading = section.heading or f"## {name}"
        if name == TIMELINE_KEY:
            rendered = _timeline_lines(timeline)
        else:
            rendered = _section_lines(name, analysis)
        if not rendered:
            # **空セクションは見出しごと省略する**（§13.4）
            continue
        lines += [heading, "", *rendered]

    lines += _sources(links)
    lines += _links(links)

    body = "\n".join(lines).rstrip("\n") + "\n"
    return frontmatter + notes.escape_body(body)


def _timeline_lines(timeline: Sequence[TimelineBlock]) -> list[str]:
    rendered: list[str] = []
    for block in timeline:
        rendered += block.render()
    return rendered


def _section_lines(name: str, analysis: object) -> list[str]:
    values = getattr(analysis, name, None)
    if name == SUMMARY_KEY:
        text = str(values or "").strip()
        return [text, ""] if text else []
    if not isinstance(values, list) or not values:
        return []
    if name == TASKS_KEY:
        return [*[_task(task) for task in values], ""]
    return [*[f"- {value}" for value in values], ""]


def _task(task: object) -> str:
    """`- [ ] {text}`。`due` があれば ` 📅 {due}` を付加（§13.4）。"""
    text = str(getattr(task, "text", task))
    due = getattr(task, "due", None)
    return f"- [ ] {text} {DUE_MARK} {due}" if due else f"- [ ] {text}"


def _sources(links: wiki.LinkPlan) -> list[str]:
    """`## Sources` から Raw ノートへ辿る（§13.4 / §13.8）。

    **全文は載せない**（`wiki.include_transcript` 既定 false）。W-9 がこのリンクを見る。
    """
    if not links.raw:
        return []
    return ["## Sources", "", *[f"- {link}" for link in links.raw], ""]


def _links(links: wiki.LinkPlan) -> list[str]:
    values = [value for value in (links.daily_note, *links.adjacent) if value]
    values += [value for value in links.tags if value.startswith("[[")]
    if not values:
        return []
    return ["## Links", "", *[f"- {value}" for value in values], ""]


def _warnings(failed: Sequence[ExcludedPart], skipped: Sequence[ExcludedPart]) -> list[str]:
    """**欠落を隠さない**（§13.4）。見出し直後に最大 2 行入れる。

    **`FAILED` と `SKIPPED` で別の行にする**（#140）。1 行に混ぜて条件分岐した文言を
    作ると、読みにくいうえにテストで落としにくい。

    **`SKIPPED` に「自動で再試行されます」と書いてはならない。**`SKIPPED` は終端であり、
    §10.2 の墓標があるのでデバイスからの再コピーも起きない。**ノートが、起きないことを
    約束することになる。**

    **除外された Part の音声は削除されない** — §14.1 が `voicedock_recording_keys` への
    収録を要求するため自動的に保証される。
    """
    lines: list[str] = []
    if failed:
        lines.append(
            f"> ⚠ この日の録音のうち {len(failed)} 本が処理できませんでした。"
            "次にデバイスを接続したときに自動で再試行されます。"
        )
    if skipped:
        # **許可リストで判定する。**知らない理由は「操作が要る」側へ倒す
        actionable = any(part.error_code not in BENIGN_SKIP_REASONS for part in skipped)
        mark = "⚠ " if actionable else ""
        action = RETRY_ACTION if actionable else ""
        lines.append(
            f"> {mark}この日の録音のうち {len(skipped)} 本を除外しました"
            f"（{_skip_reasons(skipped)}）。自動では再試行されません。{action}"
        )
    return lines


def _skip_reasons(skipped: Sequence[ExcludedPart]) -> str:
    """除外理由を重複なく並べる。**並びは §15.1 の表の順**（`ErrorCode` の宣言順）。

    **並びを出現順にしない。**同じ組み合わせのノートが日によって違う文面になり、
    再生成の冪等性（§9.4 の `output_sha256` 一致）が Part の処理順に左右される。
    """
    order = {str(code): index for index, code in enumerate(ErrorCode)}
    seen = {part.error_code or "" for part in skipped}
    ordered = sorted(seen, key=lambda code: order.get(code, len(order)))
    return "・".join(SKIP_REASON_LABELS.get(code) or code or "理由不明" for code in ordered)


def _tags(analysis: object, cfg: Config) -> list[str]:
    """`obsidian.default_tags` + LLM の `tags` を結合し重複除去（§13.4）。

    **空白を `-` へ置換する。**Obsidian のタグは空白で切れるので、`VoiceDock 設計` が
    2 つのタグになる。
    """
    values = [*cfg.obsidian.default_tags]
    extra = getattr(analysis, "tags", None)
    if isinstance(extra, list):
        values += [str(tag) for tag in extra]
    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        cleaned = value.strip().replace(" ", "-").replace("　", "-")
        if not cleaned or cleaned.casefold() in seen:
            continue
        seen.add(cleaned.casefold())
        kept.append(cleaned)
    return kept


def _duration(seconds: float | None) -> str:
    """`recorded` は実録音時間の合計（`HH:MM:SS`。§13.4）。"""
    if seconds is None or seconds < 0:
        return "00:00:00"
    total = int(seconds)
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


# --- パスと保存（§13.5 / §13.6 → §13.7） -------------------------------


def daily_folder(vault_root: Path, cfg: Config, day: date) -> VaultPath:
    return VaultPath(vault_root / raw.render_template(cfg.obsidian.wiki.folder_template, day))


def daily_filename(cfg: Config, day: date) -> str:
    """`.md` を除いたファイル名。**`{title}` を入れてはならない**（V-14 が起動時に弾く）。

    LLM が生成した題をファイル名にすると、**再生成のたびに別のファイルができる。**
    """
    return notes.sanitize_filename(
        raw.render_template(cfg.obsidian.wiki.filename_template, day),
        max_bytes=cfg.obsidian.max_title_bytes,
    )


def write_daily_note(
    content: str,
    *,
    day: date,
    session_key: SessionKey,
    recording_keys: Sequence[PartKey],
    cfg: Config,
    vault_root: Path,
) -> DailyNoteResult:
    """レンダリング済みの本文を書き、保存検証する（§13.6 → §13.7）。

    **DB 更新と状態遷移は行わない**（#21 の `ensure_daily_note()` が呼び手）。§13.7 は
    「`output_path` / `output_sha256` の DB 更新が成功するまで `SAVED` とみなさない」と
    規定しており、**その判断は呼び手の責務**である。
    """
    folder = daily_folder(vault_root, cfg, day)
    Path(folder).mkdir(parents=True, exist_ok=True)
    path = notes.resolve_output_path(folder, daily_filename(cfg, day), session_key)

    sha256 = notes.atomic_write(path, content.encode("utf-8"))
    verification = notes.verify_note(
        Path(path),
        kind=NoteKind.DAILY,
        session_key=session_key,
        expected_sha=sha256,
        expected_keys=list(recording_keys),
        summary_heading=_summary_heading(cfg),
        require_raw_link=not cfg.obsidian.wiki.include_transcript,
    )
    return DailyNoteResult(path=path, sha256=sha256, verification=verification)


def _summary_heading(cfg: Config) -> str:
    section = cfg.llm.analysis.sections.get(SUMMARY_KEY)
    if section is not None and section.heading:
        return section.heading
    return notes.DEFAULT_SUMMARY_HEADING


def raw_note_names(session_key: SessionKey, cfg: Config, day: date) -> list[str]:
    """`## Sources` から辿る Raw ノートの basename（§13.8 の `link_raw`）。

    **常に 1 本である。**Raw ノートは日単位で 1 ファイルに書き直される（§13.3）。
    `granularity: part` は v5.48 で廃止した（変更 BJ-1）。
    """
    return [raw.raw_filename(cfg.obsidian.raw, day, max_bytes=cfg.obsidian.max_title_bytes)]


# --- Map 中間結果の保存（§10.9。v5.5→v5.6 の変更 R-1） ------------------


def timeline_path(analysis_path: Path) -> Path:
    """`<analysis_path>` の隣の `<slug>.timeline.json`（§10.9）。"""
    return analysis_path.with_suffix(".timeline.json")


TIMELINE_SCHEMA: Final = 2
"""`<slug>.timeline.json` の形式版。**1 は指紋を持たない**（v5.18 以前）。"""


def save_timeline(
    analysis_path: Path, blocks: Sequence[TimelineBlock], *, fingerprint: str
) -> None:
    """Map 中間結果を保存する（§10.9）。**失敗しても例外にしない。**

    §13.4 の Timeline はこれを時刻順に並べたものであり、**再生成のために 18 回の
    LLM 呼び出しをやり直すわけにはいかない。**

    **入力の指紋を併記する**（§9.4）。この関数は `OSError` を握りつぶすので、
    **失敗すると古い Timeline がそのまま残る。**解析本体だけが新しくなり、
    `load_timeline()` が古い Timeline を返すと、**本文が古いノートができる**（#108）。
    指紋があれば `load_timeline()` がそれを弾ける。
    """
    payload = [
        {
            "start_at": block.start_at.isoformat(timespec="seconds"),
            "end_at": block.end_at.isoformat(timespec="seconds"),
            "lines": list(block.lines),
        }
        for block in blocks
    ]
    document = {
        "schema": TIMELINE_SCHEMA,
        "transcript_sha256": fingerprint,
        "blocks": payload,
    }
    try:
        target = timeline_path(analysis_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        return


def load_timeline(analysis_path: Path, *, fingerprint: str) -> list[TimelineBlock]:
    """保存済みの Map 中間結果を読む。**読めなければ空**（§10.9 の代替経路へ落ちる）。

    **指紋が一致しないものは「無い」と同じ扱いにする**（§9.4）。`save_timeline()` は
    書き込みの失敗を握りつぶすので、**解析だけが新しく Timeline が古い**状態が起こりうる。
    代替経路（Block ごとに `summary` の各文）は transcript から作るので**必ず今の内容になる** —
    粒度は粗くなるが、**古い時間帯を載せたノートよりは良い。**

    形式版 1（v5.18 以前。指紋を持たない配列）も一致しない扱いとする。
    """
    try:
        document = json.loads(timeline_path(analysis_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(document, dict) or document.get("schema") != TIMELINE_SCHEMA:
        return []
    if document.get("transcript_sha256") != fingerprint:
        return []
    entries = document.get("blocks")
    if not isinstance(entries, list):
        return []
    blocks: list[TimelineBlock] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            start = datetime.fromisoformat(str(entry["start_at"]))
            end = datetime.fromisoformat(str(entry["end_at"]))
        except (KeyError, ValueError):
            continue
        lines = entry.get("lines")
        if not isinstance(lines, list):
            continue
        blocks.append(
            TimelineBlock(start_at=start, end_at=end, lines=tuple(str(line) for line in lines))
        )
    return blocks
