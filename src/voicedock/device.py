"""`/inbox` の走査とファイル名パース（SPEC §5.2, §5.3, §10.2, §11.1）。

**このモジュールはデバイスに到達しない。**v4.0 以降、コンテナは `/Volumes` を一切
マウントしない（§14.4 N-3）。デバイスの検出・走査・安定性判定・readOnly 再マウントは
ホストの `voicedock-ingest` が行い（§4.2）、コンテナが見るのは Helper が書いた
`/inbox` と `/state` だけである。

**このモジュールは削除しない。**`tests/unit/test_no_device_delete.py` が
`src/voicedock/*.py` を AST で全走査しており、削除呼び出しを書いた時点で落ちる（N-10）。

**パース失敗を例外にしない。**デバイスには DJI 以外のファイルが必ず混ざる
（macOS が作る `._*` / `.Spotlight-V100` / `.Trashes`、ユーザーが置いたメモ）。
`None` を返し、呼び手がログへ落とす。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from voicedock.paths import DevicePath, InboxPath, PartKey, partkey_for

DEFAULT_STATE_ROOT: Final = Path("/state")
INVENTORY_FILENAME: Final = "inventory.json"
META_SUFFIX: Final = ".meta.json"

# --- §5.2 ファイル名パーサ ----------------------------------------------
# **SPEC §5.2 のコードブロックの写しである。**
# `tests/unit/test_device_parse.py::test_patterns_match_spec` が SPEC から読んで
# 突き合わせるので、ここを変えるときは §5.2 も変える。

RECORDING_FILENAME_RE = re.compile(
    r"^(?P<tx>TX\d{2})"
    r"_(?P<mic>MIC\d{3})"
    r"_(?P<date>\d{8})"
    r"_(?P<time>\d{6})"
    r"(?P<orig>_orig)?"
    r"\.(?P<ext>wav|WAV)$"
)

RECORDING_FOLDER_RE = re.compile(r"^TX_(?P<mic>MIC\d{3})_(?P<date>\d{8})_(?P<time>\d{6})$")

Variant = Literal["orig", "denoised"]

ORIG: Final[Variant] = "orig"
DENOISED: Final[Variant] = "denoised"


@dataclass(frozen=True)
class ParsedFilename:
    """§5.2 の 4 フィールド。"""

    transmitter_id: str
    """`TX01` など。**弱い識別子。**恒久 ID として使わない（§8.1 は `partkey` を使う）。"""

    mic_index: int
    started_at: datetime
    """`config.timezone` を付与した aware な datetime（§5.2）。"""

    variant: Variant
    """`_orig` の有無。**`denoised` は取り込まない**（§5.3）。"""


def parse_filename(name: str, *, tz: tzinfo) -> ParsedFilename | None:
    """ファイル名をパースする。失敗したら `None`（§5.2）。

    **例外を投げない。**デバイスには DJI 以外のファイルが必ず混ざるので、
    パース失敗は異常ではなく日常である。呼び手が `unparsable_filename` を出す。

    存在しない日時（`20260230_000000` のような 2 月 30 日）も `None` にする。
    正規表現は桁数しか見ないため、**`datetime` の構築で初めて分かる**。
    """
    matched = RECORDING_FILENAME_RE.match(name)
    if matched is None:
        return None
    try:
        naive = datetime.strptime(  # tz は直後に replace で付ける
            f"{matched.group('date')}{matched.group('time')}", "%Y%m%d%H%M%S"
        )
    except ValueError:
        # 桁は合っていても存在しない日時。**例外にせず「パースできない」に倒す**
        return None
    return ParsedFilename(
        transmitter_id=matched.group("tx"),
        mic_index=int(matched.group("mic").removeprefix("MIC")),
        started_at=naive.replace(tzinfo=tz),
        variant=ORIG if matched.group("orig") else DENOISED,
    )


def is_recording_folder(name: str) -> bool:
    """`RECORDING_FOLDER_RE` に一致するか（§5.2）。"""
    return RECORDING_FOLDER_RE.match(name) is not None


# --- §11.1 の dataclass ------------------------------------------------


@dataclass(frozen=True)
class SourceFile:
    """取り込み対象の `_orig` ファイル 1 本（§5.3）。denoised は候補にならない。"""

    relpath: DevicePath
    """ボリュームルートからの相対パス。**絶対パスを持たない**（§14.1.1）。"""

    inbox_path: InboxPath | None
    """変換後に削除されると `None` になる（§10.5）。"""

    size: int
    mtime: float
    sha256_helper: str
    """Helper がコピーしながら算出した値。コンテナが再計算して照合する（§10.5）。"""


@dataclass(frozen=True)
class PartCandidate:
    """`/inbox` から見つけた Part 候補（§11.1）。まだ DB へ入っていない。"""

    partkey: PartKey
    """`paths.partkey_for(device_id, source.relpath)`（§8.1）。**連結して作らない。**"""

    device_id: str
    transmitter_id: str
    mic_index: int
    started_at: datetime
    source_folder: str
    source: SourceFile


@dataclass(frozen=True)
class DeviceInventory:
    """Helper が書いた `state/inventory.json`（§7.5）。**コンテナの唯一のデバイス視界。**"""

    generated_at: datetime | None
    mount_readonly: bool | None
    """安全ロック 2-B が**実際にかかったか**。**`None`（不明）は安全側へ倒す**（§7.5）。"""

    devices: dict[str, frozenset[DevicePath]]

    def is_connected(self, device_id: str) -> bool:
        """そのデバイスが現在接続されているか（§10.12）。

        **録音が 0 件になったデバイスは `devices` に現れない**（§5.4 規則 5 が
        「録音が 1 件以上ある」ことを要求するため）。つまり偽になるのは
        「消すものが無くなったとき」だけである（§7.5）。
        """
        return device_id in self.devices

    def contains(self, device_id: str, rel: DevicePath) -> bool:
        """そのファイルがデバイス上に現在存在するか（§14.1 / §10.12）。"""
        return rel in self.devices.get(device_id, frozenset())

    @property
    def is_empty(self) -> bool:
        """デバイスが 1 台も無いか。§15.2 の `FAILED` 再評価の契機に使う。"""
        return not self.devices


def read_inventory(state_root: Path = DEFAULT_STATE_ROOT) -> DeviceInventory | None:
    """`state/inventory.json` を読む。読めなければ `None`（§7.5）。

    **例外を投げない。**Helper の書き込み途中を読む可能性があるため、
    ファイルが無い / 読めない / JSON として壊れている / 型が合わない、のいずれも
    `None` にする。`heartbeat.py` と同じ方針である。

    **未知のキーは無視する。**`config.yaml` が未知キーを拒否する（V-1）のと非対称だが、
    `config.yaml` は人が書くのでタイポが問題になり、`inventory.json` は Helper が書くので
    **フィールドが増えてもコンテナが壊れないこと**が優先される（§7.5）。
    """
    path = state_root / INVENTORY_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None

    generated_at: datetime | None = None
    value = document.get("generated_at")
    if isinstance(value, str):
        try:
            generated_at = datetime.fromisoformat(value)
        except ValueError:
            generated_at = None

    mount_readonly = document.get("mount_readonly")
    if not isinstance(mount_readonly, bool):
        mount_readonly = None

    devices: dict[str, frozenset[DevicePath]] = {}
    raw_devices = document.get("devices")
    if isinstance(raw_devices, dict):
        for device_id, files in raw_devices.items():
            if not isinstance(device_id, str) or not isinstance(files, list):
                continue
            devices[device_id] = frozenset(
                DevicePath(PurePosixPath(name)) for name in files if isinstance(name, str)
            )

    return DeviceInventory(
        generated_at=generated_at, mount_readonly=mount_readonly, devices=devices
    )


# --- §10.2 inbox の走査 ------------------------------------------------


@dataclass(frozen=True)
class SkippedEntry:
    """候補にしなかったエントリ。呼び手がログへ落とす（§10.2 / §16.4）。

    **`.` 始まりはここに入れない。**§10.2 は「黙って無視する。ログを出さない」と
    規定している。macOS が FAT32 / exFAT へ書き込むたびに `._<名前>`（AppleDouble）を
    作るため、素直に警告すると **Part ごとにログが埋まる。**
    """

    relpath: str
    event: Literal["part_skipped", "unparsable_filename"]
    reason: str


@dataclass(frozen=True)
class InboxScan:
    """`list_inbox_parts()` の結果。"""

    parts: tuple[PartCandidate, ...]
    skipped: tuple[SkippedEntry, ...]


def list_inbox_parts(inbox_root: Path, *, tz: tzinfo) -> InboxScan:
    """`/inbox` の Part 候補を列挙する（§10.2）。

    **`.wav` と `.wav.meta.json` の両方が揃ったものだけを返す。**§10.2 の 4 状態:

    | inbox の状態 | ここでの扱い |
    |---|---|
    | 両方ある | **Part 候補にする** |
    | `.meta.json` だけ | 候補にしない（Helper の墓標 = 処理済み） |
    | `.wav` だけ | 候補にしない（コピー中か Helper が落ちた痕跡。次回置き直される） |
    | どちらも無い | — |

    **`.meta.json` を起点に走査する。**`.wav` を起点にすると、墓標だけが残った
    ファイル（年 1.2 万件になる）を毎回 `stat` することになる。

    読み取りエラーは当該エントリだけスキップして続行する。**1 個の不良ファイルで
    その日の記録全体を止めない**（§1.3）。
    """
    parts: list[PartCandidate] = []
    skipped: list[SkippedEntry] = []

    for device_dir in _sorted_dirs(inbox_root):
        device_id = device_dir.name
        for folder_dir in _sorted_dirs(device_dir):
            _scan_folder(folder_dir, device_id, tz=tz, parts=parts, skipped=skipped)
        # デバイス直下に置かれた録音も見る（§10.2 の「および device 直下」）
        _scan_folder(device_dir, device_id, tz=tz, parts=parts, skipped=skipped)

    return InboxScan(parts=tuple(parts), skipped=tuple(skipped))


def _sorted_dirs(parent: Path) -> list[Path]:
    """`parent` 直下のディレクトリを名前順に返す。`.` 始まりは黙って除く（§10.2）。

    **出力を決定的にする。**`scandir` の順序は環境依存であり、順序が揺れると
    §10.5 の「古い順に 1 件ずつ直列」の再現性が失われる。
    """
    try:
        entries = sorted(parent.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    found: list[Path] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                found.append(entry)
        except OSError:
            continue
    return found


def _scan_folder(
    folder: Path,
    device_id: str,
    *,
    tz: tzinfo,
    parts: list[PartCandidate],
    skipped: list[SkippedEntry],
) -> None:
    """1 ディレクトリ分の `.meta.json` を見て候補を足す。"""
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name)
    except OSError:
        return

    for meta_path in entries:
        name = meta_path.name
        if name.startswith(".") or not name.endswith(META_SUFFIX):
            continue
        wav_name = name[: -len(META_SUFFIX)]
        wav_path = meta_path.with_name(wav_name)

        parsed = parse_filename(wav_name, tz=tz)
        relpath = _relpath_for(folder, device_id, wav_name)
        if parsed is None:
            skipped.append(
                SkippedEntry(relpath=relpath, event="unparsable_filename", reason="filename")
            )
            continue
        if parsed.variant is not ORIG:
            # v5.0 の `_orig` 固定（§5.3）。Helper は copy しない設計だが、
            # **古い Helper が置いた denoised を黙って取り込まない**ための防壁である
            skipped.append(
                SkippedEntry(relpath=relpath, event="part_skipped", reason="variant_not_orig")
            )
            continue

        try:
            if not wav_path.is_file():
                # 墓標だけがある = 処理済み（§10.2）。**ログを出さない**
                continue
            stat = wav_path.stat()
        except OSError:
            skipped.append(
                SkippedEntry(relpath=relpath, event="part_skipped", reason="inbox_unreadable")
            )
            continue

        meta = _read_meta(meta_path)
        if meta is None:
            skipped.append(
                SkippedEntry(relpath=relpath, event="part_skipped", reason="meta_unreadable")
            )
            continue

        source_folder = folder.name if folder.name != device_id else ""
        device_relpath = meta.get("relpath")
        if not isinstance(device_relpath, str) or not device_relpath:
            skipped.append(
                SkippedEntry(relpath=relpath, event="part_skipped", reason="meta_missing_relpath")
            )
            continue

        try:
            partkey = partkey_for(device_id, DevicePath(PurePosixPath(device_relpath)))
        except ValueError:
            # 壊れた relpath（絶対パス / `..` / `.` 始まり）。**鍵を作らない**（§8.5 / N-17）
            skipped.append(
                SkippedEntry(relpath=relpath, event="part_skipped", reason="meta_unsafe_relpath")
            )
            continue

        parts.append(
            PartCandidate(
                partkey=partkey,
                device_id=device_id,
                transmitter_id=parsed.transmitter_id,
                mic_index=parsed.mic_index,
                started_at=parsed.started_at,
                source_folder=source_folder,
                source=SourceFile(
                    relpath=DevicePath(PurePosixPath(device_relpath)),
                    inbox_path=InboxPath(wav_path),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    sha256_helper=str(meta.get("sha256", "")),
                ),
            )
        )


def _relpath_for(folder: Path, device_id: str, wav_name: str) -> str:
    """ログに出すための inbox 内の相対パス。"""
    if folder.name == device_id:
        return f"{device_id}/{wav_name}"
    return f"{device_id}/{folder.name}/{wav_name}"


def _read_meta(path: Path) -> dict[str, object] | None:
    """`.meta.json` を読む。壊れていれば `None`（§7.5 と同じ方針で例外にしない）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    return document
