"""`ensure_*` の直列実行（SPEC §11.3, §10.5〜§10.7, §9.4, §15.2）。

**すべての `ensure_*` は冪等である**（§11.3）。各関数は先頭で §9.4 の表に従って
「既に完了しているか」を確認し、完了していれば即座に真を返す。

**各工程が偽を返したら、以降の工程を実行してはならない**（§11.3）。とくに
#36 の `delete_sources_if_safe()` が保留を返したときに `cleanup_staging()` と
`mark_completed()` を呼んではならない（§14.3）。**この規範をここで枠として固定する。**

**状態を変えるのは `db.record_transition()` だけである**（§8.4）。列の更新は
`db.update_recording()` / `update_session()` が行い、**`status` を受け付けない。**
`status` を直接書ける経路があると「`events` の無い状態変化」が生まれる。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from voicedock import audio, cleaner, daily, device, llm, paths, raw, session, transcribe, wiki
from voicedock.config import Config
from voicedock.db import Database, EntityType, Recording, Session, TransitionConflict
from voicedock.device import DeviceInventory
from voicedock.errors import ErrorCode, counts_against_max_attempts
from voicedock.heartbeat import DEFAULT_STATE_ROOT
from voicedock.log import Logger
from voicedock.paths import InboxPath, PartKey, SessionKey, StagingPath
from voicedock.states import (
    PART_DELETABLE,
    PART_RECOVERY,
    PART_RETRYABLE_FROM_FAILED,
    SESSION_RECOVERY,
    SESSION_RETRYABLE_FROM_FAILED,
    PartStatus,
    SessionStatus,
)


class PartOutcome(StrEnum):
    """`process_part()` の結果（§11.3）。"""

    STOPPED = "stopped"
    """どこかの工程が偽を返した。**以降の工程は実行していない。**"""

    READY_FOR_SESSION = "ready_for_session"
    """`RAW_SAVED` まで到達した。"""


# **`ensure_normalized_audio` を飛ばしてよい状態**（§9.4 の「完了済みステップを飛ばす」）
NORMALIZED_OR_BEYOND: Final[frozenset[str]] = frozenset(
    {
        PartStatus.NORMALIZED,
        PartStatus.TRANSCRIBING,
        PartStatus.TRANSCRIBED,
        PartStatus.RAW_WRITING,
        PartStatus.RAW_SAVED,
        PartStatus.SOURCE_DELETING,
        PartStatus.SOURCE_DELETE_PENDING,
        PartStatus.COMPLETED,
    }
)

TRANSCRIBED_OR_BEYOND: Final[frozenset[str]] = frozenset(
    {
        PartStatus.TRANSCRIBED,
        PartStatus.RAW_WRITING,
        PartStatus.RAW_SAVED,
        PartStatus.SOURCE_DELETING,
        PartStatus.SOURCE_DELETE_PENDING,
        PartStatus.COMPLETED,
    }
)

RAW_SAVED_OR_BEYOND: Final[frozenset[str]] = frozenset(
    {
        PartStatus.RAW_SAVED,
        PartStatus.SOURCE_DELETING,
        PartStatus.SOURCE_DELETE_PENDING,
        PartStatus.COMPLETED,
    }
)

DELETE_EVALUATED: Final[frozenset[str]] = frozenset(
    {
        SessionStatus.SAVED,
        SessionStatus.SOURCE_DELETING,
        SessionStatus.SOURCE_DELETE_PENDING,
        SessionStatus.CLEANUP,
    }
)
"""§14.1 を評価するセッションの状態（§9.3 の `SAVED` 行以降）。

**`COMPLETED` を含めない。**後始末まで終わった行をもう一度評価すると、`delete_attempts`
が毎周回増え続ける。
"""

MERGEABLE: Final[frozenset[str]] = frozenset({SessionStatus.READY, SessionStatus.MERGING})
"""`ensure_merged()` が進められるセッションの状態。

**`MERGING` を含める。**再オープンは §9.3 に従って `MERGING` へ戻すので、`READY` だけを
受け付けると作り直しが次の起動まで動かない。クラッシュで `MERGING` のまま残った行も
同じ経路で進む（§9.4 の巻き戻しは起動時の 1 回だけである）。
"""

ANALYZABLE: Final[frozenset[str]] = frozenset({SessionStatus.MERGED, SessionStatus.ANALYZING})
"""`ensure_analysis()` が進められるセッションの状態。

**`ANALYZING` を含める。**`resume_failed()` は `FAILED` を**落ちた工程へ**戻すので
（§9.3 / §15.2）、LLM で落ちた行は `ANALYZING` に戻ってくる。`MERGED` だけを受け付けると
**戻した先に受け手がいない。**`MERGEABLE` が `MERGING` を含むのと同じ理由である。
"""

WRITABLE: Final[frozenset[str]] = frozenset({SessionStatus.ANALYZED, SessionStatus.WRITING})
"""`ensure_daily_note()` が進められるセッションの状態。`ANALYZABLE` と同じ理由で
`WRITING` を含める（Obsidian の書き込みで落ちた行はここへ戻る）。
"""

PROCESSABLE: Final[frozenset[str]] = MERGEABLE | ANALYZABLE | WRITABLE
"""`process_session()` に渡す価値があるセッションの状態。

**`worker.ready_session_keys()` の走査対象そのものである。**ここから漏れた状態は
**誰にも拾われない。**v5.13 まで走査は `{READY, MERGING}` だけで、`MERGED` /
`ANALYZING` / `ANALYZED` / `WRITING` に座った行は**再起動しても動かなかった**
（§9.4 の巻き戻しは `ANALYZING → MERGED` などへ移すだけで、`MERGED` もまた
走査対象外だったため）。2026-09-14 に実機で踏んだ（#97）。
"""

REOPENABLE: Final[frozenset[str]] = frozenset({SessionStatus.SAVED, SessionStatus.COMPLETED})
"""再オープンできるセッションの状態（§9.3 の `SAVED / COMPLETED → MERGING` 行）。

**`READY` や `MERGING` を入れてはならない。**まだ確定していないセッションは、そのまま
次の統合で新しい Part を拾う。戻す必要があるのは**一度 Daily ノートを書き終えたもの**だけである。
"""

SKIP_REASONS: Final[dict[ErrorCode, str]] = {
    ErrorCode.SOURCE_MISSING: "source_missing",
    ErrorCode.DUPLICATE_CONTENT: "duplicate_content",
    ErrorCode.NO_SPEECH_DETECTED: "no_speech",
}
"""`part_skipped` の `reason`（§16.4）。**専用のイベント名を作らない** —
v5.0 で `duplicate_content` / `no_speech_detected` は `part_skipped` の `reason` になった。
"""


class SessionOutcome(StrEnum):
    """`process_session()` の結果（§11.3）。"""

    STOPPED = "stopped"
    """どこかの工程が偽を返した。**以降の工程は実行していない。**"""

    EMPTY = "empty"
    """有効な Part が 0 件。ノートを作らず `COMPLETED`（`session_empty`。§10.8）。"""

    ANALYZED = "analyzed"
    """LLM 解析まで到達したが Daily ノートで止まった。"""

    SAVED = "saved"
    """Daily ノートを保存し検証も通った（§13.7）。削除の評価は #36 が行う。"""


SAVED_OR_BEYOND: Final[frozenset[str]] = frozenset(
    {
        SessionStatus.SAVED,
        SessionStatus.SOURCE_DELETING,
        SessionStatus.SOURCE_DELETE_PENDING,
        SessionStatus.CLEANUP,
        SessionStatus.COMPLETED,
    }
)

ANALYZED_OR_BEYOND: Final[frozenset[str]] = frozenset(
    {
        SessionStatus.ANALYZED,
        SessionStatus.WRITING,
        SessionStatus.SAVED,
        SessionStatus.SOURCE_DELETING,
        SessionStatus.SOURCE_DELETE_PENDING,
        SessionStatus.CLEANUP,
        SessionStatus.COMPLETED,
    }
)

MERGED_OR_BEYOND: Final[frozenset[str]] = frozenset(
    {SessionStatus.MERGED, SessionStatus.ANALYZING, *ANALYZED_OR_BEYOND}
)


@dataclass
class Pipeline:
    """1 つの Part / Session を進める。**状態を持たない**（DB と設定だけを見る）。"""

    database: Database
    cfg: Config
    log: Logger
    now: datetime | None = None
    """時刻の注入口。**テストが固定する。**"""

    state_root: Path = DEFAULT_STATE_ROOT
    """`/state`（Helper の報告）。**§14.1 のロック 2-B を読むのに要る**（§7.5）。"""

    # --- §11.3 の本体 ----------------------------------------------------

    def process_part(self, partkey: PartKey) -> PartOutcome:
        """§11.3 の `process_part`。**偽を返したら以降の工程を実行しない。**"""
        record = self.database.get_recording(partkey)
        if record is None:
            return PartOutcome.STOPPED
        if not self.ensure_normalized_audio(record):
            return PartOutcome.STOPPED
        if not self.ensure_part_transcript(self._reload(partkey)):
            return PartOutcome.STOPPED
        if not self.ensure_raw_note(self._reload(partkey)):
            return PartOutcome.STOPPED
        return PartOutcome.READY_FOR_SESSION

    # --- §10.5 変換 ------------------------------------------------------

    def ensure_normalized_audio(self, record: Recording) -> bool:
        """16 kHz WAV を用意する（§10.5）。**冪等。**

        §9.4 の「`normalized_path` が存在し `size > 0` ∧ 形式が正しい」は
        `audio.normalize()` の冪等判定がそのまま担う（出力を検証して再利用する）。
        """
        if record.status in NORMALIZED_OR_BEYOND:
            return True
        if record.status != PartStatus.DISCOVERED:
            # `FAILED` / `SKIPPED` / `NORMALIZING`。**ここでは進めない** —
            # `FAILED` の再投入は §15.2（#32）、`NORMALIZING` は §9.4 の巻き戻しが扱う
            return False

        source = record.inbox_path
        if source is None or not self._usable_source(Path(source)):
            # §9.3「取り込み直前の再確認」。**デバイス上のファイルには触れない**
            self._skip(record, ErrorCode.SOURCE_MISSING, f"inbox に原本がありません: {source}")
            return False

        space = audio.check_space(self.cfg, duration_seconds=record.duration_seconds)
        if not space.ok:
            # **ガードである。**§9.3 は空き容量を `DISCOVERED → NORMALIZING` の
            # ガード条件にしている。遷移しないので `FAILED` にもならず、次の周回で再評価される
            self.log.warning("disk_space_low", recording_key=record.partkey, reason=space.detail)
            return False

        self._transition(record.partkey, PartStatus.DISCOVERED, PartStatus.NORMALIZING)
        claimed = self.database.recording_by_normalized_path(
            str(audio.normalized_path_for(PartKey(record.partkey)))
        )
        result = audio.normalize(
            InboxPath(Path(source)),
            partkey=PartKey(record.partkey),
            duration_seconds=record.duration_seconds,
            sha256_helper=record.sha256_helper,
            cfg=self.cfg,
            claimed_by=claimed.partkey if claimed else None,
            duplicate_of=self._duplicate_of,
        )

        if result.error_code is ErrorCode.DUPLICATE_CONTENT:
            self._skip(
                record,
                ErrorCode.DUPLICATE_CONTENT,
                result.error_message,
                from_status=PartStatus.NORMALIZING,
            )
            return False
        if result.error_code is not None or result.path is None:
            self._fail(
                record.partkey,
                PartStatus.NORMALIZING,
                result.error_code or ErrorCode.IMPORT_FAILED,
                result.error_message,
                event="normalize_failed",
            )
            return False

        self.database.update_recording(
            record.partkey,
            sha256=result.sha256,
            normalized_path=str(result.path),
            staging_dir=str(audio.staging_dir_for(PartKey(record.partkey))),
            error_code=None,
            error_message=None,
            now=self.now,
        )
        self._transition(record.partkey, PartStatus.NORMALIZING, PartStatus.NORMALIZED)
        self.log.info(
            "normalize_completed",
            recording_key=record.partkey,
            in_bytes=result.in_bytes,
            out_bytes=result.out_bytes,
            elapsed_s=round(result.elapsed_seconds, 1),
        )
        # **DB 更新が成功して初めて `NORMALIZED` である**（§9.3 の副作用）。
        # だから inbox の原本を消すのはこの後である
        audio.release_inbox(InboxPath(Path(source)), self.cfg)
        return True

    def _usable_source(self, path: Path) -> bool:
        """§9.3: `_orig` が存在し `size > 0`。"""
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _duplicate_of(self, digest: str) -> str | None:
        other = self.database.recording_by_sha256(digest)
        return None if other is None else other.partkey

    # --- §10.6 文字起こし ------------------------------------------------

    def ensure_part_transcript(self, record: Recording | None) -> bool:
        """Part transcript を用意する（§10.6）。**冪等。**"""
        if record is None:
            return False
        if record.status in TRANSCRIBED_OR_BEYOND:
            return True
        if record.status != PartStatus.NORMALIZED or record.normalized_path is None:
            return False

        self._transition(record.partkey, PartStatus.NORMALIZED, PartStatus.TRANSCRIBING)
        result = transcribe.transcribe(
            StagingPath(Path(record.normalized_path)),
            partkey=PartKey(record.partkey),
            started_at=record.started_at,
            duration_seconds=record.duration_seconds,
            cfg=self.cfg,
        )

        if result.error_code is ErrorCode.NO_SPEECH_DETECTED:
            # **失敗ではない**（§10.6）。`SKIPPED` へ倒して次の Part へ進む
            self._skip(
                record,
                ErrorCode.NO_SPEECH_DETECTED,
                result.error_message,
                from_status=PartStatus.TRANSCRIBING,
            )
            return False
        if result.error_code is not None or result.transcript is None:
            self._fail(
                record.partkey,
                PartStatus.TRANSCRIBING,
                result.error_code or ErrorCode.WHISPER_FAILED,
                result.error_message,
                event="transcription_failed",
            )
            return False

        self.database.update_recording(
            record.partkey,
            transcript_path=str(paths.transcript_path_for(PartKey(record.partkey))),
            error_code=None,
            error_message=None,
            now=self.now,
        )
        self._transition(record.partkey, PartStatus.TRANSCRIBING, PartStatus.TRANSCRIBED)
        self.log.info(
            "transcription_completed",
            recording_key=record.partkey,
            **transcribe.metrics(result, duration_seconds=record.duration_seconds),
        )
        if self.cfg.cleanup.delete_normalized_after_transcribe:
            # 容量節約（§10.6 の「成功後」）。**DB 更新の後に消す** — 先に消すと
            # DB 更新に失敗したときに「16 kHz も transcript も無い」状態になりうる
            self._discard_staging(StagingPath(Path(record.normalized_path)))
        return True

    # --- §10.7 Raw ノート -----------------------------------------------

    def ensure_raw_note(self, record: Recording | None) -> bool:
        """Raw ノートを保存する（§10.7 / §13.3）。**冪等。**

        **`FAILED` にするのはトリガ Part 1 件だけである**（§9.3）。同じ Raw ノートに
        含まれる他の Part は `TRANSCRIBED` のまま据え置いて次回の再生成で再試行する。
        """
        if record is None:
            return False
        if record.status in RAW_SAVED_OR_BEYOND:
            return True
        if record.status != PartStatus.TRANSCRIBED or record.session_key is None:
            return False

        session_key = SessionKey(record.session_key)
        parts = self._raw_parts(session_key)
        if not parts:
            return False

        self._transition(record.partkey, PartStatus.TRANSCRIBED, PartStatus.RAW_WRITING)
        try:
            result = raw.write_raw_note(
                parts,
                day=paths.day_of_session(session_key),
                session_key=session_key,
                cfg=self.cfg.obsidian.raw,
                vault_root=Path(self.cfg.obsidian.root),
                max_title_bytes=self.cfg.obsidian.max_title_bytes,
            )
        except (OSError, ValueError) as exc:
            self._fail(
                record.partkey,
                PartStatus.RAW_WRITING,
                ErrorCode.OBSIDIAN_RAW_WRITE_FAILED,
                f"{type(exc).__name__}: {exc}",
                event="raw_note_failed",
                reason="write",
            )
            return False

        if not result.ok:
            self._fail(
                record.partkey,
                PartStatus.RAW_WRITING,
                ErrorCode.OBSIDIAN_RAW_VERIFY_FAILED,
                f"落ちた規則: {', '.join(result.failed_rules)}",
                event="raw_note_failed",
                reason="verify",
            )
            return False

        # **DB 更新が成功するまで `RAW_SAVED` とみなさない**（§13.7）
        self.database.update_session(
            session_key,
            raw_output_path=self._vault_relative(Path(result.path)),
            raw_output_sha256=result.sha256,
            now=self.now,
        )
        self._transition(record.partkey, PartStatus.RAW_WRITING, PartStatus.RAW_SAVED)
        self.log.info(
            "raw_note_saved",
            session_key=session_key,
            parts=len(parts),
            bytes=Path(result.path).stat().st_size,
        )
        self.reopen_session(session_key)
        return True

    # --- §9.2 再オープン ---------------------------------------------------

    def reopen_session(self, session_key: SessionKey) -> bool:
        """確定済みのセッションへ新しい Part が入ったら作り直す（§9.2 / §9.3）。

        **契機は `RAW_SAVED` である。**分組（§10.4）ではない — 分組は `DISCOVERED` の
        時点で走るので、そこで `MERGING` へ戻すと**まだ文字起こししていない Part を
        含んだまま統合し、本文が欠けた Daily ノートを書く**（§14.1 の削除根拠になる）。

        `allow_reopen` が偽なら何もしない。**その日のノートは最初の確定時のまま残り、
        あとから来た Part は載らない。**（既定は真である）

        Daily ノートは同じファイル名へ atomic write で上書きされるので**増殖しない**
        （§13.6）。`regenerated_count` が何回作り直したかを持つ。
        """
        if not self.cfg.session.allow_reopen:
            return False
        session = self.database.get_session(session_key)
        if session is None or session.status not in REOPENABLE:
            return False

        try:
            self.database.record_transition(
                EntityType.SESSION,
                session_key,
                from_status=session.status,
                to_status=SessionStatus.MERGING,
                detail="reopen",
                now=self.now,
            )
        except TransitionConflict:
            # 別の経路が先に動かした。**握って次へ進む**（1 件で全体を止めない）
            return False
        self.database.update_session(
            session_key, regenerated_count=session.regenerated_count + 1, now=self.now
        )
        self.log.info(
            "session_reopened",
            session_key=session_key,
            regenerated_count=session.regenerated_count + 1,
        )
        return True

    def _raw_parts(self, session_key: SessionKey) -> list[raw.RawPart]:
        """Raw ノートへ載せる Part（§13.3）。

        **`TRANSCRIBED` 以降の Part を全部載せる。**1 日 32 回書き直される（§10.7）ので、
        毎回その時点で文字起こし済みのものを集め直す。
        """
        built: list[raw.RawPart] = []
        for record in self.database.recordings_for_session(session_key):
            if record.status not in TRANSCRIBED_OR_BEYOND | {PartStatus.RAW_WRITING}:
                continue
            loaded = transcribe.load_transcript(PartKey(record.partkey))
            if loaded is None:
                continue
            started = datetime.fromisoformat(record.started_at)
            built.append(
                raw.RawPart(
                    partkey=record.partkey,
                    started_at=started,
                    ended_at=(datetime.fromisoformat(record.ended_at) if record.ended_at else None),
                    segments=tuple(
                        raw.RawSegment(
                            at=started + _offset(segment.start),
                            end_at=started + _offset(segment.end),
                            text=segment.text,
                        )
                        for segment in loaded.segments
                    ),
                )
            )
        return built

    def _vault_relative(self, path: Path) -> str:
        """Vault ルートからの相対パス（§8.3 の `raw_output_path`）。

        **絶対パスを DB へ入れない。**Vault の置き場所が変わっても鍵が壊れないようにする。
        """
        try:
            return path.relative_to(Path(self.cfg.obsidian.root)).as_posix()
        except ValueError:  # pragma: no cover - 出力は必ず Vault 配下である
            return path.as_posix()

    # --- §10.8 セッション統合 / §10.9 LLM 解析 ---------------------------

    def process_session(self, session_key: SessionKey) -> SessionOutcome:
        """`READY` のセッションを `ANALYZED` まで進める（§11.3）。

        **`build_session_transcript()` は永続化しない**（§10.8）ので、毎回安価に再実行する。
        """
        row = self.database.get_session(session_key)
        if row is None:
            return SessionOutcome.STOPPED
        if row.status in SAVED_OR_BEYOND:
            # **保存済みでも削除の評価だけは進める**（§14.3）。`SAVED` で止まったままだと
            # 削除が無効な運用でも Part が `RAW_SAVED` に残り続ける（§9.3 の `SAVED` 行）
            self.delete_sources_if_safe(session_key)
            return SessionOutcome.SAVED

        transcript = self.build_transcript(session_key)
        if not self.ensure_merged(row, transcript):
            # **`session_empty` と失敗を区別する。**前者はノートを作らず `COMPLETED` で
            # 正常終了であり（§10.8）、呼び手が `FAILED` として扱ってはならない
            after = self.database.get_session(session_key)
            if after is not None and after.status == SessionStatus.COMPLETED:
                return SessionOutcome.EMPTY
            return SessionOutcome.STOPPED
        if transcript is None:  # pragma: no cover - ensure_merged が先に弾く
            return SessionOutcome.STOPPED
        if not self.ensure_analysis(session_key, transcript):
            return SessionOutcome.STOPPED
        if not self.ensure_daily_note(session_key, transcript):
            return SessionOutcome.ANALYZED
        self.delete_sources_if_safe(session_key)
        return SessionOutcome.SAVED

    # --- §14.3 削除 ---------------------------------------------------------

    def delete_sources_if_safe(self, session_key: SessionKey) -> bool:
        """§14.1 を評価し、真なら削除要求を書く（§10.12 / §14.3）。

        戻り値は「このセッションの後始末まで進めたか」である。**保留を返したときは
        `cleanup_staging()` も `COMPLETED` も行わない**（§11.3 / §14.3）。

        **削除が無効なときもここを通る。**§9.3 の `SAVED → CLEANUP`（`delete_source_audio
        == false`）と `RAW_SAVED → COMPLETED`（同）がこの経路であり、**Phase 7 前の
        通常運用はすべてこちら**である。

        **このメソッドはデバイスに触れない。**触れられない（§14.4 N-16）。
        """
        row = self.database.get_session(session_key)
        if row is None or row.status not in DELETE_EVALUATED:
            return False
        parts = self.database.recordings_for_session(session_key)

        # **先に結果を回収する。**`SOURCE_DELETING` のまま残った行を片付けてから
        # 新しい要求を評価しないと、同じ Part の要求が二重に並ぶ（§10.12）
        inventory = device.read_inventory(self.state_root)
        self.collect_delete_results(parts, inventory)
        parts = self.database.recordings_for_session(session_key)

        if not self.cfg.cleanup.delete_source_audio:
            # **安全ロック 1 が掛かっている。**元音声を残したまま完了する（§9.3）
            return self._complete_without_deleting(row, parts)

        requested = 0
        for part in parts:
            if part.status not in {status.value for status in PART_DELETABLE}:
                continue
            request = cleaner.request_part_deletion(
                part,
                row,
                parts,
                self.cfg,
                inventory,
                vault_root=Path(self.cfg.obsidian.root),
                log=self.log,
                now=self.now or datetime.now(self.cfg.tz),
            )
            if request is None:
                continue
            self._transition(
                PartKey(part.partkey), PartStatus.RAW_SAVED, PartStatus.SOURCE_DELETING
            )
            requested += 1

        if requested == 0:
            # §9.3 の「`SAVED` のまま」。**`delete_attempts += 1` で backoff を進める**
            # （ビジーループ防止。§15.2）。**遷移ではないので `events` を書かない**
            self.database.update_session(
                session_key, delete_attempts=row.delete_attempts + 1, now=self.now
            )
            return False

        self._session_transition(session_key, row.status, SessionStatus.SOURCE_DELETING)
        return False

    def collect_delete_results(
        self, parts: list[Recording], inventory: DeviceInventory | None
    ) -> int:
        """reaper の結果を回収し、`SOURCE_DELETING` を進める（§10.12 / §9.3）。

        **結果を 2 系統で確認する。**`queue/result/` は reaper の自己申告であり、
        `state/inventory.json` は ingest が**独立に**走査した結果である。
        **両方が一致して初めて `COMPLETED` にする**（§14.4 N-12 と同じ考え方）。

        タイムアウト（`delete_result_timeout_seconds`）を超えた要求は取り下げ、
        `SOURCE_DELETE_PENDING` にする。**reaper が存在しない場合もここへ落ちる** —
        それは Phase 7 前の正常な状態である。

        戻り値は状態を進めた件数。
        """
        by_key = {part.partkey: part for part in parts}
        moved = 0
        for result in cleaner.read_results(self.cfg):
            part = by_key.get(result.partkey)
            if part is None or part.status != PartStatus.SOURCE_DELETING:
                # 別のセッションのもの、または既に片付いたもの。**捨てない** —
                # そのセッションを処理する周回で回収される
                continue
            if not result.deleted:
                self._delete_pending(part, ErrorCode.SOURCE_IDENTITY_MISMATCH, result.detail)
            elif not cleaner.source_is_gone(part, inventory):
                # **reaper は消したと言うが `inventory.json` にまだ在る**（§10.12）
                self._delete_pending(part, ErrorCode.SOURCE_DELETE_FAILED, "still_in_inventory")
            else:
                self.database.update_recording(
                    PartKey(part.partkey),
                    source_deleted_at=self._moment().isoformat(timespec="seconds"),
                    now=self.now,
                )
                self._transition(
                    PartKey(part.partkey), PartStatus.SOURCE_DELETING, PartStatus.COMPLETED
                )
                self.log.info(
                    "source_deleted", recording_key=part.partkey, request_id=result.request_id
                )
            cleaner.discard_result(result, cfg=self.cfg)
            moved += 1

        moved += self._expire_delete_requests(parts)
        return moved

    def _expire_delete_requests(self, parts: list[Recording]) -> int:
        """結果が来ないまま `delete_result_timeout_seconds` を過ぎた要求を取り下げる。

        **reaper が存在しない場合もここへ落ちる**（安全ロック 2-A）。§10.12 は
        「**これは正常な状態である**（Phase 7 前）」と規定している。
        """
        limit = self.cfg.cleanup.delete_result_timeout_seconds
        moment = self._moment()
        expired = 0
        for part in parts:
            if part.status != PartStatus.SOURCE_DELETING:
                continue
            age = (moment - datetime.fromisoformat(part.updated_at)).total_seconds()
            if age < limit:
                continue
            cleaner.withdraw_request(part.partkey, cfg=self.cfg)
            self._delete_pending(part, ErrorCode.DELETE_TIMEOUT, "no_result")
            expired += 1
        return expired

    def _delete_pending(self, part: Recording, code: ErrorCode, reason: str) -> None:
        """`SOURCE_DELETING → SOURCE_DELETE_PENDING`（§9.3）。**ノートは残す。**"""
        self.database.record_transition(
            EntityType.RECORDING,
            part.partkey,
            from_status=PartStatus.SOURCE_DELETING,
            to_status=PartStatus.SOURCE_DELETE_PENDING,
            error_code=code,
            detail=reason,
            now=self.now,
        )
        self.log.warning("source_delete_pending", recording_key=part.partkey, reason=reason)

    def _moment(self) -> datetime:
        return self.now if self.now is not None else datetime.now(self.cfg.tz)

    def _complete_without_deleting(self, row: Session, parts: list[Recording]) -> bool:
        """`delete_source_audio == false` の経路（§9.3）。**元音声を残したまま完了する。**"""
        for part in parts:
            if part.status == PartStatus.RAW_SAVED:
                self._transition(PartKey(part.partkey), PartStatus.RAW_SAVED, PartStatus.COMPLETED)
        if row.status == SessionStatus.SAVED:
            self._session_transition(row.session_key, SessionStatus.SAVED, SessionStatus.CLEANUP)
        fresh = self.database.get_session(SessionKey(row.session_key))
        if fresh is None or fresh.status != SessionStatus.CLEANUP:
            return False
        for part in parts:
            cleaner.cleanup_staging(part)
        self._session_transition(row.session_key, SessionStatus.CLEANUP, SessionStatus.COMPLETED)
        # **ログを出さない。**§16.4 は `session_completed` を v5.0 で削除し、行き先を
        # 「`events` テーブル（状態遷移そのもの）」と定めている
        return True

    def build_transcript(self, session_key: SessionKey) -> session.SessionTranscript | None:
        """§10.8: `started_at` 昇順に Part transcript を連結する。**永続化しない。**"""
        return session.build_session_transcript(
            self.database.recordings_for_session(session_key),
            day_date=paths.day_of_session(session_key),
            load=lambda record: transcribe.load_transcript(PartKey(record.partkey)),
            gap_seconds=self.cfg.session.block_gap_seconds,
        )

    def ensure_merged(self, row: Session, transcript: session.SessionTranscript | None) -> bool:
        """`READY → MERGING → MERGED`（§9.3 / §10.8）。**冪等。**

        **既に `MERGING` の行も進める。**再オープン（§9.3 の `SAVED` / `COMPLETED` 行）は
        `MERGING` へ戻すので、`READY` だけを受け付けると**作り直しが次の起動まで動かない**
        （`recover_interrupted()` が巻き戻すのは起動時の 1 回だけである）。

        **有効な Part が 0 件なら、ノートを作らずセッションを `COMPLETED` にする**
        （`session_empty`。§10.8）。**`FAILED` にしない** — 無音だけの日は異常ではない。
        """
        session_key = SessionKey(row.session_key)
        if row.status in MERGED_OR_BEYOND:
            return True
        if row.status not in MERGEABLE:
            return False

        parts = self.database.recordings_for_session(session_key)
        excluded = [p for p in parts if p.status in session.EXCLUDED_FROM_MERGE]
        if row.status == SessionStatus.READY:
            self._session_transition(session_key, SessionStatus.READY, SessionStatus.MERGING)

        if transcript is None:
            self._session_transition(session_key, SessionStatus.MERGING, SessionStatus.COMPLETED)
            self.log.info("session_empty", session_key=session_key, parts=len(parts))
            return False

        self.database.update_session(session_key, failed_part_count=len(excluded), now=self.now)
        self._session_transition(session_key, SessionStatus.MERGING, SessionStatus.MERGED)
        self.log.info(
            "session_merged",
            session_key=session_key,
            parts=len(parts) - len(excluded),
            excluded=len(excluded),
            chars=sum(len(segment.text) for segment in transcript.segments),
        )
        return True

    def ensure_analysis(
        self, session_key: SessionKey, transcript: session.SessionTranscript
    ) -> bool:
        """`MERGED → ANALYZING → ANALYZED`（§10.9 / §12）。**冪等。**

        §9.4 の「`analysis_path` が存在しスキーマ検証を通れば再実行しない」を先頭で見る。

        **`LLM_INVALID_JSON` でも文字起こしは失われない。**Raw ノートは既に Vault へ
        保存済みであり、元音声も削除されない（§14.1 が `analysis_path` の妥当性を
        要求するため自動的に保証される）。
        """
        row = self.database.get_session(session_key)
        if row is None:
            return False
        if row.status in ANALYZED_OR_BEYOND:
            return True
        if row.status not in ANALYZABLE:
            return False
        if self._analysis_matches(session_key, transcript):
            # §9.4: 生成物が実在し、**今の統合結果を解析したもの**なら工程を飛ばす
            self._session_transition(session_key, row.status, SessionStatus.ANALYZED)
            return True

        # **既に `ANALYZING` なら遷移を記録しない**（`MERGEABLE` の扱いと同じ）。
        # 戻ってきた行に `MERGED → ANALYZING` を書くと、events に在りもしない遷移が残る
        if row.status == SessionStatus.MERGED:
            self._session_transition(session_key, SessionStatus.MERGED, SessionStatus.ANALYZING)
        result = llm.analyze_session(transcript, cfg=self.cfg)
        if not result.ok or result.result is None:
            self._fail_session(
                session_key,
                SessionStatus.ANALYZING,
                result.error_code or ErrorCode.LLM_FAILED,
                result.error_message,
            )
            return False

        if result.trimmed:
            # **黙って切らない**（§12.2）。上限の 2 倍返し始めても気づけなくなる —
            # 件数の膨張は「録音に無い内容をでっち上げている」徴候でもある（AC-1）
            self.log.info(
                "analysis_trimmed", session_key=session_key, fields="; ".join(result.trimmed)
            )

        target = paths.analysis_path_for(session_key)
        try:
            self._write_analysis(target, result.result)
            # **Map 中間結果を残す**（§10.9 / R-1）。§13.4 の Timeline はこれを並べたもので、
            # 再生成には 18 回の LLM 呼び出しが要る
            daily.save_timeline(
                target,
                daily.build_timeline(
                    partials=result.partials,
                    chunks=result.chunks,
                    transcript=transcript,
                    summary=str(getattr(result.result, "summary", "")),
                ),
                fingerprint=session.transcript_fingerprint(transcript),
            )
            # **指紋は解析本体を書いた後に置く**（§9.4）。順序を逆にすると、
            # 解析の書き込みに失敗したのに指紋だけが残り、**古い解析が「最新」と
            # 判定される。**`_write_analysis` が投げれば指紋は書かれない
            self._write_fingerprint(target, transcript)
        except OSError as exc:
            self._fail_session(
                session_key,
                SessionStatus.ANALYZING,
                ErrorCode.LLM_FAILED,
                f"{type(exc).__name__}: {exc}",
            )
            return False

        self.database.update_session(
            session_key,
            analysis_path=str(target),
            title=getattr(result.result, "title", None),
            error_code=None,
            error_message=None,
            now=self.now,
        )
        self._session_transition(session_key, SessionStatus.ANALYZING, SessionStatus.ANALYZED)
        self.log.info(
            "llm_completed",
            session_key=session_key,
            chunks=len(result.chunks),
            elapsed_s=round(result.elapsed_seconds, 1),
        )
        return True

    def ensure_daily_note(
        self, session_key: SessionKey, transcript: session.SessionTranscript
    ) -> bool:
        """`ANALYZED → WRITING → SAVED`（§10.10 / §10.11 / §13.4）。**冪等。**

        §9.4 の「`output_path` が存在し `output_sha256` と一致すれば飛ばす」を
        保存検証（W-1〜W-9）の再実行で担う。**別の判定を書かない** — #36 の
        `cleaner.py` も同じ `verify_note()` を呼ぶ。

        **リンクの生成失敗は保存検証の合否に影響させない**（§13.8）。
        """
        row = self.database.get_session(session_key)
        if row is None:
            return False
        if row.status in SAVED_OR_BEYOND:
            return True
        if row.status not in WRITABLE or row.analysis_path is None:
            return False

        analysis = self._load_analysis(session_key)
        if analysis is None:
            self._fail_session(
                session_key,
                row.status,
                ErrorCode.OBSIDIAN_WRITE_FAILED,
                f"解析結果を読めません: {row.analysis_path}",
                event="obsidian_failed",
                reason="write",
            )
            return False

        parts = self.database.recordings_for_session(session_key)
        included = [p for p in parts if p.status not in session.EXCLUDED_FROM_MERGE]
        excluded = [p for p in parts if p.status in session.EXCLUDED_FROM_MERGE]
        day = paths.day_of_session(session_key)

        # **既に `WRITING` なら遷移を記録しない**（`ensure_analysis()` と同じ扱い）
        if row.status == SessionStatus.ANALYZED:
            self._session_transition(session_key, SessionStatus.ANALYZED, SessionStatus.WRITING)
        content = self._render_daily(
            analysis,
            session_key=session_key,
            day=day,
            transcript=transcript,
            included=included,
            excluded=excluded,
            recorded_seconds=row.recorded_seconds,
        )
        try:
            result = daily.write_daily_note(
                content,
                day=day,
                session_key=session_key,
                recording_keys=[PartKey(p.partkey) for p in included],
                cfg=self.cfg,
                vault_root=Path(self.cfg.obsidian.root),
            )
        except (OSError, ValueError) as exc:
            self._fail_session(
                session_key,
                SessionStatus.WRITING,
                ErrorCode.OBSIDIAN_WRITE_FAILED,
                f"{type(exc).__name__}: {exc}",
                event="obsidian_failed",
                reason="write",
            )
            return False

        if not result.ok:
            self._fail_session(
                session_key,
                SessionStatus.WRITING,
                ErrorCode.OBSIDIAN_VERIFY_FAILED,
                f"落ちた規則: {', '.join(result.failed_rules)}",
                event="obsidian_failed",
                reason="verify",
            )
            return False

        # **DB 更新が成功するまで `SAVED` とみなさない**（§13.7）
        self.database.update_session(
            session_key,
            output_path=self._vault_relative(Path(result.path)),
            output_sha256=result.sha256,
            error_code=None,
            error_message=None,
            now=self.now,
        )
        self._session_transition(session_key, SessionStatus.WRITING, SessionStatus.SAVED)
        self.log.info(
            "obsidian_saved",
            session_key=session_key,
            path=self._vault_relative(Path(result.path)),
            bytes=Path(result.path).stat().st_size,
        )
        return True

    def _render_daily(
        self,
        analysis: BaseModel,
        *,
        session_key: SessionKey,
        day: date,
        transcript: session.SessionTranscript,
        included: list[Recording],
        excluded: list[Recording],
        recorded_seconds: float | None,
    ) -> str:
        """§13.4 のレンダリング。**リンクの生成が失敗しても保存は続ける**（§13.8）。"""
        links = self._plan_links(analysis, session_key=session_key, day=day)
        # **保存済みの Map 中間結果を優先する**（§10.9 / R-1）。読めなければ §13.4 の
        # 代替経路（Block ごとに `summary` の各文）へ落ちる。**失敗させない** —
        # Timeline は要約の付随物である
        timeline = daily.load_timeline(
            paths.analysis_path_for(session_key),
            fingerprint=session.transcript_fingerprint(transcript),
        )
        if not timeline:
            timeline = daily.build_timeline(
                partials=(),
                chunks=(),
                transcript=transcript,
                summary=str(getattr(analysis, "summary", "")),
            )
        return daily.render_daily_note(
            analysis,
            day=day,
            session_key=session_key,
            recording_keys=[PartKey(p.partkey) for p in included],
            failed_keys=[PartKey(p.partkey) for p in excluded],
            recorded_seconds=recorded_seconds,
            block_count=len(transcript.blocks),
            timeline=timeline,
            links=links,
            cfg=self.cfg,
        )

    def _plan_links(
        self, analysis: BaseModel, *, session_key: SessionKey, day: date
    ) -> wiki.LinkPlan:
        """§13.8。**失敗しても空の計画を返す**（保存検証の合否に影響させない）。"""
        try:
            index = wiki.index_for(self.cfg, Path(self.cfg.obsidian.root))
            tags = getattr(analysis, "tags", None)
            return wiki.plan_links(
                cfg=self.cfg,
                day=day,
                tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
                index=index,
                self_name=daily.daily_filename(self.cfg, day),
                name_for_day=lambda other: daily.daily_filename(self.cfg, other),
                raw_names=daily.raw_note_names(session_key, self.cfg, day),
            )
        except (OSError, ValueError) as exc:
            self.log.debug("obsidian_saved", session_key=session_key, reason=f"link: {exc}")
            return wiki.LinkPlan()

    def _load_analysis(self, session_key: SessionKey) -> BaseModel | None:
        target = paths.analysis_path_for(session_key)
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return llm.build_schema(self.cfg).model_validate(document)
        except ValidationError:
            return None

    def _analysis_matches(
        self, session_key: SessionKey, transcript: session.SessionTranscript
    ) -> bool:
        """`analysis_path` が実在し、スキーマ検証を通り、**今の transcript を解析したもの**か。

        §9.4 の「完了した工程は飛ばす」は、**入力が同じである限り**成り立つ。
        §9.2 の再オープンでは入力が増えるので指紋が変わり、解析はやり直される。

        **名前に「何に対して妥当か」を入れてある。**`_analysis_is_valid` のままだと、
        呼び手が入力を渡さずに済んでしまい、**古い解析をそのままノートへ書く**
        （2026-09-14 に実機で 2 時間 51 分ぶんが Daily ノートから欠けた。#108）。

        **指紋が読めなければ偽を返す。**旧版からの移行と、指紋の書き込みに失敗した場合。
        **正しさを費用より優先する**（§1.3） — 余計に 1 回解析するほうが、
        嘘のノートを残すより良い。
        """
        target = paths.analysis_path_for(session_key)
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        try:
            llm.build_schema(self.cfg).model_validate(document)
        except ValidationError:
            return False
        return self._fingerprint_of(target) == session.transcript_fingerprint(transcript)

    def _fingerprint_of(self, analysis_path: Path) -> str | None:
        """`<slug>.source.json` に記録した指紋。読めなければ `None`。"""
        try:
            document = json.loads(
                paths.analysis_source_path(analysis_path).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        value = document.get("transcript_sha256") if isinstance(document, dict) else None
        return value if isinstance(value, str) else None

    def _write_fingerprint(
        self, analysis_path: Path, transcript: session.SessionTranscript
    ) -> None:
        """解析の入力の指紋を残す（§9.4）。**解析本体を書いた後に呼ぶ。**"""
        paths.analysis_source_path(analysis_path).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "transcript_sha256": session.transcript_fingerprint(transcript),
                    "segments": len(transcript.segments),
                    "blocks": len(transcript.blocks),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_analysis(self, target: Path, analysis: BaseModel) -> None:
        """**`/data` へ書く。Vault ではない**（§4.1 / §8.3）。

        Vault は利用者の同期対象であり、中間生成物を置かない。
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(analysis.model_dump(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _session_transition(self, session_key: str, from_status: str, to_status: str) -> None:
        self.database.record_transition(
            EntityType.SESSION,
            session_key,
            from_status=from_status,
            to_status=to_status,
            now=self.now,
        )

    def _fail_session(
        self,
        session_key: str,
        from_status: str,
        code: ErrorCode,
        detail: str | None,
        *,
        event: str = "llm_failed",
        reason: str | None = None,
    ) -> None:
        """**元音声は削除しない**（§9.3 の `ANALYZING → FAILED` の副作用）。

        削除は §14.1 が `analysis_path` の妥当性を要求するため**自動的に保証される** —
        `FAILED` では `analysis_path` が `NULL` のままである。
        """
        self.database.record_transition(
            EntityType.SESSION,
            session_key,
            from_status=from_status,
            to_status=SessionStatus.FAILED,
            error_code=code,
            error_message=detail,
            now=self.now,
        )
        fields: dict[str, str | None] = {"session_key": session_key, "error_code": code}
        if reason is not None:
            fields["reason"] = reason
        if detail:
            # **理由をログに出す。**`error_code` だけだと `sessions.error_message` を
            # 直接読むまで何が起きたか分からない（2026-09-14 に実機で踏んだ。#112）
            fields["detail"] = detail
        self.log.error(event, **fields)

    # --- 共通の遷移ヘルパ -----------------------------------------------

    def _reload(self, partkey: PartKey) -> Recording | None:
        return self.database.get_recording(partkey)

    def _transition(self, partkey: str, from_status: str, to_status: str) -> None:
        self.database.record_transition(
            EntityType.RECORDING,
            partkey,
            from_status=from_status,
            to_status=to_status,
            now=self.now,
        )

    def _skip(
        self,
        record: Recording,
        code: ErrorCode,
        detail: str | None,
        *,
        from_status: str | None = None,
    ) -> None:
        """`SKIPPED` へ倒す。**失敗ではない**（§10.6 / §9.3）。

        `part_skipped` の `reason` で内訳を表す（§16.4。**専用のイベント名を作らない**）。
        """
        self.database.record_transition(
            EntityType.RECORDING,
            record.partkey,
            from_status=from_status or record.status,
            to_status=PartStatus.SKIPPED,
            error_code=code,
            error_message=detail,
            now=self.now,
        )
        self.log.info(
            "part_skipped", recording_key=record.partkey, reason=SKIP_REASONS.get(code, str(code))
        )

    def _fail(
        self,
        partkey: str,
        from_status: str,
        code: ErrorCode,
        detail: str | None,
        *,
        event: str,
        reason: str | None = None,
    ) -> None:
        self.database.record_transition(
            EntityType.RECORDING,
            partkey,
            from_status=from_status,
            to_status=PartStatus.FAILED,
            error_code=code,
            error_message=detail,
            now=self.now,
        )
        fields: dict[str, str | None] = {"recording_key": partkey, "error_code": code}
        if reason is not None:
            fields["reason"] = reason
        self.log.error(event, **fields)

    def _discard_staging(self, path: StagingPath) -> None:
        try:
            paths.safe_unlink_staging(path, missing_ok=True)
        except (OSError, ValueError) as exc:
            # **失敗しても工程は進む。**容量節約のための削除であり、記録には影響しない
            self.log.warning("disk_space_low", reason=f"staging を消せません: {exc}")


def _offset(seconds: float) -> timedelta:
    return timedelta(seconds=seconds)


# --- §9.4 クラッシュリカバリ --------------------------------------------


def recover_interrupted(database: Database, *, cfg: Config, log: Logger) -> int:
    """進行中で残った行を、その工程の直前の完了状態へ巻き戻す（§9.4）。

    **`SOURCE_DELETE_PENDING` は進行中ではない**（巻き戻しの行き先そのものである）。
    名前が `ING` で終わるため接尾辞だけで判定すると取り違える。逆に `CLEANUP` は
    `ING` で終わらないが進行中である。**だから `states.py` の表を引く。**

    **部分出力の削除もここで行う**（§9.4 の「部分出力を削除してから」）。残すと
    次回の再開で「変換済み」「文字起こし済み」と誤認する。
    """
    moved = 0
    for part_status, part_target in PART_RECOVERY.items():
        for record in _recordings_with_status(database, part_status):
            _discard_partial(record, part_status, cfg=cfg, log=log)
            database.record_transition(
                EntityType.RECORDING,
                record.partkey,
                from_status=part_status,
                to_status=part_target,
                detail="recovery",
            )
            moved += 1
    for session_status, session_target in SESSION_RECOVERY.items():
        for row in database.sessions_with_status(session_status):
            database.record_transition(
                EntityType.SESSION,
                row.session_key,
                from_status=session_status,
                to_status=session_target,
                detail="recovery",
            )
            moved += 1
    if moved:
        log.info("recovery_completed", rolled_back=moved)
    return moved


def _recordings_with_status(database: Database, status: str) -> list[Recording]:
    rows = database.conn.execute(
        "SELECT * FROM recordings WHERE status = ? ORDER BY started_at", (status,)
    ).fetchall()
    from voicedock.db import _from_row

    return [_from_row(Recording, row) for row in rows]


def _discard_partial(record: Recording, status: str, *, cfg: Config, log: Logger) -> None:
    """巻き戻す前に部分出力を消す（§9.4）。"""
    targets: list[Path] = []
    if status == PartStatus.NORMALIZING and record.normalized_path:
        targets.append(Path(record.normalized_path))
    if status == PartStatus.TRANSCRIBING:
        targets.append(Path(paths.transcript_path_for(PartKey(record.partkey))))
    for target in targets:
        try:
            paths.safe_unlink_staging(StagingPath(target), missing_ok=True)
        except (OSError, ValueError) as exc:
            log.warning("config_warning", rule="§9.4", message=f"{target} を消せません: {exc}")


def close_stale_open_sessions(
    database: Database, *, cfg: Config, now: datetime | None = None
) -> int:
    """日付が過去の `OPEN` を `READY` へ（§9.4）。

    **`session.close_open_sessions()` をそのまま使う。**条件 1（過去日）と条件 3
    （起動時のリカバリ）は同じ検査であり、§10.4 の条件 3 は呼ぶ場面の指定である。
    """
    return len(session.close_open_sessions(database, cfg=cfg, now=now))


# ============================================================
# セッション（§10.8〜§10.9, §11.3）
# ============================================================


def process_session(runner: Pipeline, session_key: SessionKey) -> SessionOutcome:
    """§11.3 の `process_session`（`ANALYZED` まで）。

    **Daily ノート以降は #28 / #36 が足す。**ここで `ensure_daily_note()` /
    `delete_sources_if_safe()` を呼ばないのは、**まだ無いから**であって順序の問題ではない。
    """
    return runner.process_session(session_key)


# ============================================================
# リトライ（§15.2）
# ============================================================


def retry_delay(attempt: int, cfg: Config) -> float | None:
    """`attempt` 回目の失敗のあと待つ秒数（§15.2 の `backoff_seconds`）。

    `attempt` は失敗の回数（`retry_count`）で、1 回目の失敗のあとは `backoff_seconds[0]` を
    待つ。**`max_attempts` に達していれば `None`**（もう試さない）。

    **リストが `max_attempts` より短いことは V-9 が起動時に弾く**ので、添字は必ず引ける。
    """
    if attempt < 1 or attempt >= cfg.retry.max_attempts:
        return None
    return float(cfg.retry.backoff_seconds[attempt - 1])


def in_process_retry(
    database: Database,
    entity: EntityType,
    key: str,
    *,
    cfg: Config,
    now: datetime | None = None,
) -> float | None:
    """工程内リトライの可否と待機秒を返す（§15.2）。**状態は変えない。**

    戻り値が `None` なら試さない。理由は 4 つある。

    | 理由 | 判定 |
    |---|---|
    | `FAILED` ではない | 行の状態 |
    | `max_attempts` に達した | `retry_count >= max_attempts` |
    | **`ATTEMPTS` 以外のエラー** | §15.1 の「リトライ」列。契機を待つものは回さない |
    | 戻り先が分からない | `events` に `FAILED` への遷移が無い |

    **待つのは呼び手である。**`pipeline` は時間を持たない（テストが `sleep` を差し替える）。
    """
    row = _status_and_retry(database, entity, key)
    if row is None:
        return None
    status, retry_count, error_code = row
    if status != PartStatus.FAILED:
        return None
    if error_code is None or not _is_attempt_retryable(error_code):
        # **契機待ちのものは工程内で回さない**（§15.1 / §15.2）。`DISK_SPACE_LOW` を
        # 3 秒後に試し直しても空かない
        return None
    if _resume_target(database, entity, key) is None:
        return None
    return retry_delay(retry_count, cfg)


def resume_failed(
    database: Database,
    entity: EntityType,
    key: str,
    *,
    log: Logger,
    reset_retry: bool,
    now: datetime | None = None,
) -> bool:
    """`FAILED` を**直前の進行中状態**へ戻す（§9.3 / §15.2）。

    **リトライは最初からやり直さない**（§15.2）。戻り先は `events` が持つ
    「どの進行中状態から落ちたか」であり、§9.4 の再開規則が完了済みステップを飛ばす。

    `reset_retry` が真なら `retry_count = 0`（**再評価**。デバイス再接続 / サービス起動）。
    偽なら据え置き（**工程内リトライ**。`max_attempts` を効かせる）。
    """
    target = _resume_target(database, entity, key)
    if target is None:
        return False
    try:
        database.record_transition(
            entity,
            key,
            from_status=PartStatus.FAILED,
            to_status=target,
            detail="requeue" if reset_retry else "retry",
            reset_retry=reset_retry,
            now=now,
        )
    except TransitionConflict:
        # 別の経路が先に動かした。**握って次へ進む**（1 件で全体を止めない）
        return False
    return True


def requeue_failed(database: Database, *, log: Logger, now: datetime | None = None) -> int:
    """`FAILED` の Part / Session を `retry_count = 0` で再投入する（§15.2）。

    **上限を持たないのは意図である。**v4.6 は 3 ラウンドを超えた行を「以降は自動で
    触らない」状態に置いていたが、**原因が直っても人が手を動かすまで永久に戻らない。**
    `retry` コマンドも v5.0 で削除したので、上限を残すと復帰経路が無くなる。

    **時間を見ない。**`updated_at` も `backoff_seconds` も参照しない — 契機（呼ばれたこと）
    そのものが判定である。
    """
    moved = 0
    for entity in (EntityType.RECORDING, EntityType.SESSION):
        for key, _count, _code in database.rows_with_status(entity, PartStatus.FAILED):
            if resume_failed(database, entity, key, log=log, reset_retry=True, now=now):
                moved += 1
    if moved:
        log.info("recovery_completed", requeued=moved)
    return moved


def delete_evaluation_delay(attempts: int, cfg: Config) -> float:
    """`SAVED` で削除条件が偽のときの待機秒（§15.2 / §9.3）。

    **5 秒ごとに実ファイル検証を繰り返すビジーループを避ける。**`delete_attempts` が
    リストより大きくなったら最後の値を使い続ける（**無限に伸ばさない**）。

    #36 の `cleaner.py` が使う。**ここに置くのは §15.2 の規定が 1 箇所に集まるようにする
    ため**であり、削除そのものは行わない。
    """
    backoff = cfg.cleanup.delete_evaluation_backoff_seconds
    if not backoff:
        return 0.0
    index = min(max(attempts, 1), len(backoff)) - 1
    return float(backoff[index])


def _status_and_retry(
    database: Database, entity: EntityType, key: str
) -> tuple[str, int, str | None] | None:
    row = database.conn.execute(
        f"SELECT status, retry_count, error_code FROM {entity.table} "  # noqa: S608
        f"WHERE {entity.key_column} = ?",
        (key,),
    ).fetchone()
    return None if row is None else (row["status"], row["retry_count"], row["error_code"])


def _is_attempt_retryable(error_code: str) -> bool:
    """§15.1 の「リトライ」列が `ATTEMPTS` か。**未知のコードは偽**（安全側）。"""
    try:
        return counts_against_max_attempts(ErrorCode(error_code))
    except ValueError:
        return False


def _resume_target(database: Database, entity: EntityType, key: str) -> str | None:
    """`FAILED` からの戻り先（§9.3 の 2 行）。**戻れない状態なら `None`。**"""
    origin = database.failed_from(entity, key)
    if origin is None:
        return None
    allowed = (
        PART_RETRYABLE_FROM_FAILED
        if entity is EntityType.RECORDING
        else SESSION_RETRYABLE_FROM_FAILED
    )
    return origin if origin in {status.value for status in allowed} else None
