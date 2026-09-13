"""`voicedock.notes.atomic_write` が SPEC §13.6 の 7 手順に従うことを固定する。

**再生成の失敗で既存ノートを壊さないことが §1.3 の優先順位 1（記録の保護）に直結する。**
1 日 32 回書き直される Raw ノート（§10.7）で、32 回に 1 回でも壊れれば記録が失われる。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from voicedock import notes, paths
from voicedock.notes import atomic_write, sha256_bytes
from voicedock.paths import VaultPath

BODY = "# 2026-08-29\n\nおはようございます。\n".encode()


@pytest.fixture(autouse=True)
def _vault_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`tmp_path` を Vault のマウント点として扱う。

    `paths.safe_unlink_tmp()` は `/obsidian` / `/data` の配下しか消さない（§11.2）。
    **本番のコードには上書きの入口を作らない**方針なので、差し替えはテスト側で行う
    （`test_paths.py` の `_patch_roots` と同じ）。
    """
    monkeypatch.setattr(paths, "VAULT_ROOT", tmp_path)


def target(tmp_path: Path, name: str = "note.md") -> VaultPath:
    return VaultPath(tmp_path / name)


def tmp_name(path: Path) -> Path:
    return path.with_name(f"{paths.TMP_PREFIX}{path.name}{paths.TMP_SUFFIX}")


# --- 正常系 --------------------------------------------------------------


def test_writes_the_content_and_returns_its_sha(tmp_path: Path) -> None:
    path = target(tmp_path)
    returned = atomic_write(path, BODY)
    assert Path(path).read_bytes() == BODY
    assert returned == hashlib.sha256(BODY).hexdigest()


def test_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = target(tmp_path)
    atomic_write(path, BODY)
    assert not tmp_name(Path(path)).exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["note.md"]


def test_overwrites_an_existing_note(tmp_path: Path) -> None:
    """再生成で上書きできること（Raw ノートは 1 日 32 回書き直される。§10.7）。"""
    path = target(tmp_path)
    Path(path).write_bytes(b"old")
    atomic_write(path, BODY)
    assert Path(path).read_bytes() == BODY


def test_temporary_file_is_in_the_same_directory(tmp_path: Path) -> None:
    """**一時ファイルは必ず最終ファイルと同じディレクトリに作る**（§13.6）。

    別のファイルシステムに作ると `os.replace` が atomic でなくなる
    （`EXDEV` で失敗するか、コピー + 削除に退化する）。
    """
    folder = tmp_path / "Daily" / "Voice" / "Raw"
    folder.mkdir(parents=True)
    path = VaultPath(folder / "note.md")

    # 書き込みの途中（replace の直前）に一時ファイルがどこに在るかを捕まえる
    seen: list[Path] = []

    def capture(self: Path, _dest: object) -> None:
        seen.append(self)
        raise OSError("stop here")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "replace", capture)
        with pytest.raises(OSError, match="stop here"):
            atomic_write(path, BODY)

    assert seen, "replace が呼ばれていない"
    assert seen[0].parent == folder, "一時ファイルが別のディレクトリに在る"
    assert seen[0].name.startswith(paths.TMP_PREFIX)
    assert seen[0].name.endswith(paths.TMP_SUFFIX)


def test_temporary_file_name_starts_with_a_dot(tmp_path: Path) -> None:
    """`.` 始まりにするのは **Obsidian が検証途中のファイルを拾わないようにするため**（§13.6）。"""
    path = target(tmp_path)
    assert tmp_name(Path(path)).name == ".note.md.tmp"


# --- 異常系: 最終ファイルを壊さない -------------------------------------


def test_a_write_failure_leaves_the_existing_note_intact(tmp_path: Path) -> None:
    """**途中で失敗したら最終ファイルを差し替えない**（§13.6）。

    再生成の失敗で既存ノートを壊さないことが §1.3 の優先順位 1 に直結する。
    """
    folder = tmp_path / "readonly"
    folder.mkdir()
    locked = VaultPath(folder / "note.md")
    Path(locked).write_bytes(b"existing")
    folder.chmod(0o500)
    try:
        with pytest.raises(OSError):
            atomic_write(locked, BODY)
    finally:
        folder.chmod(0o755)
    assert Path(locked).read_bytes() == b"existing"


def test_a_sha_mismatch_does_not_replace_the_final_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**読み直しの SHA-256 が一致しなければ `os.replace` を実行しない**（§13.6 手順 4 → 5）。

    ここを飛ばすと**「書けたつもり」のノートが §14.1 の削除根拠になる。**
    """
    path = target(tmp_path)
    Path(path).write_bytes(b"existing")

    calls: list[str] = []
    real_sha = notes.sha256_bytes

    def wrong(content: bytes) -> str:
        calls.append("sha")
        return "0" * 64 if len(calls) > 1 else real_sha(content)

    monkeypatch.setattr(notes, "sha256_bytes", wrong)
    with pytest.raises(ValueError, match="SHA-256"):
        atomic_write(path, BODY)

    assert Path(path).read_bytes() == b"existing", "最終ファイルが差し替わっている"
    assert not tmp_name(Path(path)).exists(), "一時ファイルが残っている"


def test_a_failure_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = target(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write(path, BODY)
    assert not tmp_name(Path(path)).exists()
    assert not Path(path).exists()


def test_a_keyboard_interrupt_still_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`BaseException` でも一時ファイルを消すこと。

    `Exception` だけを捕まえると **SIGINT / SIGTERM で `.note.md.tmp` が残り、
    Obsidian の同期に乗る。**
    """
    path = target(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        atomic_write(path, BODY)
    assert not tmp_name(Path(path)).exists()


# --- 親ディレクトリの fsync ---------------------------------------------


def test_a_directory_fsync_failure_is_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**親ディレクトリの fsync が失敗しても致命的としない**（§13.6 手順 6）。

    ファイルシステムによってはディレクトリの `fsync` が通らない。ここで落とすと
    **実際には書けているのに失敗扱いになる。**
    """
    path = target(tmp_path)
    real_fsync = os.fsync
    seen: list[int] = []

    def flaky(fd: int) -> None:
        seen.append(fd)
        if len(seen) > 1:  # 1 回目はファイル、2 回目がディレクトリ
            raise OSError("fsync not supported")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky)
    atomic_write(path, BODY)  # 例外を投げない
    assert Path(path).read_bytes() == BODY


def test_a_directory_open_failure_is_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = target(tmp_path)
    real_open = os.open

    def flaky(target_path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if flags == os.O_RDONLY:
            raise OSError("cannot open directory")
        return real_open(target_path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", flaky)
    atomic_write(path, BODY)
    assert Path(path).read_bytes() == BODY


# --- 削除経路（§14.4 N-10） ---------------------------------------------


def test_cleanup_goes_through_safe_unlink_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一時ファイルの削除が **`paths.safe_unlink_tmp()` 経由**であること（N-10）。

    `notes.py` に直接 `unlink` を書くと `test_no_device_delete.py` が落ちる。
    ここでは「実際にその関数を通っている」ことを見る。
    """
    path = target(tmp_path)
    called: list[Path] = []
    real = paths.safe_unlink_tmp

    def spy(p: Path, **kwargs: object) -> None:
        called.append(Path(p))
        real(p, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(paths, "safe_unlink_tmp", spy)
    monkeypatch.setattr(Path, "replace", _boom)
    with pytest.raises(OSError):
        atomic_write(path, BODY)
    assert called == [tmp_name(Path(path))]


def _boom(*_args: object, **_kwargs: object) -> None:
    raise OSError("replace failed")


# --- 補助 ----------------------------------------------------------------


def test_sha256_bytes_matches_hashlib() -> None:
    assert sha256_bytes(BODY) == hashlib.sha256(BODY).hexdigest()


def test_empty_content_is_written(tmp_path: Path) -> None:
    """空の内容でも書けること（**検証は R-2 / W-2 が落とす**。書き込み自体は成功する）。"""
    path = target(tmp_path)
    atomic_write(path, b"")
    assert Path(path).read_bytes() == b""
