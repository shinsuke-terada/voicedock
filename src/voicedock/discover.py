"""`/inbox` の Part 候補を `DISCOVERED` で登録する（SPEC §10.2, §9.3 Part 表の 1〜2 行目）。

**二重処理防止は `recordings.partkey` の主キー制約そのもので成立させる。**事前の `SELECT` は
ffprobe の起動を省くための最適化であり、**正しさの根拠ではない。**`SELECT` と `INSERT` の
間に別プロセスが入りうるので、`IntegrityError` を握って `already_known` に倒す。

**既知の Part には ffprobe すら起動しない**（§10.2）。`launchd` は 300 秒ごとに Helper を
起動し、1 日 32 件が inbox に残りうる。素直に毎回 probe すると**5 分ごとに 32 回の
プロセス起動**になる。

`device.py` にも `pipeline.py` にも置かない。前者は**ファイルシステムを読むだけ**の層で
DB を知らず、後者は #19 まで存在しない。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from sqlite3 import IntegrityError

from voicedock import audio, device
from voicedock.config import Config
from voicedock.db import Database, Recording
from voicedock.device import PartCandidate, SkippedEntry
from voicedock.errors import ErrorCode
from voicedock.log import Logger
from voicedock.paths import PartKey
from voicedock.states import PART_INITIAL

ALREADY_KNOWN: str = "already_known"
"""`part_skipped` の `reason`（§10.2）。

**`already_known` という名前のイベントを作ってはならない。**§16.4 の 28 件に無く、
`log.py` が未登録の名前に `ValueError` を投げる（v5.2→v5.3 の変更 O-1）。
"""

Prober = Callable[[Path], audio.ProbeResult]
"""`probe_for()` が返す形。テストが差し替えて「起動しなかったこと」を見る。"""


@dataclass(frozen=True)
class DiscoverResult:
    """1 回の走査の結果。**呼び手（#19）が件数をログとメトリクスに使う。**"""

    registered: tuple[PartKey, ...]
    already_known: tuple[PartKey, ...]
    probe_failed: tuple[PartKey, ...]
    """`registered` の部分集合。`duration_seconds` が `NULL` で入った Part。"""

    skipped: tuple[SkippedEntry, ...]
    """`list_inbox_parts()` が候補にしなかったもの（§10.2）。"""


def probe_for(cfg: Config) -> Prober:
    """設定から `probe()` を部分適用する。"""

    def run(path: Path) -> audio.ProbeResult:
        return audio.probe(
            path,
            ffprobe=cfg.audio.ffprobe,
            timeout_seconds=cfg.audio.ffprobe_timeout_seconds,
            max_attempts=cfg.retry.max_attempts,
        )

    return run


def discover(
    database: Database,
    *,
    cfg: Config,
    log: Logger,
    inbox_root: Path | None = None,
    prober: Prober | None = None,
    now: datetime | None = None,
) -> DiscoverResult:
    """`/inbox` を走査して新規 Part を `DISCOVERED` で登録する（§10.2）。

    **`started_at` 昇順で登録する。**`list_inbox_parts()` はディレクトリ名順であって
    時刻順ではない。`events` が時系列に読めるようにするためだけの並べ替えである。

    **`sessions` の行は作らない**（#17）。**`sha256` も埋めない**（#18 が変換時に算出して
    `sha256_helper` と照合する。§10.5）。
    """
    root = inbox_root if inbox_root is not None else cfg.import_.inbox_root
    probe = prober if prober is not None else probe_for(cfg)
    scan = device.list_inbox_parts(root, tz=cfg.tz)

    moment = now if now is not None else datetime.now(cfg.tz)

    for entry in scan.skipped:
        # **`unparsable_filename` だけが WARN である**（§10.2）。`part_skipped` は
        # §16.2 の例が INFO で、`variant_not_orig` は異常ではなく日常である（§5.3）
        emit = log.warning if entry.event == "unparsable_filename" else log.info
        emit(entry.event, recording_key=entry.relpath, reason=entry.reason)

    registered: list[PartKey] = []
    known: list[PartKey] = []
    probe_failed: list[PartKey] = []

    for candidate in sorted(scan.parts, key=lambda part: (part.started_at, part.partkey)):
        if database.get_recording(candidate.partkey) is not None:
            log.debug("part_skipped", recording_key=candidate.partkey, reason=ALREADY_KNOWN)
            known.append(candidate.partkey)
            continue

        result = probe(Path(candidate.source.inbox_path or ""))
        try:
            database.insert_recording(_row(candidate, result, now=moment), now=moment)
        except IntegrityError:
            # 事前の SELECT と INSERT の間に別プロセスが入った。**主キー制約が
            # 二重処理防止の本体である**ことがここで効く（§10.2）
            log.debug("part_skipped", recording_key=candidate.partkey, reason=ALREADY_KNOWN)
            known.append(candidate.partkey)
            continue

        registered.append(candidate.partkey)
        if result.ok:
            log.info(
                "part_discovered",
                recording_key=candidate.partkey,
                tx=candidate.transmitter_id,
                mic=candidate.mic_index,
                started_at=candidate.started_at.isoformat(timespec="seconds"),
                duration=result.duration_seconds,
            )
        else:
            probe_failed.append(candidate.partkey)
            # **`recordings.error_code` は設定しない**（§10.5）。行は DISCOVERED として
            # 健全であり、duration が NULL なのは SPEC が支える恒久状態である。
            # error_code を置くと §15.2 のリトライが終わらない再試行対象として拾い続ける
            log.warning(
                "part_discovered",
                recording_key=candidate.partkey,
                tx=candidate.transmitter_id,
                mic=candidate.mic_index,
                started_at=candidate.started_at.isoformat(timespec="seconds"),
                duration=None,
                error_code=ErrorCode.AUDIO_PROBE_FAILED,
                attempts=result.attempts,
                reason=result.error,
            )

    return DiscoverResult(
        registered=tuple(registered),
        already_known=tuple(known),
        probe_failed=tuple(probe_failed),
        skipped=scan.skipped,
    )


def _row(candidate: PartCandidate, result: audio.ProbeResult, *, now: datetime) -> Recording:
    """`recordings` の 1 行を組み立てる（§8.2）。

    **`partkey` は候補が持っている値をそのまま運ぶ。**`device.py` が
    `paths.partkey_for()` で作っており、ここで文字列を組み直すと出所が 2 つになる
    （§8.5 の唯一の禁則）。
    """
    duration = result.duration_seconds
    started = candidate.started_at
    return Recording(
        partkey=candidate.partkey,
        device_id=candidate.device_id,
        source_folder=candidate.source_folder,
        transmitter_id=candidate.transmitter_id,
        mic_index=candidate.mic_index,
        started_at=started.isoformat(timespec="seconds"),
        status=PART_INITIAL,
        updated_at=now.isoformat(timespec="seconds"),
        duration_seconds=duration,
        ended_at=_ended_at(started, duration),
        source_path=str(candidate.source.relpath),
        source_size=candidate.source.size,
        source_mtime=candidate.source.mtime,
        sha256_helper=candidate.source.sha256_helper,
        inbox_path=str(candidate.source.inbox_path)
        if candidate.source.inbox_path is not None
        else None,
    )


def _ended_at(started_at: datetime, duration_seconds: float | None) -> str | None:
    """`ended_at = started_at + duration`（§10.5(1)）。duration が `NULL` なら `NULL`。"""
    if duration_seconds is None:
        return None
    return (started_at + timedelta(seconds=duration_seconds)).isoformat(timespec="seconds")
