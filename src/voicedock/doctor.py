"""環境診断（SPEC §19.2）。

**検査は 1 チケット 1 行ずつ増える。**本チケットは D-1（設定）だけを実装し、
レジストリの枠組みを先に置く。「新しい PR をマージしたら doctor の行が 1 本増える」ことを
毎回の動作確認手段にするためである。

出力の書式は §19.2 に規定がある。記号・ラベル桁・続き行の桁をここで一箇所に持つ。
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, Final

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


CHECKS: Final[tuple[Check, ...]] = (
    Check(id="D-1", fatal=True, labels=("Config file", "Config validation"), run=check_config),
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
