"""削除経路が 1 本に保たれていることを静的に検査する（SPEC §14.4 N-10 / N-14, §20.1）。

**`src/voicedock/` の全モジュールを AST で走査する。**モジュールが増えても自動的に
対象になるため、新しいチケットが `Path.unlink()` を書いた時点で落ちる。

v4.0 以降、**コンテナのどのモジュールもデバイス上のファイルを削除できない。**
削除を実行できるのはホストの `voicedock-reaper` だけである（N-16）。
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

import pytest

from voicedock.paths import DevicePath, InboxPath, StagingPath, VaultPath

SRC_DIR = Path(__file__).parents[2] / "src" / "voicedock"
ALLOWED_MODULE = "paths.py"

DELETE_NAMES = frozenset({"unlink", "remove", "rmdir", "removedirs", "rmtree"})
"""削除を行う呼び出しの名前。属性呼び出しと、import した名前の直接呼び出しの両方を見る。"""


def modules() -> list[Path]:
    found = sorted(SRC_DIR.glob("*.py"))
    assert found, f"モジュールが見つかりません: {SRC_DIR}"
    return found


def parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def delete_calls(tree: ast.AST) -> list[str]:
    """削除呼び出しの一覧を `名前:行` で返す。"""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in DELETE_NAMES:
            found.append(f"{func.attr}:{node.lineno}")
        elif isinstance(func, ast.Name) and func.id in DELETE_NAMES:
            found.append(f"{func.id}:{node.lineno}")
    return found


def test_only_paths_py_deletes() -> None:
    """削除呼び出しが `paths.py` 以外に 1 つも無いこと（§14.4 N-10）。"""
    offenders = {
        module.name: calls
        for module in modules()
        if module.name != ALLOWED_MODULE and (calls := delete_calls(parsed(module)))
    }
    assert offenders == {}, (
        f"削除呼び出しは paths.py の safe_unlink_* に限定する（N-10）: {offenders}"
    )


def test_paths_py_does_delete() -> None:
    """逆に `paths.py` には削除があること（検査が空振りしていないことの確認）。"""
    assert delete_calls(parsed(SRC_DIR / ALLOWED_MODULE))


def test_no_delete_names_are_imported_outside_paths() -> None:
    """`from os import remove` のような名前の持ち込みも無いこと。

    属性呼び出しだけを見ていると `remove(p)` の形で迂回できてしまう。
    """
    offenders: dict[str, list[str]] = {}
    for module in modules():
        if module.name == ALLOWED_MODULE:
            continue
        names = [
            alias.asname or alias.name
            for node in ast.walk(parsed(module))
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name in DELETE_NAMES
        ]
        if names:
            offenders[module.name] = names
    assert offenders == {}, f"削除関数を import してはならない（N-10）: {offenders}"


def test_no_function_takes_a_device_path_and_deletes() -> None:
    """`DevicePath` を受け取る関数が削除を行わないこと。

    issue #7 が求める「枠」である。`device.py`（#14）や `cleaner.py`（#36）が
    増えたときに効きはじめる。現時点では `test_only_paths_py_deletes` が
    より強い条件で守っている。
    """
    offenders: dict[str, list[str]] = {}
    for module in modules():
        tree = parsed(module)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            annotations = [
                ast.unparse(arg.annotation)
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                if arg.annotation is not None
            ]
            if not any("DevicePath" in text for text in annotations):
                continue
            if calls := delete_calls(node):
                offenders[f"{module.name}::{node.name}"] = calls
    assert offenders == {}, f"DevicePath を受け取る関数は削除してはならない（N-10）: {offenders}"


def test_py_typed_marker_exists() -> None:
    """`py.typed` があること（PEP 561）。

    **これが無いと、パッケージを import した外部コードに対して mypy が
    「型スタブが無い」として解析を打ち切る。**§11.2 の `NewType` による保証は
    型検査器が回ることを前提にしており（§17.4）、マーカーの欠落は保証を黙って無効化する。
    """
    assert (SRC_DIR / "py.typed").is_file()


@pytest.mark.parametrize("method", ["unlink", "open", "stat", "lstat", "exists", "is_symlink"])
def test_purepath_has_no_io(method: str) -> None:
    """`PurePosixPath` が I/O を持たないこと。

    **これが「コンテナはデバイスを触れない」の根拠である**（§11.2, v4.0 の E-7）。
    将来 Python 側が `PurePath` に I/O を足したら、この保証は消える。そのときに気づく。
    """
    assert not hasattr(PurePosixPath, method)


def _supertype(newtype: object) -> object:
    """`NewType` の基底型。mypy は `__supertype__` を知らないので 1 箇所に閉じる。"""
    return newtype.__supertype__  # type: ignore[attr-defined]


def test_device_path_is_a_pure_posix_path() -> None:
    """`DevicePath` の基底が `PurePosixPath` であること（§11.2）。

    `Path` に戻すと `open()` / `unlink()` が生えて、型による保証が消える。
    """
    assert _supertype(DevicePath) is PurePosixPath


@pytest.mark.parametrize("newtype", [InboxPath, StagingPath, VaultPath])
def test_container_paths_are_path(newtype: object) -> None:
    assert _supertype(newtype) is Path
