"""`make enable-deletion`（`scripts/enable-deletion.sh`）を固定する（SPEC §14.2 / §21.2）。

**三重ロックは 2 つの系統に分かれている**（§7.4）。ホスト側（`helper.conf` と reaper）と
コンテナ側（`config.yaml`）である。**1 つずつ手で直すと、どれか 1 つ忘れた状態に
落ちやすい** —— ロック 1 だけの解除は**削除もされずセッションも進まない**（#145）。

**このスクリプトは、product で最も危険なコマンドである。**確認入力を求めること、
中止したら 1 つも変えないこと、戻せることを、振る舞いで固定する。

`docker` は `PATH` の先頭に偽物を置いて差し替える。**本物を呼ばない。**
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "enable-deletion.sh"
HELPER_DIR = REPO_ROOT / "helper"

pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file() or not HELPER_DIR.is_dir(), reason="scripts/ か helper/ が無い"
)

HELPER_FILES = (
    "install.sh",
    "voicedock-ingest",
    "voicedock-ingest-launcher.c",
    "helper.example.conf",
    "voicedock-reaper",
    "com.voicedock.ingest.plist",
)


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    (repo / "helper").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "config").mkdir()
    for name in HELPER_FILES:
        shutil.copy(HELPER_DIR / name, repo / "helper" / name)
    (repo / "helper" / "install.sh").chmod(0o755)
    shutil.copy(SCRIPT, repo / "scripts" / "enable-deletion.sh")
    (repo / "scripts" / "enable-deletion.sh").chmod(0o755)
    shutil.copy(REPO_ROOT / "config" / "config.example.yaml", repo / "config" / "config.yaml")

    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("docker", "cc", "codesign", "launchctl"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    return {"repo": repo, "home": home, "bin": bin_dir}


def run(sandbox: dict[str, Path], *args: str, reply: str = "") -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{sandbox['bin']}:{environment['PATH']}",
            "HOME": str(sandbox["home"]),
            "VOICEDOCK_HOME": str(sandbox["home"] / "VoiceDock"),
        }
    )
    return subprocess.run(  # noqa: S603 - argv はリスト。shell=True を使わない
        ["/bin/bash", str(sandbox["repo"] / "scripts" / "enable-deletion.sh"), *args],
        capture_output=True,
        text=True,
        env=environment,
        input=reply,
        timeout=60,
        check=False,
    )


def install_helper(sandbox: dict[str, Path]) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{sandbox['bin']}:{environment['PATH']}",
            "HOME": str(sandbox["home"]),
            "VOICEDOCK_HOME": str(sandbox["home"] / "VoiceDock"),
        }
    )
    subprocess.run(  # noqa: S603
        ["/bin/bash", str(sandbox["repo"] / "helper" / "install.sh")],
        capture_output=True,
        text=True,
        env=environment,
        input="",
        timeout=60,
        check=True,
    )


def locks(sandbox: dict[str, Path]) -> dict[str, str]:
    conf = (sandbox["home"] / "VoiceDock" / "helper.conf").read_text(encoding="utf-8")
    values: dict[str, str] = {}
    for line in conf.splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    config = (sandbox["repo"] / "config" / "config.yaml").read_text(encoding="utf-8")
    container = "true" if "  delete_source_audio: true" in config else "false"
    return {
        "helper": values["DELETE_SOURCE_AUDIO"],
        "mount": values["MOUNT_MODE"],
        "container": container,
        "reaper": "yes"
        if (sandbox["home"] / "VoiceDock" / "bin" / "voicedock-reaper").is_file()
        else "no",
    }


# --- 有効化 -------------------------------------------------------------


def test_enable_releases_both_systems(sandbox: dict[str, Path]) -> None:
    """**ホスト側とコンテナ側を 1 度に動かす。**半端な状態を作らせない。"""
    install_helper(sandbox)
    result = run(sandbox, reply="ENABLE\n")
    assert result.returncode == 0, result.stderr

    assert locks(sandbox) == {
        "helper": "true",
        "mount": "rw",
        "container": "true",
        "reaper": "yes",
    }


def test_a_wrong_reply_changes_nothing(sandbox: dict[str, Path]) -> None:
    """**中止したら 1 つも変えない。**`y` では通らない（打ち間違いで有効化させない）。"""
    install_helper(sandbox)
    before = locks(sandbox)

    result = run(sandbox, reply="y\n")

    assert result.returncode != 0
    assert "中止しました" in result.stderr
    assert locks(sandbox) == before


def test_the_warning_says_what_cannot_be_undone(sandbox: dict[str, Path]) -> None:
    """**何が起きるかを先に出す。**確認を求めるだけでは足りない。"""
    install_helper(sandbox)
    output = run(sandbox, reply="no\n").stdout

    assert "戻せません" in output, "取り返しがつかないことを言っていない"
    assert "E2E" in output and "#125" in output, "先に確かめることが書かれていない"
    assert "make disable-deletion" in output, "戻し方が書かれていない"


def test_enable_refuses_without_a_container_config(sandbox: dict[str, Path]) -> None:
    """`config/config.yaml` が無ければ**何もしない**。"""
    install_helper(sandbox)
    (sandbox["repo"] / "config" / "config.yaml").unlink()

    result = run(sandbox, reply="ENABLE\n")

    assert result.returncode != 0
    assert "config/config.yaml" in result.stderr
    conf = (sandbox["home"] / "VoiceDock" / "helper.conf").read_text(encoding="utf-8")
    assert "DELETE_SOURCE_AUDIO=false" in conf, "コンテナ側が無いのにホスト側を解除した"
    assert "MOUNT_MODE=ro" in conf


# --- 無効化 -------------------------------------------------------------


def test_disable_puts_everything_back(sandbox: dict[str, Path]) -> None:
    """**戻せること。**戻せない変更は怖くて実行できない。"""
    install_helper(sandbox)
    assert run(sandbox, reply="ENABLE\n").returncode == 0

    result = run(sandbox, "--disable")

    assert result.returncode == 0, result.stderr
    assert locks(sandbox) == {
        "helper": "false",
        "mount": "ro",
        "container": "false",
        "reaper": "no",
    }


def test_disable_does_not_ask(sandbox: dict[str, Path]) -> None:
    """**安全側へ戻す操作は確認しない。**止めたいときに止められないと困る。"""
    install_helper(sandbox)
    result = run(sandbox, "--disable")
    assert result.returncode == 0
    assert "ENABLE と入力" not in (result.stdout + result.stderr)
