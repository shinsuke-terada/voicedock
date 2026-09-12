"""偽 inbox / 偽ボリュームが SPEC §10.2 / §5.3 / §14.1.1 の前提を満たすことを固定する。

**この fixture が間違っていると、それを使う全テストが静かに無意味になる。**とくに
`.meta.json` の sha256 が実ファイルと合っていないと、§10.5 のコピー検証テストが
「常に不一致」または「常に一致」になって意味を失う。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.fixtures.fake_tree import (
    DEFAULT_RECORDINGS,
    DENOISED,
    DEVICE_ID,
    NOISE_APPLEDOUBLE,
    NOISE_TRASHED_FILE,
    NOISE_TRASHED_FOLDER,
    NOISE_UNPARSABLE,
    ORIG,
    ORPHAN_FILE,
    build_fake_inbox,
    write_state,
)
from tests.helpers import REPO_ROOT
from voicedock.paths import is_safe_relpath

# §5.2 の正規表現（#14 が device.py へ実装する。ここでは fixture の検証に使う）
FOLDER_RE = re.compile(r"^TX_MIC(?P<mic>\d{3})_(?P<date>\d{8})_(?P<time>\d{6})$")
FILE_RE = re.compile(
    r"^TX(?P<tx>\d{2})_MIC(?P<mic>\d{3})_(?P<date>\d{8})_(?P<time>\d{6})(?P<orig>_orig)?\.wav$",
    re.IGNORECASE,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metas(inbox: Path) -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(inbox.rglob("*.wav.meta.json"))
    ]


# --- inbox の構造 --------------------------------------------------------


def test_inbox_layout(fake_inbox: Path) -> None:
    """`<root>/<device_id>/<folder>/<name>.wav` と同名の `.meta.json`（§10.2）。"""
    device_root = fake_inbox / DEVICE_ID
    assert device_root.is_dir()
    for recording in DEFAULT_RECORDINGS:
        for variant in recording.variants:
            wav = device_root / recording.folder / recording.filename(variant)
            assert wav.is_file(), wav
            assert wav.with_name(f"{wav.name}.meta.json").is_file()


def test_names_match_the_spec_regexes(fake_inbox: Path) -> None:
    """フォルダ名とファイル名が §5.2 の正規表現に一致すること（実機実測準拠）。"""
    for recording in DEFAULT_RECORDINGS:
        assert FOLDER_RE.match(recording.folder), recording.folder
        for variant in recording.variants:
            assert FILE_RE.match(recording.filename(variant)), recording.filename(variant)


def test_real_device_recording_is_reproduced() -> None:
    """1 件目が実機の実測そのものであること（`docs/POC.md` §6.2）。

    **`TX00` が実在し、Variant は `_orig` だけだった。**
    """
    first = DEFAULT_RECORDINGS[0]
    assert first.folder == "TX_MIC001_20260912_120950"
    assert first.filename(ORIG) == "TX00_MIC001_20260912_120950_orig.wav"
    assert first.variants == (ORIG,)


# --- .meta.json ----------------------------------------------------------


def test_meta_matches_the_file(fake_inbox: Path) -> None:
    """`size` / `sha256` が実ファイルと一致すること。

    **ここがずれると §10.5 のコピー検証テストが無意味になる。**
    """
    for path in sorted((fake_inbox / DEVICE_ID).rglob("*.wav.meta.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        wav = path.with_name(path.name.removesuffix(".meta.json"))
        assert wav.is_file(), wav
        assert meta["size"] == wav.stat().st_size
        assert meta["sha256"] == sha256(wav)
        assert meta["mtime"] == pytest.approx(wav.stat().st_mtime)


def test_meta_relpath_has_no_device_id(fake_inbox: Path) -> None:
    """`relpath` はボリュームルートからの相対（`device_id` を含めない。§14.1.1）。"""
    for meta in metas(fake_inbox):
        relpath = str(meta["relpath"])
        assert not relpath.startswith(DEVICE_ID)
        assert relpath.count("/") == 1, relpath


def test_meta_relpath_is_safe(fake_inbox: Path) -> None:
    """全 `relpath` が `paths.is_safe_relpath()` を通ること（§14.4 N-17）。"""
    from pathlib import PurePosixPath

    for meta in metas(fake_inbox):
        assert is_safe_relpath(PurePosixPath(str(meta["relpath"]))), meta["relpath"]


def test_meta_has_the_spec_schema(fake_inbox: Path) -> None:
    expected = {
        "schema",
        "device_id",
        "relpath",
        "size",
        "mtime",
        "sha256",
        "copied_at",
        "helper_version",
    }
    for meta in metas(fake_inbox):
        assert set(meta) == expected


# --- Variant の構成（§5.3） ----------------------------------------------


def test_variants_cover_every_branch() -> None:
    """`orig のみ` / `両方` / `denoised のみ` の 3 分岐が揃っていること（§5.3）。"""
    assert [r.variants for r in DEFAULT_RECORDINGS] == [
        (ORIG,),
        (ORIG, DENOISED),
        (DENOISED,),
    ]


def test_orig_and_denoised_differ(fake_inbox: Path) -> None:
    """同じ録音の 2 Variant が別の内容であること（別ファイルなので）。"""
    both = DEFAULT_RECORDINGS[1]
    folder = fake_inbox / DEVICE_ID / both.folder
    assert sha256(folder / both.filename(ORIG)) != sha256(folder / both.filename(DENOISED))


def test_has_a_silent_recording() -> None:
    """無音の録音が 1 件あること（#21 の `NO_SPEECH_DETECTED` で使う）。"""
    assert any(r.content == "silence" for r in DEFAULT_RECORDINGS)


def test_spans_two_days() -> None:
    """`started_at` が 2 日にまたがること（§10.4 のセッション分組に使える）。"""
    days = {FILE_RE.match(r.filename(r.variants[0])).group("date") for r in DEFAULT_RECORDINGS}  # type: ignore[union-attr]
    assert days == {"20260912", "20260913"}


# --- OS のノイズ（POC §6.3） --------------------------------------------


def test_appledouble_is_present(fake_inbox: Path) -> None:
    """AppleDouble。`.` 始まりなので黙って無視される（§10.2）。"""
    first = DEFAULT_RECORDINGS[0]
    assert (fake_inbox / DEVICE_ID / first.folder / NOISE_APPLEDOUBLE).is_file()
    assert not FILE_RE.match(NOISE_APPLEDOUBLE)


@pytest.mark.parametrize("name", [".Spotlight-V100", ".fseventsd"])
def test_os_index_dirs_are_present(fake_inbox: Path, name: str) -> None:
    assert (fake_inbox / DEVICE_ID / name).is_dir()


def test_trashes_contains_a_parsable_recording(fake_inbox: Path) -> None:
    """**正規表現に一致する録音が `.Trashes` に入っていること。**

    「一致するのに走査してはならない」（§10.2）を試せる形にしておく。
    """
    trashed = fake_inbox / DEVICE_ID / ".Trashes" / NOISE_TRASHED_FOLDER / NOISE_TRASHED_FILE
    assert trashed.is_file()
    assert FOLDER_RE.match(NOISE_TRASHED_FOLDER)
    assert FILE_RE.match(NOISE_TRASHED_FILE)


def test_unparsable_file_is_present(fake_inbox: Path) -> None:
    """`WARN unparsable_filename` になるもの（§10.2）。"""
    first = DEFAULT_RECORDINGS[0]
    assert (fake_inbox / DEVICE_ID / first.folder / NOISE_UNPARSABLE).is_file()
    assert not FILE_RE.match(NOISE_UNPARSABLE)


def test_wav_without_meta_exists(fake_inbox: Path) -> None:
    """`.meta.json` を持たない `.wav` が 1 件あること。

    コピー中か Helper が落ちた状態であり、**コンテナは Part 候補にしてはならない**（§10.2）。
    """
    orphans = [
        path
        for path in (fake_inbox / DEVICE_ID).rglob("*.wav")
        if not path.with_name(f"{path.name}.meta.json").exists()
        # `.Trashes` 配下と `.` 始まり（AppleDouble）は別の規則で落ちるので除く
        and not any(part.startswith(".") for part in path.parts)
    ]
    assert [path.name for path in orphans] == [ORPHAN_FILE]


def test_without_noise(tmp_path: Path) -> None:
    """`with_noise=False` ならノイズも orphan も置かないこと。"""
    root = build_fake_inbox(tmp_path / "clean", with_noise=False)
    assert not (root / DEVICE_ID / ".Trashes").exists()
    wavs = sorted(path.name for path in (root / DEVICE_ID).rglob("*.wav"))
    assert ORPHAN_FILE not in wavs


# --- 偽ボリューム（reaper 用） ------------------------------------------


def test_volumes_have_no_meta(fake_volumes: Path) -> None:
    """デバイス上に `.meta.json` は存在しない（Helper が inbox 側へ書くもの）。"""
    assert list((fake_volumes / DEVICE_ID).rglob("*.meta.json")) == []
    assert list((fake_volumes / DEVICE_ID).rglob("*.wav"))


def test_volumes_layout(fake_volumes: Path) -> None:
    for recording in DEFAULT_RECORDINGS:
        for variant in recording.variants:
            assert (
                fake_volumes / DEVICE_ID / recording.folder / recording.filename(variant)
            ).is_file()


# --- state（§7.5） ------------------------------------------------------


def test_write_state_defaults_to_all_locks_engaged(tmp_path: Path) -> None:
    """既定は**ロックが全部掛かった状態**（§14.2）。"""
    path = write_state(tmp_path / "state")
    heartbeat = json.loads(path.read_text(encoding="utf-8"))
    assert heartbeat["mount_mode"] == "ro"
    assert heartbeat["mount_readonly"] is True
    assert heartbeat["delete_source_audio"] is False
    assert heartbeat["reaper_installed"] is False


def test_write_state_can_override(tmp_path: Path) -> None:
    path = write_state(tmp_path / "state", delete_source_audio=True, mount_readonly=False)
    heartbeat = json.loads(path.read_text(encoding="utf-8"))
    assert heartbeat["delete_source_audio"] is True
    assert heartbeat["mount_readonly"] is False


# --- git 管理下を汚さないこと --------------------------------------------


def test_builds_under_tmp_path_only(fake_inbox: Path, fake_volumes: Path, tmp_path: Path) -> None:
    """組み立て先が `tmp_path` 配下だけであること（§20.2）。

    git 管理下を直接与えると、削除テストが実 fixture を消す。
    """
    assert tmp_path in fake_inbox.parents or fake_inbox == tmp_path / "inbox"
    assert tmp_path in fake_volumes.parents or fake_volumes == tmp_path / "volumes"

    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        # make test のコンテナには git も .git も無い。ホストの make test / lint で見る
        pytest.skip("git を使えない環境")
    result = subprocess.run(  # noqa: S603
        [git, "status", "--porcelain", "tests/fixtures/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", f"git 管理下が汚れている:\n{result.stdout}"


def test_two_builds_are_independent(tmp_path: Path) -> None:
    """2 回組み立てても互いに影響しないこと。"""
    first = build_fake_inbox(tmp_path / "one", with_noise=False)
    second = build_fake_inbox(tmp_path / "two", with_noise=False)
    recording = DEFAULT_RECORDINGS[0]
    target = first / DEVICE_ID / recording.folder / recording.filename(ORIG)
    target.unlink()
    assert not target.exists()
    assert (second / DEVICE_ID / recording.folder / recording.filename(ORIG)).is_file()
