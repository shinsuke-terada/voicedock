"""`voicedock status` の収集と整形（SPEC §17.2）。

**GUI は作らず（§1.6）、CLI も 5 本に絞った（§17.1）ので、運用の可視性はすべてここに
集約される。**とくに 3 つが重要である。

1. **失敗の一覧** — 削除した `history` / `show` / `pending` の役割を引き受ける。再試行は
   自動なので（§15.2）、人が見るのは「いま何が失敗しているか」だけである
2. **未処理 Part の合計時間**（§22 R-16）— 1 日 32 Part の直列処理が追いつかないと溜まる。
   閾値超過は Whisper モデルを下げる判断材料になる
3. **削除モードの状態**（§14.2）— 三重ロックのどれが効いているかが見えないと Phase 7 の
   移行判断ができない

**`cli.py` には書かない。**あちらは §17.1 のパーサとディスパッチだけを持つ層であり、
収集処理を入れると**テストが `argparse` 越しになる。**

**落ちない。**Helper が死んでいても DB が無くても表示を続ける。`status` が落ちると、
**異常時にこそ何も分からなくなる**（§22 R-23 と同じ形）。
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Final

from voicedock import __version__, db, device, paths
from voicedock.config import Config, ConfigError, load_config
from voicedock.device import read_inventory
from voicedock.errors import EXIT_CONFIG, EXIT_OK
from voicedock.heartbeat import DEFAULT_STATE_ROOT, Heartbeat, read_heartbeat
from voicedock.states import PART_TERMINAL, PartStatus, SessionStatus

LABEL_WIDTH: Final = 22
SEPARATOR: Final = "─" * 56
BUCKET_INDENT: Final = "  "
BUCKET_WIDTH: Final = 20
UNKNOWN: Final = "unknown"
GIB: Final = 1024**3
FAILED_LIMIT: Final = 20
"""`Failed parts` に並べる最大件数。**全部出すと 1 日分のログが埋まる。**"""

# §17.2 の表示順。**名前は `states.py` の enum から取る**（書き写さない）
PART_ORDER: Final[tuple[PartStatus, ...]] = (
    PartStatus.DISCOVERED,
    PartStatus.NORMALIZING,
    PartStatus.NORMALIZED,
    PartStatus.TRANSCRIBING,
    PartStatus.TRANSCRIBED,
    PartStatus.RAW_WRITING,
    PartStatus.RAW_SAVED,
    PartStatus.SOURCE_DELETING,
    PartStatus.SOURCE_DELETE_PENDING,
    PartStatus.COMPLETED,
    PartStatus.SKIPPED,
    PartStatus.FAILED,
)
SESSION_ORDER: Final[tuple[SessionStatus, ...]] = (
    SessionStatus.OPEN,
    SessionStatus.READY,
    SessionStatus.MERGING,
    SessionStatus.MERGED,
    SessionStatus.ANALYZING,
    SessionStatus.ANALYZED,
    SessionStatus.WRITING,
    SessionStatus.SAVED,
    SessionStatus.SOURCE_DELETING,
    SessionStatus.SOURCE_DELETE_PENDING,
    SessionStatus.CLEANUP,
    SessionStatus.COMPLETED,
    SessionStatus.FAILED,
)

PART_NOTES: Final[dict[str, str]] = {
    PartStatus.SKIPPED.value: "（無音）",
    PartStatus.FAILED.value: "（次回接続時に再試行）",
}
"""§17.2 が添えている注記。**Part 専用である。**

`SessionStatus` にも `FAILED` / `SOURCE_DELETING` があるので、**entity で分けずに
状態名だけで引くと Session の `FAILED` に「次回接続時に再試行」が付く**（実際に付いた）。
Session の `FAILED` はリトライの契機が違う（§15.2 は工程内リトライと再評価を分けている）。
"""

SESSION_NOTES: Final[dict[str, str]] = {}
"""§17.2 は Session 側に注記を置いていない。**空でも明示する**（Part 用を流用させない）。"""

RETRY_HINT: Final = "→ 次にデバイスを接続したときに自動で再試行される（§15.2）"


@dataclass(frozen=True)
class FailedPart:
    """`Failed parts` の 1 件。"""

    partkey: str
    started_at: str
    error_code: str | None
    retry_count: int

    def render(self, max_attempts: int) -> list[str]:
        """**`partkey` を 1 行目に、詳細を次の行へ字下げする。**

        §17.2 の例は整数 ID（`42`）で 1 行に収めているが、v5.1 で自然キーへ移った
        （v5.0→v5.1 の変更 M-1）。**70 文字の鍵を表の 1 列目に入れると読めない。**
        §16.2 が「短い別名を作ると識別子が 2 系統になる」と決めているので**別名は作らない。**
        """
        when = self.started_at[:16].replace("T", " ")
        code = self.error_code or UNKNOWN
        return [
            f"{BUCKET_INDENT}{self.partkey}",
            f"{BUCKET_INDENT * 2}{when}  {code}  retry {self.retry_count}/{max_attempts}",
        ]


@dataclass(frozen=True)
class Backlog:
    """未処理 Part の量（§22 R-16）。"""

    parts: int
    seconds: float
    unknown_parts: int
    """`duration_seconds` が `NULL` の件数。**0 として数えた分を併記する。**"""

    def render(self) -> str:
        if self.parts == 0:
            return "未処理なし"
        text = f"未処理 {self.seconds / 3600:.1f} 時間ぶん（{self.parts} part）"
        if self.unknown_parts:
            text += f"、うち {self.unknown_parts} part は長さ不明"
        return text


@dataclass(frozen=True)
class Locks:
    """§14.2 の三重ロック。**個別に出す**（どれが効いているか分からないと移行判断ができない）。"""

    config_delete: bool
    helper_delete: bool | None
    reaper_installed: bool | None
    mount_readonly: bool | None

    @property
    def lock1(self) -> bool:
        """**両方が true のときだけ true**（§14.2 はロック 1 を「両方」と定義している）。"""
        return self.config_delete and self.helper_delete is True

    @property
    def enabled(self) -> bool:
        """削除が実際に起こりうるか。**不明は安全側（起こらない）へ倒す**（§7.5）。"""
        return self.lock1 and self.reaper_installed is True and self.mount_readonly is False

    def render(self) -> str:
        state = "ENABLED " if self.enabled else "DISABLED"
        return (
            f"{state}  (lock1={str(self.lock1).lower()}, "
            f"lock2A={_reaper_text(self.reaper_installed)}, "
            f"lock2B={mount_text(self.mount_readonly)})"
        )


def _reaper_text(installed: bool | None) -> str:
    if installed is None:
        return UNKNOWN
    return "reaper present" if installed else "reaper absent"


def mount_text(readonly: bool | None) -> str:
    """観測されたマウント状態の表示語。**`status` と `doctor` で同じ語を使う**（#107）。

    **`None` を `writable` に丸めない。**Helper が値を書けなかったことと、デバイスが
    書き込み可能であることは別の事実であり、後者は §14.2 のロック 2-B を開ける。
    """
    if readonly is None:
        return UNKNOWN
    return "readOnly" if readonly else "writable"


@dataclass(frozen=True)
class Snapshot:
    """1 回の `status` が表示する全部。**整形と収集を分ける**（テストが値で確かめられる）。"""

    helper: str
    devices: str
    device_free: str
    inbox: str
    delete_queue: str
    locks: Locks
    parts: dict[str, int]
    sessions: dict[str, int]
    backlog: Backlog
    failed: tuple[FailedPart, ...]
    failed_total: int
    staging: str
    free_disk: str
    max_attempts: int


# --- 収集 ----------------------------------------------------------------


def collect(
    cfg: Config,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    now: datetime | None = None,
) -> Snapshot:
    """表示に必要な値を集める。**例外を投げない。**"""
    moment = now if now is not None else datetime.now(cfg.tz)
    beat = read_heartbeat(state_root)
    inventory = _inventory(state_root, cfg)

    parts, sessions, backlog, failed, failed_total, orphaned = _from_database(cfg)
    return Snapshot(
        helper=_helper_text(beat, cfg, moment),
        devices=inventory,
        device_free=_free_space_text(state_root),
        inbox=_inbox_text(cfg, orphaned),
        delete_queue=_queue_text(),
        locks=Locks(
            config_delete=cfg.cleanup.delete_source_audio,
            helper_delete=beat.delete_source_audio if beat else None,
            reaper_installed=beat.reaper_installed if beat else None,
            mount_readonly=beat.mount_readonly if beat else None,
        ),
        parts=parts,
        sessions=sessions,
        backlog=backlog,
        failed=failed,
        failed_total=failed_total,
        staging=_staging_text(cfg),
        free_disk=_free_disk_text(cfg),
        max_attempts=cfg.retry.max_attempts,
    )


def _from_database(
    cfg: Config,
) -> tuple[dict[str, int], dict[str, int], Backlog, tuple[FailedPart, ...], int, frozenset[str]]:
    """DB から件数・Backlog・失敗一覧を取る。

    **`migrate=False` で開く。**doctor D-2 と同じ理由で、**`status` が DB を作ってしまうと
    「まだ 1 度も動いていない」ことを報告できない。**
    """
    empty_parts = {status.value: 0 for status in PART_ORDER}
    empty_sessions = {status.value: 0 for status in SESSION_ORDER}
    if not Path(cfg.database.path).is_file():
        return empty_parts, empty_sessions, Backlog(0, 0.0, 0), (), 0, frozenset()

    try:
        with db.connect(
            cfg.database.path,
            busy_timeout_ms=cfg.database.busy_timeout_ms,
            tz=cfg.tz,
            migrate=False,
        ) as opened:
            parts = empty_parts | _counts(opened, "recordings")
            sessions = empty_sessions | _counts(opened, "sessions")
            return parts, sessions, _backlog(opened), *_failed(opened), _orphaned(opened)
    except sqlite3.Error:
        return empty_parts, empty_sessions, Backlog(0, 0.0, 0), (), 0, frozenset()


def orphaned_partkeys(cfg: Config) -> frozenset[str] | None:
    """取り残しになりうる Part の `partkey`。**DB を読めなければ `None`**（#120）。

    `doctor` の D-19 と `status` の `Inbox` 行が**同じ集合を使う** — 判定が 2 本に
    分かれると、片方だけ直したときに**一方が「取り残しなし」と言いながらもう一方が
    数える。**
    """
    if not Path(cfg.database.path).is_file():
        return None
    try:
        with db.connect(
            cfg.database.path,
            busy_timeout_ms=cfg.database.busy_timeout_ms,
            tz=cfg.tz,
            migrate=False,
        ) as opened:
            return _orphaned(opened)
    except sqlite3.Error:
        return None


def _orphaned(opened: db.Database) -> frozenset[str]:
    """inbox に原本が残っていたら取り残しになる Part の `partkey`（#120）。"""
    placeholders = ", ".join("?" for _ in ORPHANED_STATES)
    rows = opened.conn.execute(
        f"SELECT partkey FROM recordings WHERE status IN ({placeholders})",  # noqa: S608
        sorted(ORPHANED_STATES),
    ).fetchall()
    return frozenset(row["partkey"] for row in rows)


def _counts(opened: db.Database, table: str) -> dict[str, int]:
    rows = opened.conn.execute(
        f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status"  # noqa: S608
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}


def _backlog(opened: db.Database) -> Backlog:
    """未処理 Part の量（§22 R-16）。

    **未処理の定義は「終端状態でない Part」**（`PART_TERMINAL` の補集合）。`FAILED` は
    終端だが再試行されるので、**別の行（`Failed parts`）で出す**（§17.2 がそうしている）。

    `duration_seconds` が `NULL` の Part は 0 として合計し、**件数を併記する** —
    NULL は §10.5 が支える恒久状態なので、黙って 0 にすると過小に見える。
    """
    pending = [status.value for status in PartStatus if status not in PART_TERMINAL]
    placeholders = ", ".join("?" for _ in pending)
    row = opened.conn.execute(
        "SELECT COUNT(*) AS parts, "  # noqa: S608 - placeholders は ? のみ
        "COALESCE(SUM(duration_seconds), 0) AS seconds, "
        "SUM(CASE WHEN duration_seconds IS NULL THEN 1 ELSE 0 END) AS unknown_parts "
        f"FROM recordings WHERE status IN ({placeholders})",
        pending,
    ).fetchone()
    return Backlog(
        parts=row["parts"],
        seconds=float(row["seconds"] or 0.0),
        unknown_parts=int(row["unknown_parts"] or 0),
    )


def _failed(opened: db.Database) -> tuple[tuple[FailedPart, ...], int]:
    total = opened.conn.execute(
        "SELECT COUNT(*) AS n FROM recordings WHERE status = ?", (PartStatus.FAILED,)
    ).fetchone()["n"]
    rows = opened.conn.execute(
        "SELECT partkey, started_at, error_code, retry_count FROM recordings "
        "WHERE status = ? ORDER BY started_at LIMIT ?",
        (PartStatus.FAILED, FAILED_LIMIT),
    ).fetchall()
    return (
        tuple(
            FailedPart(
                partkey=row["partkey"],
                started_at=row["started_at"],
                error_code=row["error_code"],
                retry_count=row["retry_count"],
            )
            for row in rows
        ),
        int(total),
    )


def _helper_text(beat: Heartbeat | None, cfg: Config, now: datetime) -> str:
    """§17.2 の `Helper` 行。

    **Helper が居なくても表示を続ける**（§22 R-23）。`status` が落ちると、
    Helper が死んだときに何も分からなくなる。
    """
    if beat is None:
        return f"{UNKNOWN}  (heartbeat.json がありません)"
    age = beat.age_seconds(now)
    stale = beat.is_stale(now, cfg.import_.helper_heartbeat_max_age_seconds)
    seen = UNKNOWN if age is None else f"{age:.0f}s ago"
    state = "stale" if stale else "running"
    # **実測を出す。設定値ではない**（#107）。`doctor` の DH-12 と同じ語を使う。
    # `mount_mode` は「そうしたい」であり、`mount_readonly` は「そうなっている」である。
    # **両方を出す** — 食い違い自体が異常の徴候だからである
    observed = mount_text(beat.mount_readonly)
    detail = (
        f"{state}   (last seen {seen}, {beat.helper_version or UNKNOWN}, "
        f"mount={observed} (MOUNT_MODE={beat.mount_mode or UNKNOWN}))"
    )
    if beat.config_error:
        detail += f"\n{' ' * (LABEL_WIDTH + 3)}config_error: {beat.config_error}"
    return detail


def _inventory(state_root: Path, cfg: Config) -> str:
    """§17.2 の `Devices connected` 行。`inventory.json` を読む（§7.5）。"""
    found = read_inventory(state_root)
    if found is None:
        return f"{UNKNOWN}  (inventory.json がありません)"
    if found.is_empty:
        return "0"
    mode = mount_text(found.mount_readonly)
    names = ", ".join(sorted(found.devices))
    return f"{len(found.devices)}  ({names}, {mode})"


ORPHANED_STATES: Final[frozenset[str]] = frozenset(PART_TERMINAL) - {PartStatus.FAILED}
"""inbox に原本が残っていたら**取り残し**である Part の状態。

**`FAILED` を除く。**§15.2 でデバイス再接続とサービス起動のたびに再投入されるので、
**処理待ちである。**残りの終端状態（`COMPLETED` / `SKIPPED` / `RAW_SAVED` /
`SOURCE_DELETING` / `SOURCE_DELETE_PENDING`）は二度と変換されない。

**手で並べない。**`PART_TERMINAL` から引く — 終端が増えたときに追随させる場所を 1 つにする。
"""


def _inbox_text(cfg: Config, orphaned_keys: frozenset[str]) -> str:
    """inbox の `.wav` を「処理待ち」と「取り残し」に分ける。

    **`.meta.json` だけのもの（墓標）は数えない**（§10.2）。処理済みの印である。

    **`pending` は「これから処理される」という意味である。**partkey が終端状態の
    ファイルは二度と処理されないので、**同じ数に混ぜてはならない**（#120）。
    2026-09-15 に実機で、処理されない 259 MB を `1 parts pending` と報告し続けた
    （`Backlog : 未処理なし` と同時に出るので、どちらを信じればよいか分からない）。

    **候補の列挙は `device.list_inbox_parts()` に任せる。**partkey の作り方を
    ここで書き直すと出所が 2 つになる（§8.5 の唯一の禁則）。
    """
    root = Path(cfg.import_.inbox_root)
    if not root.is_dir():
        return f"{UNKNOWN}  ({root} がありません)"

    # **取り残しの原本を先に特定する。**`list_inbox_parts()` は `.wav` と `.meta.json` が
    # 揃った候補だけを返すので、partkey を作れるのはここだけである
    orphan_paths: set[Path] = set()
    orphan_bytes = 0
    for candidate in device.list_inbox_parts(root, tz=cfg.tz).parts:
        if candidate.partkey in orphaned_keys and candidate.source.inbox_path is not None:
            orphan_paths.add(Path(candidate.source.inbox_path))
            orphan_bytes += candidate.source.size

    # **`.wav` の総数は従来どおり数える。**候補にならないファイル（名前が §5.1 に
    # 合わない、墓標が無い）も**ディスクは使っている** — 見えなくしない
    count = 0
    total = 0
    for path in root.rglob("*.wav"):
        if path in orphan_paths:
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
        count += 1

    text = f"{count} parts pending, {total / GIB:.1f} GiB"
    if orphan_paths:
        # **0 件なら括弧ごと出さない。**正常な状態に注意を引かない
        text += f"  (+ 取り残し {len(orphan_paths)} 件 {orphan_bytes / GIB:.1f} GiB)"
    return text


def _queue_text() -> str:
    """`queue/delete/` と `queue/result/` の件数（§14.1.1 / §14.3）。"""
    requested = _json_count(paths.QUEUE_ROOT / "delete")
    results = _json_count(paths.QUEUE_ROOT / "result")
    return f"{requested} requested, {results} awaiting result"


def _json_count(folder: Path) -> int:
    try:
        return sum(1 for path in folder.iterdir() if path.suffix == ".json")
    except OSError:
        return 0


def _staging_text(cfg: Config) -> str:
    root = Path(cfg.import_.staging_root)
    used = 0
    for path in root.rglob("*") if root.is_dir() else ():
        try:
            used += path.stat().st_size if path.is_file() else 0
        except OSError:
            continue
    limit = cfg.import_.staging_max_bytes
    return f"{used / GIB:.1f} GiB / {limit / GIB:.1f} GiB"


def _free_disk_text(cfg: Config) -> str:
    try:
        free = shutil.disk_usage(paths.DATA_ROOT).free
    except OSError:
        return UNKNOWN
    return f"{free / GIB:.1f} GiB"


def _free_space_text(state_root: Path) -> str:
    """§17.2 の `Device free space` 行。`inventory.json` の `device_free_bytes`（§7.5）。

    **デバイスごとに出す。**複数のデバイスが同時に接続されうる（§5.4 の同名衝突の注記が
    前提にしている）ので、1 つの数では表せない。**コンテナはデバイスに到達できない**
    （§14.4 N-3）ため、Helper が報告しなければ `unknown` である。
    """
    found = read_inventory(state_root)
    if found is None or not found.free_bytes:
        return UNKNOWN
    return ", ".join(
        f"{device_id} {value / GIB:.1f} GiB"
        for device_id, value in sorted(found.free_bytes.items())
    )


# --- 整形（§17.2） ------------------------------------------------------


def render(snapshot: Snapshot) -> list[str]:
    """§17.2 の書式。**行ラベルと順序をここ 1 箇所に持つ。**"""
    lines = [f"VoiceDock v{__version__}", SEPARATOR]
    lines += [
        _row("Helper", snapshot.helper),
        _row("Devices connected", snapshot.devices),
        _row("Device free space", snapshot.device_free),
        _row("Inbox", snapshot.inbox),
        _row("Delete queue", snapshot.delete_queue),
        _row("Source deletion", snapshot.locks.render()),
        "",
        "Parts",
    ]
    lines += [_bucket(state.value, snapshot.parts[state.value], PART_NOTES) for state in PART_ORDER]
    lines += ["", "Sessions"]
    lines += [
        _bucket(state.value, snapshot.sessions[state.value], SESSION_NOTES)
        for state in SESSION_ORDER
    ]
    lines += [
        "",
        _row("Backlog", snapshot.backlog.render()),
        _row("Staging usage", snapshot.staging),
        _row("Free disk (/data)", snapshot.free_disk),
    ]
    if snapshot.failed:
        lines += ["", "Failed parts"]
        for part in snapshot.failed:
            lines += part.render(snapshot.max_attempts)
        if snapshot.failed_total > len(snapshot.failed):
            remaining = snapshot.failed_total - len(snapshot.failed)
            lines.append(f"{BUCKET_INDENT}… ほか {remaining} 件")
        lines.append(f"{BUCKET_INDENT}{RETRY_HINT}")
    return lines


def _row(label: str, value: str) -> str:
    return f"{label:<{LABEL_WIDTH}}: {value}"


def _bucket(label: str, count: int, notes: dict[str, str]) -> str:
    """状態別の件数。**注記は entity ごとの辞書から引く**（Part 用を Session へ流用しない）。"""
    note = notes.get(label, "")
    text = f"{BUCKET_INDENT}{label:<{BUCKET_WIDTH}}: {count}"
    return f"{text}    {note}".rstrip() if note else text


def run(
    out: IO[str] | None = None,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    now: datetime | None = None,
) -> int:
    """`voicedock status` の本体。

    **`FAILED` が在っても 0 を返す**（§17.3）。1 は「一般的なエラー」であり、
    **失敗の一覧が出ていることは `status` 自身の失敗ではない。**`FAILED` で 1 を返すと
    `make status` が常に赤くなり、見る習慣が失われる。
    """
    stream = out if out is not None else sys.stdout
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"voicedock: 設定エラー（SPEC §7.3）: {e.path}", file=sys.stderr)
        for violation in e.violations:
            print(f"  {violation.render()}", file=sys.stderr)
        return EXIT_CONFIG

    for line in render(collect(cfg, state_root=state_root, now=now)):
        stream.write(line + "\n")
    return EXIT_OK


def lines_of(values: Sequence[str]) -> str:
    """テストと `make status` のための連結。"""
    return "\n".join(values) + "\n"
