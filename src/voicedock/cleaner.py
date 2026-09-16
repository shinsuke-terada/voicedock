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
from voicedock.states import PART_DELETABLE, RAW_NOTE_MEMBERS, STAGING_DISPOSABLE

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

    **根拠は「テキストが 2 か所に独立して存在すること」である**（v5.36→v5.37 の変更 AY-1）。

    - **Vault の Raw ノート** —— `verify_raw_note()` が実ファイルを読み直して
      §13.7 R-1〜R-6 を再実行し、`_note_contains()` がその Part の鍵を確かめる
    - **`/data/transcripts/parts/`** —— `part_transcript_is_valid()`。
      `retain_transcript_days: 0` で**無期限保持**され、Raw ノートの再生成元になる

    **Daily ノートと解析結果は条件にしない。**要約は元音声を使わないので、
    **要約の成否を記録保全の根拠にする理由が無い。**条件にしていると、LLM が
    落ちているあいだ**本文は Vault に在るのにデバイスの容量が永久に解放されない。**

    **全 Part 終端も条件にしない。**1 本詰まるとその日ぶん丸ごと解放されない
    （#131 / #133 と同じ「1 件の失敗が全体を止める」形）。**Part ごとに評価する。**

    **`NULL` / 空文字の番犬は消さない** — `os.path.join(volume, "")` は
    **ボリュームのルートを指す**（§14.1）。

    **DB の `status` を信用しない**（§14.4 N-12）。Raw ノートは実ファイルを
    読み直して §13.7 を再実行する。
    """
    return (
        # --- 安全ロック（§14.2） ---
        cfg.cleanup.delete_source_audio is True
        and device_is_writable(inventory) is True
        # --- Raw ノート（文字起こし本文）が保存検証済みで、この Part を含む ---
        and session.raw_output_path is not None
        and verify_raw_note(session, parts, vault_root=vault_root) is True
        and _note_contains(vault_root, session.raw_output_path, part.partkey)
        # --- Part 自身の処理が完了（**transcript がテキストの 2 つ目のコピーである**） ---
        and part.session_key == session.session_key
        and part.status in {status.value for status in PART_DELETABLE}
        and part.transcript_path is not None
        and part_transcript_is_valid(part)
        # --- Part を 1 つも持たない Session を真にしない ---
        and len(parts) >= 1
        # --- 削除対象ファイルの同定（§14.1.1）。**NULL / 空文字を真にしない** ---
        and part.source_path is not None
        and part.source_path != ""
        and target_is_identical(part, inventory)
    )


def device_is_writable(inventory: DeviceInventory | None) -> bool:
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

    **R-6 が要求する鍵は `RAW_NOTE_MEMBERS` に合わせる**（v5.46→v5.47 の変更 BI-1）。
    v5.46 までは「`transcript_path` があれば要求する」だったが、**書き手
    （`pipeline._raw_parts()`）は `FAILED` を載せない。**そのため文字起こしまで進んで
    Raw ノートの書き込みで落ちた Part が 1 本あるだけで R-6 が偽になり、
    **同じ日の他の Part がすべて削除不可になった** —— `can_delete_source()` の
    docstring が「Part ごとに評価するから起きない」と書いている形そのものである。

    **緩めても安全性は落ちない。**「そのノートがこの Part を含むか」は
    `_note_contains()` が別途確かめており、R-6 が見ているのは
    **「そのノートがいまのセッションのものか」**である。
    """
    if session.raw_output_path is None or session.raw_output_sha256 is None:
        return False
    results = notes.verify_note(
        vault_root / session.raw_output_path,
        kind=notes.NoteKind.RAW,
        session_key=session.session_key,
        expected_sha=session.raw_output_sha256,
        expected_keys=[
            p.partkey
            for p in parts
            if p.transcript_path is not None and p.status in RAW_NOTE_MEMBERS
        ],
    )
    return notes.all_passed(results)


# **`verify_daily_note()` と `analysis_is_schema_valid()` は v5.47 で削除した**（変更 BI-2）。
#
# どちらも **AY-1（v5.37）で §14.1 の条件から外れた**ものの残骸である ——
# 削除の根拠は「テキストが Vault に確実に残っていること」であり、
# **要約の成否を記録保全の根拠にしない**（要約は元音声を使わない）。
# 呼び手はその時点で消えており、以後 1 行も実行されていなかった。
#
# **残すほうが危なかった。**`verify_daily_note()` は W-9 を
# `require_raw_link=cfg.obsidian.wiki.link_raw` で見ており、
# **書き手（`daily.write_daily_note()`）の `not include_transcript` と既にズレていた。**
# W-8 の見出しも既定の `## Summary` 決め打ちで、設定した見出しを見ていない。
# **配線した瞬間に、書き手と違う判定を返す。**安全機構の顔をした死んだコードである。


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
    """staging の残骸を消す（§14.3）。**`paths.safe_unlink_staging()` を通す**（N-10）。

    **`FAILED` の Part には触れない**（v5.32→v5.33 の変更 AU-1）。その 16 kHz 音声は
    残骸ではなく**再試行の入力**であり、`inbox_retain: normalized`（既定）では
    inbox の原本が既に無いので、**消すと二度と復旧できない。**§10.2 の墓標により
    デバイスを挿し直しても再コピーされないため、手で墓標を外す以外に方法が無くなる。

    **絞りをここに置く。**呼び出し側（`Pipeline._complete_without_deleting()` と
    §14.3 の削除経路）でループ条件を足すと、**片方だけ直る。**

    **許可リストで判定する。**`STAGING_DISPOSABLE` に無い状態は、それが `PART_TERMINAL`
    ですらない異常な入力であっても**消さない**のが安全側である。
    """
    if part.status not in STAGING_DISPOSABLE:
        return
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
    completed_at: datetime | None = None
    """reaper が結果を書いた時刻。**`inventory.json` の新旧を判定するのに使う**（#156）。"""

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
                completed_at=_parse_time(document.get("completed_at")),
            )
        )
    return found


def _parse_time(value: object) -> datetime | None:
    """ISO 8601 を `datetime` へ。**読めなければ `None`**（不明は安全側。§7.5）。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def inventory_is_newer(inventory: DeviceInventory | None, result: DeleteResult) -> bool | None:
    """`inventory.json` が結果より新しいか。**判定できなければ `None`**（#156）。

    **`inventory` は ingest が 5 分ごとに書き、reaper は ingest の最後に走る。**
    したがって**削除の直後は必ず inventory のほうが古い。**古い inventory は
    その削除について**何も言えない** —— それを「まだ在る＝失敗」と読むと、
    **成功した削除が毎回ちょうど 1 度失敗として記録される。**

    #148 / #107 と同じ型である —— **「まだ観測していない」と「そうでない」を
    区別しない。**
    """
    if inventory is None or inventory.generated_at is None or result.completed_at is None:
        return None
    return inventory.generated_at >= result.completed_at


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


def withdraw_result(partkey: str, *, cfg: Config) -> int:
    """その Part 宛の結果をキューから取り下げる（§10.12 / #160）。

    **`withdraw_request()` の対である。**要求だけ取り下げて結果を残すと、
    **対応する試行が存在しない結果が溜まり続け**、`status` の `awaiting result` が
    実態と食い違う。
    """
    directory = Path(cfg.cleanup.queue_root) / RESULT_DIRNAME
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


# **`outstanding_request_id()` は v5.46 で削除した**（変更 BH-1）。
#
# あれは `queue/delete/*.json` を走査して「その Part にいま出ている要求」を求めていたが、
# **「キューが唯一の出所である」という前提が偽だった** —— `queue/delete/<id>.json` は
# **reaper が所有し、処理した瞬間に消す**（`voicedock-reaper` の `rm -f "$file"`）。
# **結果が届くとき要求は必ず 0 件**なので、照合は常に失敗し、
# **DELETED も SOURCE_IDENTITY_MISMATCH もすべて捨てていた。**Part は 1 時間後に
# `DELETE_TIMEOUT` で保留へ落ち、**本当の理由が隠れた。**
#
# いまは `recordings.delete_request_id` を見る。**コンテナの事実はコンテナが持つ** ——
# 二重管理を嫌ってキューを読みにいったが、あれはコンテナの台帳ではなかった。
#
# **X-1 は維持している。**あれは「`request_id` を**解析**して Part を引かない」
# （組み立て規則を変えたときに結果が迷子にならないようにする）という話であり、
# **「その結果がこの要求のものか」を確かめない理由にはならない。**


def discard_result(result: DeleteResult, *, cfg: Config) -> None:
    """回収済みの結果を捨てる。**`queue/` はコンテナが rw で持つ**（§18.2）。"""
    paths.safe_unlink_queue(QueuePath(result.path), root=Path(cfg.cleanup.queue_root))
