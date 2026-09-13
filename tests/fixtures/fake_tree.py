"""偽 inbox と偽ボリュームを `tmp_path` へ組み立てる（SPEC §20.2 / §10.2 / §7.5）。

**committed tree にしない。**WAV は `.gitignore` の対象であり、`.meta.json` は**その WAV の
sha256 を含む**ため、コミットした tree では整合が必ず壊れる。プログラムで組み立てれば
「git 管理下の fixture を直接使ってはならない」（§20.2）も構造的に満たされる。

録音の名前・Variant の構成・OS が作るノイズは、すべて **`docs/POC.md` §6.2 / §6.3 の実測**に
合わせている。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final

from tests.fixtures.make_wav import Content, WavFormat, write_wav
from voicedock.paths import DevicePath, PartKey, partkey_for

DEVICE_ID: Final = "DJIMIC3"
"""実機のボリューム名（`docs/POC.md` §6.1。出荷時の `NO NAME` から改名したもの）。"""


def _helper_version() -> str:
    """`helper/voicedock-ingest` の `HELPER_VERSION` を読む。

    **直書きしない。**v5.2 まではここに `"4.4.0"` と書いてあり、**実装が無かったため
    誰も気づかないまま古くなっていた。**実ファイルから読めば食い違えない。
    """
    script = Path(__file__).parents[2] / "helper" / "voicedock-ingest"
    matched = re.search(r'^HELPER_VERSION="([^"]+)"', script.read_text(encoding="utf-8"), re.M)
    if matched is None:  # pragma: no cover - 実装が壊れたときだけ通る
        raise RuntimeError(f"HELPER_VERSION を読み取れません: {script}")
    return matched.group(1)


HELPER_VERSION: Final = _helper_version()
META_SCHEMA: Final = 1
DEFAULT_COPIED_AT: Final = datetime(2026, 9, 13, 9, 0, 0, tzinfo=UTC)

ORIG: Final = "orig"
DENOISED: Final = "denoised"


@dataclass(frozen=True)
class FakeRecording:
    """1 つの録音（同じ `part_key` の Variant をまとめたもの。§5.3）。"""

    folder: str
    """`TX_MIC<NNN>_<YYYYMMDD>_<HHMMSS>`（§5.1）。"""

    stem: str
    """`TX<NN>_MIC<NNN>_<YYYYMMDD>_<HHMMSS>`（`_orig` と `.wav` を除いた部分）。"""

    variants: tuple[str, ...]
    seconds: float
    content: Content

    def filename(self, variant: str) -> str:
        """`_orig` が付いていれば原音、付いていなければノイズ除去済み（§5.1）。"""
        suffix = "_orig" if variant == ORIG else ""
        return f"{self.stem}{suffix}.wav"

    def relpath(self, variant: str) -> str:
        """ボリュームルートからの相対パス（`device_id` を含めない。§14.1.1）。"""
        return f"{self.folder}/{self.filename(variant)}"

    def partkey(self, variant: str = ORIG, *, device_id: str = DEVICE_ID) -> PartKey:
        """この録音の `partkey`（§8.1）。

        **`paths.partkey_for()` を通す。**fixture の中で文字列を連結すると、算出規則を
        変えてもテストが落ちず、**§8.5 の唯一の禁則が守られていることを確かめられなくなる。**

        既定が `ORIG` なのは、v5.0 以降 `_orig` だけが取り込まれるためである（§5.3）。
        """
        return partkey_for(device_id, DevicePath(PurePosixPath(self.relpath(variant))))

    def tone_hz(self, variant: str) -> float:
        """Variant ごとに中身を変える。**同じ録音の 2 Variant は別ファイルである。**"""
        return 220.0 if variant == ORIG else 330.0


DEFAULT_RECORDINGS: Final[tuple[FakeRecording, ...]] = (
    # 実機の実測そのもの（POC §6.2）。TX00 が実在し、Variant は _orig だけだった
    FakeRecording(
        folder="TX_MIC001_20260912_120950",
        stem="TX00_MIC001_20260912_120950",
        variants=(ORIG,),
        seconds=1.5,
        content=Content.SPEECH,
    ),
    # §5.3 の「両 Variant」分岐。transcribe_variant の選択がここで効く
    FakeRecording(
        folder="TX_MIC001_20260912_163444",
        stem="TX00_MIC001_20260912_163444",
        variants=(ORIG, DENOISED),
        seconds=2.0,
        content=Content.SPEECH,
    ),
    # §5.3 の「denoised のみ」分岐 + #21 の NO_SPEECH_DETECTED。**翌日**なので
    # §10.4 のセッション分組（日単位）のテストにそのまま使える
    FakeRecording(
        folder="TX_MIC002_20260913_090000",
        stem="TX01_MIC003_20260913_090000",
        variants=(DENOISED,),
        seconds=1.0,
        content=Content.SILENCE,
    ),
)

NOISE_APPLEDOUBLE: Final = "._TX00_MIC001_20260912_120950_orig.wav"
NOISE_UNPARSABLE: Final = "NOTES.txt"
NOISE_TRASHED_FOLDER: Final = "TX_MIC001_20260901_101010"
NOISE_TRASHED_FILE: Final = "TX00_MIC001_20260901_101010_orig.wav"
ORPHAN_FOLDER: Final = "TX_MIC001_20260914_000000"
ORPHAN_FILE: Final = "TX00_MIC001_20260914_000000_orig.wav"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_meta(wav: Path, recording: FakeRecording, variant: str, copied_at: datetime) -> Path:
    """`<name>.wav.meta.json` を書く（§10.2）。

    **WAV を書いた直後に実ファイルから** size / mtime / sha256 を読む。ここがずれると
    §10.5 のハッシュ照合テストが無意味になる。
    """
    stat = wav.stat()
    meta = {
        "schema": META_SCHEMA,
        "device_id": DEVICE_ID,
        "relpath": recording.relpath(variant),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "sha256": _sha256(wav),
        "copied_at": copied_at.isoformat(),
        "helper_version": HELPER_VERSION,
    }
    path = wav.with_name(f"{wav.name}.meta.json")
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_noise(device_root: Path, *, fmt: WavFormat) -> None:
    """macOS がデバイス上に作るものを置く（`docs/POC.md` §6.3）。"""
    first = DEFAULT_RECORDINGS[0]
    # AppleDouble。正規表現に一致しないが、`.` 始まりなので黙って無視される（§10.2）
    (device_root / first.folder / NOISE_APPLEDOUBLE).write_bytes(b"Mac OS X\0" * 4)
    # Spotlight / FSEvents
    for name in (".Spotlight-V100", ".fseventsd"):
        folder = device_root / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "dummy").write_bytes(b"\0")
    # ゴミ箱。**正規表現に一致する録音が入っている**（一致しても走査してはならない）
    write_wav(
        device_root / ".Trashes" / NOISE_TRASHED_FOLDER / NOISE_TRASHED_FILE,
        seconds=0.5,
        fmt=fmt,
        content=Content.SPEECH,
    )
    # パースに失敗するもの（WARN unparsable_filename になる）
    (device_root / first.folder / NOISE_UNPARSABLE).write_text("memo\n", encoding="utf-8")


def build_fake_inbox(
    root: Path,
    recordings: tuple[FakeRecording, ...] = DEFAULT_RECORDINGS,
    *,
    fmt: WavFormat = WavFormat.PCM24,
    with_noise: bool = True,
    copied_at: datetime = DEFAULT_COPIED_AT,
) -> Path:
    """`<root>/<device_id>/<folder>/<name>.wav` と `.meta.json` を組み立てる（§10.2）。

    Helper が置いた状態を再現する。**`import.inbox_root` にこの `root` を与える。**
    """
    device_root = root / DEVICE_ID
    for recording in recordings:
        for variant in recording.variants:
            wav = write_wav(
                device_root / recording.folder / recording.filename(variant),
                seconds=recording.seconds,
                fmt=fmt,
                content=recording.content,
                tone_hz=recording.tone_hz(variant),
            )
            _write_meta(wav, recording, variant, copied_at)

    if with_noise:
        _write_noise(device_root, fmt=fmt)
        # **`.meta.json` を持たない `.wav`。**コピー中か Helper が落ちた状態であり、
        # コンテナは Part 候補にしてはならない（§10.2）
        write_wav(
            device_root / ORPHAN_FOLDER / ORPHAN_FILE,
            seconds=0.5,
            fmt=fmt,
            content=Content.SPEECH,
        )
    return root


def build_fake_volumes(
    root: Path,
    recordings: tuple[FakeRecording, ...] = DEFAULT_RECORDINGS,
    *,
    fmt: WavFormat = WavFormat.PCM24,
    with_noise: bool = True,
) -> Path:
    """偽ボリュームを組み立てる（reaper のテスト用。§20.4）。

    **`.meta.json` は置かない。**デバイス上には存在しないものである（Helper が inbox 側へ
    書く）。#54（T-50）と #37 がここへ `queue/delete/*.json` を書いて reaper を走らせる。
    """
    device_root = root / DEVICE_ID
    for recording in recordings:
        for variant in recording.variants:
            write_wav(
                device_root / recording.folder / recording.filename(variant),
                seconds=recording.seconds,
                fmt=fmt,
                content=recording.content,
                tone_hz=recording.tone_hz(variant),
            )
    if with_noise:
        _write_noise(device_root, fmt=fmt)
    return root


def write_state(root: Path, **fields: Any) -> Path:
    """`state/heartbeat.json` を書く（§7.5）。既定は**ロックが全部掛かった状態**。"""
    root.mkdir(parents=True, exist_ok=True)
    heartbeat: dict[str, Any] = {
        "schema": 1,
        "updated_at": DEFAULT_COPIED_AT.isoformat(),
        "helper_version": HELPER_VERSION,
        "mount_mode": "ro",
        "mount_readonly": True,
        "delete_source_audio": False,
        "reaper_installed": False,
        "include_volumes": [],
        "exclude_volumes": ["Macintosh HD", "com.apple.TimeMachine.*", ".*"],
        "config_error": None,
    }
    heartbeat.update(fields)
    path = root / "heartbeat.json"
    path.write_text(json.dumps(heartbeat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
