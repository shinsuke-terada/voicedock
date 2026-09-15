"""Vault が**本物**かどうかの判定を固定する（SPEC §13.6 手順 0）。

**「書ける」を「Vault である」と読んではならない。**Docker は bind mount の source が
無ければ**空ディレクトリとして作る**ので、Vault を退避してもコンテナからは書ける場所に
見え続ける。2026-09-15 の実機では、その空ディレクトリへ Daily ノートを書いて
`obsidian_saved` を報告していた（#134）。

同じ姿になる原因は運用で普通に起きる —— 外付けディスクが未マウント、iCloud / Dropbox が
未同期、`OBSIDIAN_VAULT` の綴り間違い、Docker Desktop のファイル共有から外れている。
"""

from __future__ import annotations

from pathlib import Path

from voicedock import notes

MARKER = ".obsidian"


def test_a_vault_with_the_marker_is_available(tmp_path: Path) -> None:
    (tmp_path / MARKER).mkdir()
    assert notes.vault_is_available(tmp_path, MARKER)


def test_an_empty_directory_is_not_a_vault(tmp_path: Path) -> None:
    """**これが #134 の本体である。**Docker が作った空ディレクトリを弾く。"""
    assert not notes.vault_is_available(tmp_path, MARKER)


def test_a_missing_root_is_not_a_vault(tmp_path: Path) -> None:
    assert not notes.vault_is_available(tmp_path / "absent", MARKER)


def test_a_file_named_like_the_marker_is_not_enough(tmp_path: Path) -> None:
    """**`exists()` では足りない。**目印は**ディレクトリ**でなければならない。"""
    (tmp_path / MARKER).write_text("", encoding="utf-8")
    assert not notes.vault_is_available(tmp_path, MARKER)


def test_a_file_as_root_is_not_a_vault(tmp_path: Path) -> None:
    target = tmp_path / "vault"
    target.write_text("", encoding="utf-8")
    assert not notes.vault_is_available(target, MARKER)


def test_an_empty_marker_disables_the_check(tmp_path: Path) -> None:
    """**逃げ道を必ず用意する。**Obsidian は Vault ごとに設定フォルダ名を変更できる。

    誤検知でパイプラインが止まる方が、検査が緩いことより害が大きい。
    """
    assert notes.vault_is_available(tmp_path, "")


def test_a_blank_marker_disables_the_check(tmp_path: Path) -> None:
    """**空文字だけを見ていると、分岐そのものがテストされない。**

    `Path(root) / "" == root` なので、空文字は分岐が無くても通ってしまう。
    `" "` は通らない（半角空白 1 文字のディレクトリを探しにいく）ので、
    **無効化の分岐が働いていることをここで初めて確かめられる。**
    """
    assert notes.vault_is_available(tmp_path, " ")
    assert notes.vault_is_available(tmp_path, "\t")


def test_an_empty_marker_still_requires_the_root(tmp_path: Path) -> None:
    """**無効化しても「ディレクトリが在る」ことまでは捨てない。**"""
    assert not notes.vault_is_available(tmp_path / "absent", "")


def test_a_custom_marker_is_honoured(tmp_path: Path) -> None:
    (tmp_path / ".myvault").mkdir()
    assert notes.vault_is_available(tmp_path, ".myvault")
    assert not notes.vault_is_available(tmp_path, MARKER)
