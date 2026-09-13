"""削除要求を書く唯一のモジュール（SPEC §14.1 / §14.1.1 / §10.12 / §14.3）。

**絶対ルール: 文字起こし本文が Obsidian に保存・検証される前に元音声を削除してはならない。**

**このモジュールはデバイス上のファイルを削除しない。削除できない。**`DevicePath` は
`PurePosixPath` なので（§11.2）`open()` も `unlink()` も書こうとしても書けず、
`tests/unit/test_no_device_delete.py` が `src/voicedock/*.py` を AST で全走査して
削除呼び出しを落とす（§14.4 N-10 / N-16）。実行するのはホストの `voicedock-reaper` である。

```text
[コンテナ] §14.1 を評価 → queue/delete/<request_id>.json を書く  ← ここ
[reaper]   §14.1.1 の 12 項目を独立検証 → unlink
```

**二重に検証することが v4.0 の設計である。**ここで行うのは事前確認にすぎず、
reaper は**コンテナの判断を一切信用せずに**同じことをデバイス上の実ファイルでやり直す。
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final

from voicedock import notes, paths, transcribe
from voicedock.config import Config
from voicedock.db import Recording, Session
from voicedock.device import DeviceInventory
from voicedock.log import Logger
from voicedock.paths import DevicePath, PartKey, QueuePath, SessionKey, StagingPath
from voicedock.states import PART_DELETABLE, PART_TERMINAL, SESSION_DELETABLE

QUEUE_SCHEMA: Final = 1
"""`queue/delete/<request_id>.json` の `schema`（§14.1.1）。"""

MTIME_TOLERANCE_SECONDS: Final = 2.0
"""更新時刻の許容差（§14.1.1 検証 10）。**reaper と同じ値でなければならない。**"""

DELETE_DIRNAME: Final = "delete"
RESULT_DIRNAME: Final = "result"


@dataclass(frozen=True)
class DeleteTarget:
    """削除要求に載せる 1 ファイル（§14.1.1）。**絶対パスを持たない。**"""

    relpath: DevicePath
    size: int
    mtime: float

    def as_json(self) -> dict[str, object]:
        return {"relpath": str(self.relpath), "size": self.size, "mtime": self.mtime}


@dataclass(frozen=True)
class DeleteRequest:
    """`queue/delete/<request_id>.json`（§14.1.1）。"""

    request_id: str
    created_at: datetime
    device_id: str
    partkey: PartKey
    session_key: SessionKey
    targets: tuple[DeleteTarget, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "schema": QUEUE_SCHEMA,
            "request_id": self.request_id,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "device_id": self.device_id,
            "partkey": str(self.partkey),
            "session_key": str(self.session_key),
            "targets": [target.as_json() for target in self.targets],
        }


# --- §14.1 の論理式 -------------------------------------------------------


def can_delete_source(
    part: Recording,
    session: Session,
    parts: list[Recording],
    cfg: Config,
    inventory: DeviceInventory | None,
    *,
    vault_root: Path,
) -> bool:
    """§14.1 の必要十分条件。**論理式をそのまま写す。**

    **`all()` を使う条件は、対象集合が空でないことを別条件として必ず明示する**
    （空集合の `all()` は真になる。v3.0 の不具合 A-14）。`_orig` 固定で削除対象は
    1 ファイルになったが、**`NULL` / 空文字の番犬は消さない** —
    `os.path.join(volume, "")` は**ボリュームのルートを指す**（§14.1）。

    **DB の `status` を信用しない**（§14.4 N-12）。Raw / Daily ノートは実ファイルを
    読み直して §13.7 を再実行する。
    """
    return (
        # --- 安全ロック（§14.2） ---
        cfg.cleanup.delete_source_audio is True
        and _device_is_writable(inventory) is True
        # --- Raw ノート（文字起こし本文）が保存検証済みで、この Part を含む ---
        and session.raw_output_path is not None
        and verify_raw_note(session, parts, vault_root=vault_root) is True
        and _note_contains(vault_root, session.raw_output_path, part.partkey)
        # --- Daily ノートが保存検証済みで、この Part を含む ---
        and session.status in {status.value for status in SESSION_DELETABLE}
        and session.output_path is not None
        and verify_daily_note(session, parts, cfg, vault_root=vault_root) is True
        and _note_contains(vault_root, session.output_path, part.partkey)
        # --- 解析結果が有効 ---
        and session.analysis_path is not None
        and analysis_is_schema_valid(session, cfg)
        # --- Part 自身の処理が完了 ---
        and part.session_key == session.session_key
        and part.status in {status.value for status in PART_DELETABLE}
        and part.transcript_path is not None
        and part_transcript_is_valid(part)
        # --- 同一 Session の全 Part が終端状態（**空集合を真にしない**） ---
        and len(parts) >= 1
        and all(p.status in {status.value for status in PART_TERMINAL} for p in parts)
        # --- 削除対象ファイルの同定（§14.1.1）。**NULL / 空文字を真にしない** ---
        and part.source_path is not None
        and part.source_path != ""
        and target_is_identical(part, inventory)
    )


def _device_is_writable(inventory: DeviceInventory | None) -> bool:
    """安全ロック 2-B（§14.2）。**`mount_readonly` が偽であること。**

    **不明（`None`）は安全側へ倒す**（§7.5）。`inventory` が読めないときも同じである。
    """
    if inventory is None or inventory.mount_readonly is None:
        return False
    return inventory.mount_readonly is False


def _note_contains(vault_root: Path, relative: str, partkey: str) -> bool:
    """ノートの `voicedock_recording_keys` にこの Part の鍵が載っているか（§14.1）。

    **`part.partkey in frontmatter_keys(note)` の 1 段の論理である**（v5.0→v5.1 の変更 M-1）。
    v5.0 までは整数 ID の 2 段（ノートが 42 を含み、DB が 42 を X だと言う）だった。

    **`device_id` まで含めて一致する。**`NO NAME` のような同名衝突があるので、
    フォルダ名以降だけが一致しても**別デバイスの録音である**（ND-31）。
    """
    return partkey in notes.frontmatter_keys(vault_root / relative)


def verify_raw_note(session: Session, parts: list[Recording], *, vault_root: Path) -> bool:
    """Raw ノートを**実ファイルで**検証し直す（§13.7 R-1〜R-6 / §14.4 N-12）。

    **#23 の `notes.verify_note()` を再利用する。**検証ロジックを 2 本に分けると、
    片方だけ直った状態が生まれる。
    """
    if session.raw_output_path is None or session.raw_output_sha256 is None:
        return False
    results = notes.verify_note(
        vault_root / session.raw_output_path,
        kind=notes.NoteKind.RAW,
        session_key=session.session_key,
        expected_sha=session.raw_output_sha256,
        expected_keys=[p.partkey for p in parts if p.transcript_path is not None],
    )
    return notes.all_passed(results)


def verify_daily_note(
    session: Session, parts: list[Recording], cfg: Config, *, vault_root: Path
) -> bool:
    """Daily ノートを**実ファイルで**検証し直す（§13.7 W-1〜W-9 / §14.4 N-12）。"""
    if session.output_path is None or session.output_sha256 is None:
        return False
    results = notes.verify_note(
        vault_root / session.output_path,
        kind=notes.NoteKind.DAILY,
        session_key=session.session_key,
        expected_sha=session.output_sha256,
        expected_keys=[p.partkey for p in parts if p.transcript_path is not None],
        require_raw_link=cfg.obsidian.wiki.link_raw,
    )
    return notes.all_passed(results)


def analysis_is_schema_valid(session: Session, cfg: Config) -> bool:
    """`analysis_path` が実在しスキーマ検証を通るか（§14.1 / §10.9）。"""
    from pydantic import ValidationError

    from voicedock import llm

    if session.analysis_path is None:
        return False
    try:
        document = json.loads(Path(session.analysis_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    try:
        llm.build_schema(cfg).model_validate(document)
    except ValidationError:
        return False
    return True


def part_transcript_is_valid(part: Recording) -> bool:
    """Part transcript が実在し読めるか（§14.1）。**DB の列だけを根拠にしない。**"""
    if part.transcript_path is None:
        return False
    return transcribe.load_transcript(PartKey(part.partkey)) is not None


# --- §14.1.1 コンテナ側の事前同定 -----------------------------------------


def target_is_identical(part: Recording, inventory: DeviceInventory | None) -> bool:
    """コンテナ側の**事前**確認（§14.1.1）。**実ファイルは見ない。見られない。**

    参照するサイズ・更新時刻は **Helper が `.meta.json` に記録した値**であり、
    実ファイルとの突き合わせは reaper が行う（検証 5・6・7・8・10）。
    `DevicePath` は `PurePosixPath` なので **`stat()` は書けない**。

    コンテナが担えるのは次の 3 つである。

    | | 内容 |
    |---|---|
    | 検証 4・9 | `relpath` の健全性（`..` / 先頭 `/` / `.` 始まり）。`paths.is_safe_relpath()` |
    | 検証 3 | `state/inventory.json` にそのファイルが**現存する**こと |
    | 検証 12 | `<device_id>/<relpath>` が `partkey` と一致すること |
    """
    if inventory is None:
        return False
    if not part.source_path:
        # **空文字を真にしない。**`os.path.join(volume, "")` はボリュームのルートを指す
        return False

    rel = PurePosixPath(part.source_path)
    if not paths.is_safe_relpath(rel):
        return False

    # 検証 12: 鍵と実際に消すパスが一致すること。**文字列を連結して作らない**（§8.5）
    if paths.partkey_for(part.device_id, DevicePath(rel)) != part.partkey:
        return False

    # 検証 3: いま在ること。**「消すものが無くなった」なら要求を書く意味が無い**
    if not inventory.contains(part.device_id, DevicePath(rel)):
        return False

    # 記録が無ければ同定できない（`.meta.json` を読めていない）
    return part.source_size is not None and part.source_mtime is not None


# --- 要求の書き出し（§10.12 / §14.3） -------------------------------------


def request_part_deletion(
    part: Recording,
    session: Session,
    parts: list[Recording],
    cfg: Config,
    inventory: DeviceInventory | None,
    *,
    vault_root: Path,
    log: Logger,
    now: datetime,
) -> DeleteRequest | None:
    """§14.1 が真のときだけ要求を書く（§10.12）。書かなければ `None`。

    **一時名で書いて rename する。**reaper が書き込み途中の JSON を読むと、
    **壊れた要求を「検証できないもの」として扱う経路が要る**ことになる。
    """
    if not can_delete_source(part, session, parts, cfg, inventory, vault_root=vault_root):
        return None

    if part.source_size is None or part.source_mtime is None:
        # `target_is_identical()` が既に弾いているが、**`assert` で書かない** —
        # `python -O` で消える検査に安全を預けない
        return None
    rel = DevicePath(PurePosixPath(str(part.source_path)))
    request = DeleteRequest(
        request_id=_request_id(part.partkey, now),
        created_at=now,
        device_id=part.device_id,
        partkey=PartKey(part.partkey),
        session_key=SessionKey(session.session_key),
        targets=(DeleteTarget(relpath=rel, size=part.source_size, mtime=part.source_mtime),),
    )
    write_request(request, cfg=cfg)
    log.info(
        "delete_requested",
        request_id=request.request_id,
        recording_key=part.partkey,
        session_key=session.session_key,
    )
    return request


def write_request(request: DeleteRequest, *, cfg: Config) -> Path:
    """`queue/delete/<request_id>.json` を書く（§14.1.1）。一時名 → `os.replace`。"""
    directory = Path(cfg.cleanup.queue_root) / DELETE_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{request.request_id}.json"
    tmp = directory / f".{request.request_id}.json.tmp"
    payload = json.dumps(request.as_json(), ensure_ascii=False, indent=2) + "\n"
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(target)  # `os.replace(2)` と同じ。同一 FS 内で atomic である
    return target


def _request_id(partkey: str, now: datetime) -> str:
    """`<UTC の時刻>-<鍵の slug>-<乱数>`（§14.1.1 の例と同じ形）。

    **`request_id` は毎回新しい。**§10.12 は「再試行時は新しい `request_id` で投入する」と
    規定している（reaper の検証 11 がリプレイを拒むため、同じ ID では 2 度と通らない）。
    """
    stamp = now.astimezone(tz=None).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{paths.key_slug(PartKey(partkey))}-{secrets.token_hex(3)}"


def cleanup_staging(part: Recording) -> None:
    """staging の残骸を消す（§14.3）。**`paths.safe_unlink_staging()` を通す**（N-10）。"""
    for column in (part.normalized_path,):
        if column:
            paths.safe_unlink_staging(StagingPath(Path(column)), missing_ok=True)


# --- §10.12 結果の回収 ---------------------------------------------------


DELETED_STATUS: Final = "DELETED"
"""reaper が削除に成功したときの `status`（§14.1.1 の結果の形式）。"""


@dataclass(frozen=True)
class DeleteResult:
    """`queue/result/<request_id>.json`（§14.1.1 / v5.9→v5.10 の変更 X-1）。"""

    path: Path
    request_id: str
    partkey: str
    status: str
    detail: str

    @property
    def deleted(self) -> bool:
        return self.status == DELETED_STATUS


def read_results(cfg: Config) -> list[DeleteResult]:
    """`queue/result/` を読む。**壊れた結果は無視して先へ進む**（§7.5 と同じ方針）。

    **`request_id` を解析して Part を引かない。**結果に載っている `partkey` を使う
    （X-1）。組み立て規則に依存させると、規則を変えた瞬間に結果が迷子になる。
    """
    directory = Path(cfg.cleanup.queue_root) / RESULT_DIRNAME
    if not directory.is_dir():
        return []
    found: list[DeleteResult] = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        partkey = str(document.get("partkey", ""))
        request_id = str(document.get("request_id", ""))
        if not partkey or not request_id:
            continue
        found.append(
            DeleteResult(
                path=path,
                request_id=request_id,
                partkey=partkey,
                status=str(document.get("status", "")),
                detail=str(document.get("detail", "")),
            )
        )
    return found


def source_is_gone(part: Recording, inventory: DeviceInventory | None) -> bool:
    """`state/inventory.json` でも不在を確認する（§10.12）。

    **reaper の自己申告だけを信じない。**`inventory.json` は ingest が**独立に**走査した
    結果なので、両方が一致して初めて `COMPLETED` にできる（§14.4 N-12 と同じ考え方）。

    **読めない（`None`）ときは偽。**不明は安全側へ倒す — 確認できていないものを
    「消えた」ことにしない。
    """
    if inventory is None or not part.source_path:
        return False
    return not inventory.contains(part.device_id, DevicePath(PurePosixPath(part.source_path)))


def withdraw_request(partkey: str, *, cfg: Config) -> int:
    """その Part 宛の削除要求をキューから取り下げる（§9.3 / §10.12）。

    **要求をキューに残したままにしない。**再試行は**新しい `request_id`** で投入する
    （reaper の検証 11 がリプレイを拒むので、同じ ID では 2 度と通らない）。
    """
    directory = Path(cfg.cleanup.queue_root) / DELETE_DIRNAME
    if not directory.is_dir():
        return 0
    removed = 0
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and document.get("partkey") == partkey:
            paths.safe_unlink_queue(QueuePath(path), root=Path(cfg.cleanup.queue_root))
            removed += 1
    return removed


def discard_result(result: DeleteResult, *, cfg: Config) -> None:
    """回収済みの結果を捨てる。**`queue/` はコンテナが rw で持つ**（§18.2）。"""
    paths.safe_unlink_queue(QueuePath(result.path), root=Path(cfg.cleanup.queue_root))
