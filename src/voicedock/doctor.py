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

import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, Final

from voicedock import db, device, llm, paths, raw, status, transcribe
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
from voicedock.heartbeat import DEFAULT_STATE_ROOT, read_heartbeat


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
    """検査の間で共有する状態。"""

    config_path: Path
    state_root: Path
    config: Any = None
    document: Mapping[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    endpoint: Any = None
    """D-11 が解決したエンドポイント。**D-12 が使い回す**（env を 2 回読まない）。"""

    now: datetime | None = None
    """時刻の注入口。**テストが固定する**（D-18 の鮮度判定が現在時刻に依存する）。"""


@dataclass(frozen=True)
class Check:
    id: str
    fatal: bool
    labels: tuple[str, ...]
    """この検査が出す行のラベル。前提が崩れて実行できないときの skip 行に使う。"""

    run: Callable[[Context], list[Row]]

    always: bool = False
    """先行する致命的な検査が失敗しても実行するか。

    **D-17（削除モードの表示）だけが真である。**§19.2 は「削除モードの状態を**必ず**
    表示する」と規定しており、**Helper の heartbeat が無いときこそ見たい行**である
    （不明は安全側へ倒すが、それが表示されないと「読めなかった」のか
    「無効だった」のか分からない）。設定が読めていること（D-1 の通過）だけは前提にする。
    """


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


TOOL_TIMEOUT_SECONDS: Final = 30

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

    **無効なら警告する。**v5.16 までは黙って skip していたが、2026-09-14 の実測で
    **VAD を切ると破滅的である**ことが分かった（`docs/POC.md` §12.4）。同じ音声で:

    | | `elapsed` | `rtf` | 文字数 | 区間 / ユニーク |
    |---|---|---|---|---|
    | VAD on | 209.6 秒 | 0.134 | 667 | 40 / 39 |
    | **VAD off** | **2819.3 秒** | **1.806** | **2274** | **178 / 11** |

    **13.4 倍遅くなり、文字数が 3.4 倍に増える。**増えた分は無音から生成された幻覚で、
    178 区間のうち 74 区間が「はい、ご視聴ありがとうございました。」の繰り返しだった
    （§22 R-15）。**`rtf 1.806` では 16 時間の録音に 29 時間かかり、
    §21.2 Phase 2 の 8 時間要件を満たせない。**

    **設定としては許すが、黙っては通さない。**「遅い」だけでなく
    **嘘の記録が Vault に残る**ためである（§1.3 の優先順位 1）。
    """
    vad = ctx.config.transcription.vad
    if not vad.enabled:
        return [
            Row(
                Status.NOTICE,
                "VAD model",
                "transcription.vad.enabled: false"
                "  <- 無音から幻覚が生成され、13 倍以上遅くなります",
            )
        ]
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


def check_ffmpeg(ctx: Context) -> list[Row]:
    """D-10: `ffmpeg` / `ffprobe` が存在しバージョンを取得できる。

    **両方を 1 行で見る。**§19.2 の例が `ffmpeg / ffprobe  7.1` の 1 行なのは、
    片方だけ在る状態が実際には起きない（同じパッケージから入る）ためである。
    """
    audio_cfg = ctx.config.audio
    versions: list[str] = []
    for label, path in (("ffmpeg", Path(audio_cfg.ffmpeg)), ("ffprobe", Path(audio_cfg.ffprobe))):
        found = _tool_version(path)
        if found is None:
            return [Row(Status.FAIL, "ffmpeg / ffprobe", f"{label}: {path} を実行できません")]
        versions.append(found)
    # 版が食い違うのは異常（別の場所から入っている）。**黙って片方だけ出さない**
    detail = versions[0] if versions[0] == versions[1] else " / ".join(versions)
    return [Row(Status.OK, "ffmpeg / ffprobe", detail)]


def _tool_version(path: Path) -> str | None:
    """`-version` の 1 行目からバージョン文字列を取る。取れなければ `None`。"""
    if not path.is_file():
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - argv は固定、パスは設定検証済み
            [str(path), "-version"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=TOOL_TIMEOUT_SECONDS,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    matched = re.search(r"version\s+(\S+)", completed.stdout)
    return matched.group(1) if matched else completed.stdout.splitlines()[0][:40]


def check_llm_endpoint(ctx: Context) -> list[Row]:
    """D-11: `VOICEDOCK_LLM_URL` / `VOICEDOCK_LLM_MODEL` が設定済み。

    **値そのものは表示しない方針にしない。**URL はローカルのエンドポイントであり
    §16.3 が遮断する「本文」ではない。**どこへ繋ごうとしているかが見えないと
    切り分けができない。**
    """
    endpoint = llm.endpoint_of(ctx.config)
    if endpoint is None:
        return [
            Row(
                Status.FAIL,
                "LLM endpoint",
                f"{ctx.config.llm.endpoint_env} / {ctx.config.llm.model_env} が未設定"
                "（§18.2 の models 長構文が注入する）",
            )
        ]
    ctx.endpoint = endpoint
    return [Row(Status.OK, "LLM endpoint", endpoint.base_url)]


def check_llm_response(ctx: Context) -> list[Row]:
    """D-12: **LLM へ実際に短いリクエストを投げて応答を得る**（所要時間を表示）。

    **状態の照会ではなく疎通を見る。**`docker model status` が running でも
    モデルがロードされていないことがある（§19.2 の DH-2 の注記）。

    D-11 が失敗していれば実行しない（`endpoint` が無い）。
    """
    if ctx.endpoint is None:
        return [Row(Status.SKIP, "LLM response", "skipped（LLM endpoint が未設定）")]
    found = llm.probe(ctx.config, endpoint=ctx.endpoint)
    if not found.ok:
        return [Row(Status.FAIL, "LLM response", f"{ctx.endpoint.model}  ({found.detail})")]
    tokens = "? tokens" if found.total_tokens is None else f"{found.total_tokens} tokens"
    return [
        Row(
            Status.OK,
            "LLM response",
            f"{found.model}  ({found.elapsed_seconds:.1f}s, {tokens})",
        )
    ]


def check_vault(ctx: Context) -> list[Row]:
    """D-13: `/obsidian` が読み書き可能で、Raw / Daily の出力フォルダが作成可能（§19.2）。

    **v5.0 で D-14 を統合したので 1 行で両方を見る。**

    **フォルダを作らない。**テンプレートは `{yyyymmdd}` を含むので、doctor を走らせる
    たびに空の日付フォルダが Vault へ増える。代わりに**存在する最も深い祖先**へ
    一時ファイルを書いて「作成できる」ことを確かめる（§19.2 の「存在するか作成できる」）。
    """
    root = Path(ctx.config.obsidian.root)
    if not root.is_dir():
        return [Row(Status.FAIL, "Obsidian vault", f"{root}  （ありません）")]
    writable = _probe_write(root)
    if writable is not None:
        return [Row(Status.FAIL, "Obsidian vault", f"{root}  （書き込めません: {writable}）")]

    day = (ctx.now or datetime.now(ctx.config.tz)).date()
    for label, template in (
        ("raw", ctx.config.obsidian.raw.folder_template),
        ("wiki", ctx.config.obsidian.wiki.folder_template),
    ):
        folder = root / raw.render_template(template, day)
        problem = _probe_write(_deepest_existing(folder, root))
        if problem is not None:
            return [
                Row(
                    Status.FAIL,
                    "Obsidian vault",
                    f"{root}  （{label} の出力フォルダ {folder} を作れません: {problem}）",
                )
            ]
    return [Row(Status.OK, "Obsidian vault", f"{root} (readable, writable, folders exist)")]


def _deepest_existing(folder: Path, root: Path) -> Path:
    """`folder` の祖先のうち実在する最も深いもの。**`root` より上へは遡らない。**"""
    current = folder
    while current != root and not current.is_dir():
        current = current.parent
    return current


def _probe_write(folder: Path) -> str | None:
    """一時ファイルを作って消す。成功なら `None`、失敗なら理由。

    削除は `paths.safe_unlink_tmp()` を通す（§14.4 N-10）。`.` 始まり + `.tmp` の名前に
    するのは、その関数が一時ファイルだけを消せるようにしているからである（§13.6）。
    """
    probe_path = folder / ".voicedock-doctor-probe.tmp"
    try:
        probe_path.write_bytes(b"\0")
        paths.safe_unlink_tmp(probe_path)
    except (OSError, ValueError) as e:
        return str(e)
    return None


def check_helper(ctx: Context) -> list[Row]:
    """D-18: **Helper が稼働している**（§19.2 / §7.5）。

    気づけないと「新しい録音が無い」と解釈して静かに待つことになる（§22 R-23）。
    `mount_readonly` / バージョン / `config_error` / `INCLUDE_VOLUMES` /
    `EXCLUDE_VOLUMES` の実効値を表示する。
    """
    beat = read_heartbeat(ctx.state_root)
    if beat is None:
        return [
            Row(
                Status.FAIL,
                "Helper",
                f"{ctx.state_root / 'heartbeat.json'} がありません"
                "（helper/install.sh で LaunchAgent を配置する。§3.4(6)）",
            )
        ]
    moment = ctx.now or datetime.now(ctx.config.tz)
    age = beat.age_seconds(moment)
    limit = ctx.config.import_.helper_heartbeat_max_age_seconds
    seen = "不明" if age is None else f"{age:.0f}s ago"
    # **`None` を `writable` に丸めない**（#107）。Helper が値を書けなかったことと、
    # デバイスが書き込み可能であることは別の事実である
    mount = status.mount_text(beat.mount_readonly)
    detail = f"(last seen {seen}, v{beat.helper_version or '?'}, mount={mount})"

    extra = [
        f"INCLUDE_VOLUMES: {list(beat.include_volumes) or '(すべて)'}",
        f"EXCLUDE_VOLUMES: {list(beat.exclude_volumes)}",
    ]
    if beat.config_error:
        # **`config_error` は致命的に扱う。**helper.conf が検証を通っていない間、
        # Helper は 1 本も取り込まない（§7.4）
        return [
            Row(
                Status.FAIL,
                "Helper",
                f"config_error: {beat.config_error}",
                tuple(extra),
            )
        ]
    if beat.is_stale(moment, limit):
        return [Row(Status.FAIL, "Helper", f"stale {detail}  （上限 {limit} 秒）", tuple(extra))]
    return [Row(Status.OK, "Helper", f"running {detail}", tuple(extra))]


def check_inbox_orphans(ctx: Context) -> list[Row]:
    """D-20: **inbox に取り残しが無い**（§10.2 / #120）。

    partkey が終端状態（`FAILED` を除く）の原本が inbox に残っていると、
    **二度と処理されないのにディスクを使い続ける。**`inbox_retain` の削除は変換の後に
    行われるので、**変換しない Part の原本は誰も消さない。**

    **`FAIL` にしない。**記録が失われているわけではなく、ディスクを使っているだけである。
    **自動で消しもしない** — inbox は Helper が書く領域であり、コンテナが消してよいのは
    §10.5 の `inbox_retain` の経路だけである（§14.4 の封じ込めと同じ考え方）。
    **見えるようにして、消す判断は利用者に委ねる。**
    """
    root = Path(ctx.config.import_.inbox_root)
    if not root.is_dir():
        return [Row(Status.SKIP, "Inbox orphans", f"{root} がありません")]

    orphaned = status.orphaned_partkeys(ctx.config)
    if orphaned is None:
        return [Row(Status.SKIP, "Inbox orphans", "DB を読めません")]

    count = 0
    total = 0
    for candidate in device.list_inbox_parts(root, tz=ctx.config.tz).parts:
        if candidate.partkey in orphaned:
            count += 1
            total += candidate.source.size
    if count == 0:
        return [Row(Status.OK, "Inbox orphans", "none")]
    return [
        Row(
            Status.NOTICE,
            "Inbox orphans",
            f"{count} files, {total / 1024 / 1024 / 1024:.1f} GiB",
            (
                "処理済みの Part の原本が inbox に残っている。**二度と処理されない**",
                "`inbox_retain` の経路では消えない。**消すかどうかは利用者が決める**",
            ),
        )
    ]


def check_source_deletion(ctx: Context) -> list[Row]:
    """D-17: **削除モードの状態を必ず表示する。三重ロックの 3 つすべてを個別に**（§19.2）。

    **どれが効いているかが見えないと Phase 7 の移行判断ができない。**

    判定は `status.Locks` を使う。**ここで論理式を書き直さない** — §14.2 の
    「削除が起こりうるか」を決める場所が 2 つになると、片方だけ直したときに
    **doctor が DISABLED と言いながら実際には削除される。**
    """
    beat = read_heartbeat(ctx.state_root)
    locks = status.Locks(
        config_delete=ctx.config.cleanup.delete_source_audio,
        helper_delete=beat.delete_source_audio if beat else None,
        reaper_installed=beat.reaper_installed if beat else None,
        mount_readonly=beat.mount_readonly if beat else None,
    )
    mount_mode = (beat.mount_mode if beat else None) or "?"
    lines = [
        f"  lock 1  : config.yaml={_flag(locks.config_delete)}, "
        f"helper.conf={_flag(locks.helper_delete)}",
        f"  lock 2-A: {_reaper_line(locks.reaper_installed)}",
        f"  lock 2-B: MOUNT_MODE={mount_mode} ({_mount_line(locks.mount_readonly)})",
    ]
    if locks.enabled:
        lines += [
            "WARNING: Source deletion is ENABLED. Verified recordings will be",
            "         permanently removed from the DJI Mic 3 after saving.",
            "NOTE: Deletion is executed by the host-side reaper, not by this container.",
            "      The reaper re-verifies every request independently (§14.1.1).",
        ]
    # **必ず notice にする。**有効でも無効でも「見落としてよい行」ではない（§19.2）
    label = "ENABLED" if locks.enabled else "DISABLED"
    return [Row(Status.NOTICE, "Source deletion", label, tuple(lines))]


def _flag(value: bool | None) -> str:
    return "unknown" if value is None else str(value).lower()


def _reaper_line(installed: bool | None) -> str:
    if installed is None:
        return "voicedock-reaper: unknown  <- 安全側（削除しない）として扱う"
    if installed:
        return "voicedock-reaper is INSTALLED"
    return "voicedock-reaper is NOT installed  <- deletion is impossible"


def _mount_line(readonly: bool | None) -> str:
    if readonly is None:
        return "unknown"
    return "device mounted read-only" if readonly else "device mounted read-write"


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
    Check(id="D-10", fatal=True, labels=("ffmpeg / ffprobe",), run=check_ffmpeg),
    Check(id="D-11", fatal=True, labels=("LLM endpoint",), run=check_llm_endpoint),
    Check(id="D-12", fatal=True, labels=("LLM response",), run=check_llm_response),
    Check(id="D-13", fatal=True, labels=("Obsidian vault",), run=check_vault),
    Check(id="D-18", fatal=True, labels=("Helper",), run=check_helper),
    Check(id="D-20", fatal=False, labels=("Inbox orphans",), run=check_inbox_orphans),
    Check(
        id="D-17",
        fatal=False,
        labels=("Source deletion",),
        run=check_source_deletion,
        always=True,
    ),
)


def run(
    stream: IO[str] | None = None,
    *,
    path: Path | None = None,
    state_root: Path = DEFAULT_STATE_ROOT,
    now: datetime | None = None,
) -> int:
    """全検査を実行して表示する。戻り値がプロセスの終了コードになる（§17.3）。

    **`D-17` は `fatal=False` である。**削除モードの表示は「見落としてよい行」ではないが
    （§19.2 は「必ず表示する」と書いている）、**設定として有効なのは異常ではない。**
    致命的にすると Phase 7 で doctor が常に 4 を返し、誰も見なくなる。
    """
    out = stream if stream is not None else sys.stdout
    ctx = Context(
        config_path=path if path is not None else config_path(),
        state_root=state_root,
        now=now,
    )

    rows: list[Row] = []
    blocked = False
    for check in CHECKS:
        # `always` の検査は設定が読めていれば実行する（§19.2 の「必ず表示する」）
        if blocked and not (check.always and ctx.config is not None):
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
