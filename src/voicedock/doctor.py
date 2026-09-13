"""環境診断（SPEC §19.2）。

**検査は 1 チケット 1 行ずつ増える。**「新しい PR をマージしたら doctor の行が 1 本増える」
ことを毎回の動作確認手段にしている。現在は D-1（設定）/ D-2（DB）/ D-3（データ領域）/
D-7（whisper）/ D-8・D-9（モデル）。

**番号は詰めない**（§19.2 の方針）。v5.0 で 34 検査から 17 検査へ削ったときも番号を
詰めなかった（v4.6→v5.0 の変更 L-5）ので、D-4〜D-6 は欠番である。

**診断は副作用を持ってはならない。**D-2 は `migrate=False` で接続する — doctor が
スキーマを作ってしまうと「DB が無い」ことを「DB が無い」と報告できなくなる。

出力の書式は §19.2 に規定がある。記号・ラベル桁・続き行の桁をここで一箇所に持つ。
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, Final

from voicedock import db, paths, transcribe
from voicedock.config import (
    ConfigError,
    ConfigUnreadable,
    check_files,
    check_locks,
    config_path,
    key_count,
    load_document,
    parse_config,
)
from voicedock.errors import EXIT_DOCTOR_FATAL, EXIT_OK
from voicedock.heartbeat import DEFAULT_STATE_ROOT


class Status(StrEnum):
    OK = "ok"
    NOTICE = "notice"
    FAIL = "fail"
    SKIP = "skip"


MARKS: Final[Mapping[Status, str]] = {
    Status.OK: "✓",
    Status.NOTICE: "!",
    Status.FAIL: "✗",
    Status.SKIP: "-",
}

HEADER: Final = "VoiceDock doctor"
LABEL_WIDTH: Final = 21
DETAIL_INDENT: Final = 27
SEPARATOR: Final = "─" * 56


@dataclass(frozen=True)
class Row:
    """表示する 1 行と、その続き行。"""

    status: Status
    label: str
    detail: str
    extra: tuple[str, ...] = ()

    def render(self) -> list[str]:
        lines = [f"[{MARKS[self.status]}] {self.label:<{LABEL_WIDTH}}{self.detail}"]
        lines += [" " * DETAIL_INDENT + line for line in self.extra]
        return lines


@dataclass
class Context:
    """検査の間で共有する状態。後続のチケットが `config` を読む。"""

    config_path: Path
    state_root: Path
    config: Any = None
    document: Mapping[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Check:
    id: str
    fatal: bool
    labels: tuple[str, ...]
    """この検査が出す行のラベル。前提が崩れて実行できないときの skip 行に使う。"""

    run: Callable[[Context], list[Row]]


def check_config(ctx: Context) -> list[Row]:
    """D-1: 設定ファイルが存在し、§7.3 の全規則を通る。"""
    try:
        document = load_document(ctx.config_path)
    except ConfigUnreadable as e:
        reason = e.violations[0].message if e.violations else "読み取れません"
        return [
            Row(Status.FAIL, "Config file", f"{ctx.config_path}  （{reason}）"),
            Row(Status.SKIP, "Config validation", "skipped（設定ファイルを読めません）"),
        ]

    ctx.document = document
    keys = key_count(document)
    rows = [Row(Status.OK, "Config file", str(ctx.config_path))]

    cfg, violations = parse_config(document)
    if cfg is not None:
        ctx.config = cfg
        violations += check_files(cfg)
        violations += check_locks(cfg, state_root=ctx.state_root)

    if violations:
        rows.append(
            Row(
                Status.FAIL,
                "Config validation",
                f"{keys} keys, {len(violations)} errors",
                tuple(v.render() for v in violations),
            )
        )
    else:
        rows.append(Row(Status.OK, "Config validation", f"{keys} keys, 0 errors"))
    return rows


MIB: Final = 1024 * 1024
GIB: Final = 1024 * 1024 * 1024


def check_database(ctx: Context) -> list[Row]:
    """D-2: DB へ接続でき、スキーマ版が最新であること。件数とサイズを表示する。"""
    path = Path(ctx.config.database.path)
    if not path.is_file():
        return [Row(Status.FAIL, "Database", f"{path}  （ありません）")]
    try:
        # migrate=False。**doctor はスキーマを作らない**
        with db.connect(
            path,
            busy_timeout_ms=ctx.config.database.busy_timeout_ms,
            tz=ctx.config.tz,
            migrate=False,
        ) as database:
            version = database.schema_version()
            if version != db.SCHEMA_VERSION:
                return [
                    Row(
                        Status.FAIL,
                        "Database",
                        f"{path}  （schema v{version} は未対応。v{db.SCHEMA_VERSION} を期待）",
                    )
                ]
            counts = database.counts()
    except (sqlite3.Error, RuntimeError) as e:
        return [Row(Status.FAIL, "Database", f"{path}  （{e}）")]

    return [
        Row(
            Status.OK,
            "Database",
            f"{path} (schema v{version}, {counts.recordings} recordings, "
            f"{counts.sessions} sessions, {counts.events} events, "
            f"{counts.size_bytes / MIB:.1f} MiB)",
        )
    ]


def check_data_volume(ctx: Context) -> list[Row]:
    """D-3: `/data` が書き込み可能で、空き容量が `free_space_margin_bytes` 以上あること。"""
    root = paths.DATA_ROOT
    if not root.is_dir():
        return [Row(Status.FAIL, "Data volume", f"{root}  （ありません）")]

    # 削除は paths.safe_unlink_tmp を通す（§14.4 N-10）。`.` 始まり + `.tmp` の名前に
    # するのは、その関数が一時ファイルだけを消せるようにしているからである（§13.6）
    probe = root / ".voicedock-doctor-probe.tmp"
    try:
        probe.write_bytes(b"\0")
        paths.safe_unlink_tmp(probe)
    except OSError as e:
        return [Row(Status.FAIL, "Data volume", f"{root}  （書き込めません: {e}）")]

    free = shutil.disk_usage(root).free
    margin = ctx.config.import_.free_space_margin_bytes
    if free < margin:
        return [
            Row(
                Status.FAIL,
                "Data volume",
                f"{root} writable, {free / GIB:.1f} GiB free "
                f"（free_space_margin_bytes の {margin / GIB:.1f} GiB を下回る）",
            )
        ]
    return [Row(Status.OK, "Data volume", f"{root} writable, {free / GIB:.1f} GiB free")]


def check_whisper_executable(ctx: Context) -> list[Row]:
    """D-7: `whisper-cli` が存在し `--help` が成功する。**VAD オプションの有無を表示。**

    §10.6 の注記が「`WHISPER_CPP_REF` を上げる際は、このフラグ群が残っていることを
    D-7 で確認する」と指定している。**バージョン文字列では判定しない** —
    VAD はビルド設定で落ちうる。
    """
    found = transcribe.is_available(ctx.config.transcription)
    if not found.ok:
        return [Row(Status.FAIL, "Whisper executable", found.detail)]
    vad = "supported" if found.vad_supported else "NOT supported"
    status = Status.OK if found.vad_supported else Status.NOTICE
    return [Row(status, "Whisper executable", f"{found.detail} (VAD: {vad})")]


def check_whisper_model(ctx: Context) -> list[Row]:
    """D-8: Whisper モデルが存在し、サイズが 0 でない。"""
    return [_model_row("Whisper model", Path(ctx.config.transcription.model))]


def check_vad_model(ctx: Context) -> list[Row]:
    """D-9: VAD モデルが存在する（`vad.enabled` が true のとき）。

    **無効なら skip にする。**`vad.enabled: false` は §10.6 が認めた運用であり、
    そのときモデルが無いことは異常ではない。
    """
    vad = ctx.config.transcription.vad
    if not vad.enabled:
        return [Row(Status.SKIP, "VAD model", "transcription.vad.enabled: false")]
    return [_model_row("VAD model", Path(vad.model))]


def _model_row(label: str, path: Path) -> Row:
    """モデルファイル 1 件の行。**サイズ 0 を「在る」と報告しない** —
    取得が途中で止まったファイルは存在するが使えない。
    """
    if not path.is_file():
        return Row(Status.FAIL, label, f"{path}  （ありません）")
    size = path.stat().st_size
    if size == 0:
        return Row(Status.FAIL, label, f"{path.name}  （0 バイト。取得が途中で止まっている）")
    return Row(Status.OK, label, f"{path.name} ({size / MIB:.1f} MiB)")


CHECKS: Final[tuple[Check, ...]] = (
    Check(id="D-1", fatal=True, labels=("Config file", "Config validation"), run=check_config),
    Check(id="D-2", fatal=True, labels=("Database",), run=check_database),
    Check(id="D-3", fatal=True, labels=("Data volume",), run=check_data_volume),
    Check(
        id="D-7",
        fatal=True,
        labels=("Whisper executable",),
        run=check_whisper_executable,
    ),
    Check(id="D-8", fatal=True, labels=("Whisper model",), run=check_whisper_model),
    Check(id="D-9", fatal=True, labels=("VAD model",), run=check_vad_model),
)


def run(
    stream: IO[str] | None = None,
    *,
    path: Path | None = None,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> int:
    """全検査を実行して表示する。戻り値がプロセスの終了コードになる（§17.3）。"""
    out = stream if stream is not None else sys.stdout
    ctx = Context(config_path=path if path is not None else config_path(), state_root=state_root)

    rows: list[Row] = []
    blocked = False
    for check in CHECKS:
        if blocked:
            rows += [
                Row(Status.SKIP, label, "skipped（先行する致命的な検査が失敗）")
                for label in check.labels
            ]
            continue
        try:
            produced = check.run(ctx)
        except ConfigError:  # pragma: no cover - 検査は自分で ConfigError を処理する
            raise
        rows += produced
        if check.fatal and any(r.status is Status.FAIL for r in produced):
            blocked = True

    _write(out, rows)
    return EXIT_DOCTOR_FATAL if any(r.status is Status.FAIL for r in rows) else EXIT_OK


def _write(out: IO[str], rows: Sequence[Row]) -> None:
    lines = [HEADER, SEPARATOR]
    for row in rows:
        lines += row.render()
    lines.append(SEPARATOR)
    lines.append(_summary(rows))
    out.write("\n".join(lines) + "\n")
    out.flush()


def _summary(rows: Sequence[Row]) -> str:
    """§19.2 のサマリ。`[-]`（skip）は数えない（既に数えた失敗の結果だから）。"""
    passed = sum(1 for r in rows if r.status is Status.OK)
    failed = sum(1 for r in rows if r.status is Status.FAIL)
    notices = sum(1 for r in rows if r.status is Status.NOTICE)
    return f"{passed} checks passed, {failed} failed, {notices} notices"
