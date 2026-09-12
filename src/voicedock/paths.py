"""パス型の分離と封じ込め（SPEC §11.2, §14.1.1, §14.4 N-10 / N-14）。

**このモジュールは `voicedock` の他モジュールを import しない。**封じ込めの違反は
プログラムの誤りであり、`ErrorCode` を持つ運用エラーではない（対応するコードも存在しない）。
工程側が `LOCAL_DELETE_FAILED` などへ写す。

**削除できる関数はこのモジュールの 3 本だけである**（§14.4 N-10）。
`tests/unit/test_no_device_delete.py` が「`paths.py` 以外に削除呼び出しが無い」ことを
AST で検査しており、他のモジュールが `Path.unlink()` を書いた時点で落ちる。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath
from typing import Final, NewType

DevicePath = NewType("DevicePath", PurePosixPath)
"""デバイス上のボリュームルートからの相対パス。

**`PurePosixPath` なので `open()` も `stat()` も `unlink()` も持たない。**
「コンテナがデバイス上のパスを開く / 消す」コードは書こうとしても書けない（§11.2, v4.0）。
削除を実行できるのはホストの `voicedock-reaper` だけである（§14.4 N-16）。
"""

InboxPath = NewType("InboxPath", Path)
"""`/inbox` 配下。Helper が置いた原本。読み、変換後に消す（§10.5 の `inbox_retain`）。"""

StagingPath = NewType("StagingPath", Path)
"""`/data` 配下。変換後の 16 kHz 音声や transcript JSON。"""

VaultPath = NewType("VaultPath", Path)
"""`/obsidian` 配下。**削除しない**（`os.replace()` で上書きするだけ）。"""

# コンテナのマウント点（§18.2 の compose.yaml と一致させる）。
# 設定ではない。config.yaml から取らない — 設定で緩められる安全柵は柵ではない。
# config の import.staging_root などは「このマウント点の配下にある設定値」である。
INBOX_ROOT: Final = Path("/inbox")
DATA_ROOT: Final = Path("/data")
VAULT_ROOT: Final = Path("/obsidian")
QUEUE_ROOT: Final = Path("/queue")

# 一時ファイルの命名（§13.6）。`.` 始まりは検証途中のファイルを Obsidian に拾わせないため
TMP_PREFIX: Final = "."
TMP_SUFFIX: Final = ".tmp"

_UNSAFE_PARTS: Final = frozenset({"", ".", ".."})


# --- 封じ込め ------------------------------------------------------------


def is_under(root: Path, target: Path) -> bool:
    """`realpath` 解決後に `target` が `root` の**真の**配下かを返す（§14.1.1 検証 5）。

    **字句判定（`root in target.parents`）では足りない。**`Path.parents` は symlink を
    解決しない。macOS には `/Volumes/Macintosh HD -> /` が実在し、コンテナ内でも symlink の
    まま見えて無限に再帰できることを実機で確認した（`docs/POC.md` §5.1、§22 R-19）。

    `root` 自身は配下ではない（`root` そのものを消させない）。
    **`target` が存在しなくてもよい。**削除の「前」に呼ばれるため、存在を要求してはならない。

    §14.1.1 の規範実装が併せて課す `resolved == target`（経路上に symlink が 1 つも無い）は
    **課さない。**それはデバイス上の判定（reaper）の規則であり、コンテナへ持ち込むと
    Vault 内に symlink を張っている利用者の一時ファイルを片付けられなくなる（§11.2）。
    """
    if not root.is_absolute() or not target.is_absolute():
        return False
    if ".." in root.parts or ".." in target.parts:
        return False
    resolved_root = Path(os.path.realpath(root))
    resolved = Path(os.path.realpath(target))
    return resolved != resolved_root and resolved_root in resolved.parents


def require_under(root: Path, target: Path) -> None:
    """`is_under()` が偽なら `ValueError`。削除の直前に必ず通す（§14.4 N-14）。"""
    if not is_under(root, target):
        raise ValueError(f"{root} の配下ではありません: {target}")


def is_safe_relpath(rel: PurePosixPath) -> bool:
    """デバイス上の relpath として健全かを返す（§14.1.1 検証 4・9 / §14.4 N-17）。

    削除要求へ載せる前にこれを通す。**絶対パスや `..` を要求へ書くこと自体が禁止**である。
    `.` で始まる要素を拒むのは `.Trashes` / `.Spotlight-V100` / `.fseventsd` を削除対象に
    しないためである（§22 R-22）。
    """
    if rel.is_absolute():
        return False
    parts = rel.parts
    if not parts:
        return False
    for part in parts:
        if part in _UNSAFE_PARTS or part.startswith("."):
            return False
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in part):
            return False
    return True


# --- 削除（§14.4 N-10 が許す 3 本） --------------------------------------


def safe_unlink_inbox(path: InboxPath, *, missing_ok: bool = False) -> None:
    """`/inbox` 配下の原本を削除する（§10.5 の `inbox_retain`）。

    **デバイス上の削除とは別物である。**inbox は Helper がコピーした複製であり、
    その削除はコンテナの責務である。
    """
    _unlink(path, (INBOX_ROOT,), missing_ok=missing_ok)


def safe_unlink_staging(path: StagingPath, *, missing_ok: bool = False) -> None:
    """`/data` 配下の中間ファイルを削除する。"""
    _unlink(path, (DATA_ROOT,), missing_ok=missing_ok)


def safe_unlink_tmp(path: Path, *, missing_ok: bool = True) -> None:
    """一時ファイル（`.<name>.tmp`）を削除する（§13.6）。

    一時ファイルは**必ず最終ファイルと同じディレクトリ**に作るため、Vault 配下にも
    `/data` 配下にも現れる。`missing_ok` の既定が真なのは、失敗経路の後片付けで
    呼ばれるためである（§9.3 の `RAW_WRITING -> TRANSCRIBED`）。
    """
    target = _as_path(path)
    name = target.name
    # 長さも見る。'.tmp' は前後の条件を満たすが、'.' と '.tmp' が重なっているだけで
    # 実体の名前を持たない。一時ファイルは必ず `.<最終ファイル名>.tmp` の形である（§13.6）
    if not (
        name.startswith(TMP_PREFIX)
        and name.endswith(TMP_SUFFIX)
        and len(name) > len(TMP_PREFIX) + len(TMP_SUFFIX)
    ):
        raise ValueError(
            f"一時ファイルではありません（'{TMP_PREFIX}<最終ファイル名>{TMP_SUFFIX}' の"
            f"形だけを削除できる）: {target}"
        )
    _unlink(target, (VAULT_ROOT, DATA_ROOT), missing_ok=missing_ok)


def _unlink(target: object, roots: tuple[Path, ...], *, missing_ok: bool) -> None:
    """封じ込めを確認してから 1 ファイルだけ削除する。

    検査の順序を固定する（**先に落ちるものを先に見る**）。
    ディレクトリの再帰削除は行わない（§14.4 N-16 と同じ方針）。
    """
    path = _as_path(target)

    if not any(is_under(root, path) for root in roots):
        allowed = " / ".join(str(root) for root in roots)
        raise ValueError(f"{allowed} の配下ではありません: {path}")

    if not path.exists() and not path.is_symlink():
        if missing_ok:
            return
        raise FileNotFoundError(path)

    # symlink そのものは消さない。unlink はリンクだけを外すので無害だが、
    # 「消そうとしているものが想定と違う」ことの合図なので安全側へ倒す（§1.3）
    if path.is_symlink():
        raise ValueError(f"symlink は削除しません: {path}")

    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"通常ファイルではありません: {path}")

    path.unlink()


def _as_path(target: object) -> Path:
    """`Path` であることを実行時にも確かめる。

    **`NewType` は実行時に消える。**型検査を通していない経路（`Any` 経由・動的呼び出し）
    からでもデバイス上のパスを渡せないようにするための二重の柵である。

    判定は「`Path` を受理する」形で書くこと。`isinstance(Path("a"), PurePosixPath)` は
    **真**なので、「`PurePosixPath` を拒否する」形では `Path` も落ちる。
    """
    if isinstance(target, Path):
        return target
    raise TypeError(
        f"削除できるのは Path だけです: {type(target).__name__}。"
        "DevicePath は PurePosixPath であり、コンテナはデバイスへ到達できない（§14.4 N-10）"
    )
