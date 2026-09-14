"""`helper/install.sh` がラッパを作り、plist をそれ経由にすることを固定する（#95）。

**launchd がシェルスクリプトを直接起動すると、macOS の TCC でデバイスを読めない。**
2026-09-14 に実機で A/B を取った（§3.4(7)）。ad-hoc 署名した小さなラッパを挟むと通る。

**`cc` が無い環境ではラッパを作れない。**そのときはインストールを失敗させず、
警告してスクリプト直接で配置する（Helper が入らないより、TCC 未解決でも入っている
ほうがまし）。**ただし黙って通さない** — `doctor` の DH-16 が恒久的に検査する。

`cc` / `codesign` / `launchctl` は `PATH` の先頭へ偽物を置いて差し替える。
**本物を呼ばない** — CI には macOS の道具が無く、開発機の LaunchAgent も触らせない。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT

HELPER_DIR = REPO_ROOT / "helper"
INSTALL = HELPER_DIR / "install.sh"

pytestmark = pytest.mark.skipif(not INSTALL.is_file(), reason="helper/ がマウントされていない")

NEEDED = ("install.sh", "voicedock-ingest", "voicedock-ingest-launcher.c", "helper.example.conf")


def write_stub(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path]:
    """`install.sh` を走らせられる最小の repo と HOME。"""
    repo = tmp_path / "repo"
    (repo / "helper").mkdir(parents=True)
    for name in (*NEEDED, "com.voicedock.ingest.plist"):
        shutil.copy(HELPER_DIR / name, repo / "helper" / name)
    (repo / "helper" / "install.sh").chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()

    bin_dir = tmp_path / "bin"
    # `cc` は「空でない実行ファイルを作る」だけ。**本物のコンパイラは要らない**
    compiler = "\n".join(
        (
            "while [ $# -gt 0 ]; do",
            '  if [ "$1" = "-o" ]; then',
            "    shift",
            '    printf "#!/bin/sh\\nexec \\"\\$@\\"\\n" > "$1"',
            '    chmod 755 "$1"',
            "  fi",
            "  shift",
            "done",
            "exit 0",
        )
    )
    write_stub(bin_dir, "cc", compiler)
    write_stub(bin_dir, "codesign", "exit 0")
    write_stub(bin_dir, "launchctl", "exit 0")
    return {"repo": repo, "home": home, "bin": bin_dir, "tmp": tmp_path}


def run(sandbox: dict[str, Path], *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{sandbox['bin']}:{environment['PATH']}",
            "HOME": str(sandbox["home"]),
            "VOICEDOCK_HOME": str(sandbox["home"] / "VoiceDock"),
        }
    )
    environment.update(env)
    return subprocess.run(  # noqa: S603 - argv はリスト。shell=True を使わない
        ["/bin/bash", str(sandbox["repo"] / "helper" / "install.sh"), *args],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        check=False,
    )


def program_arguments(sandbox: dict[str, Path]) -> list[str]:
    plist = sandbox["home"] / "Library" / "LaunchAgents" / "com.voicedock.ingest.plist"
    body = plist.read_text(encoding="utf-8")
    block = body.split("<key>ProgramArguments</key>", 1)[1].split("</array>", 1)[0]
    return re.findall(r"<string>(.+?)</string>", block)


# --- ラッパが作られること -------------------------------------------------


def test_the_launcher_is_built_and_signed(sandbox: dict[str, Path]) -> None:
    """`cc` があればラッパを作り、**ad-hoc 署名する。**"""
    result = run(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = sandbox["home"] / "VoiceDock" / "bin" / "voicedock-ingest-launcher"
    assert launcher.is_file() and os.access(launcher, os.X_OK), result.stdout


def test_the_plist_goes_through_the_launcher(sandbox: dict[str, Path]) -> None:
    """**plist の 1 番目がラッパ、2 番目がスクリプト。**

    これが逆だったり片方だけだと、launchd が起動に失敗するか TCC で読めない。
    """
    run(sandbox)
    args = program_arguments(sandbox)
    assert len(args) == 2, args
    assert args[0].endswith("voicedock-ingest-launcher"), args
    assert args[1].endswith("voicedock-ingest"), args


def test_codesign_is_actually_called(sandbox: dict[str, Path]) -> None:
    """**署名しなければ TCC の許可が届かない。**呼んだことを記録して確かめる。"""
    marker = sandbox["tmp"] / "codesign-called"
    write_stub(sandbox["bin"], "codesign", f'printf "%s\\n" "$*" >> "{marker}"\nexit 0')
    run(sandbox)
    assert marker.is_file(), "codesign が呼ばれていない"
    assert "--force" in marker.read_text(encoding="utf-8")


# --- `cc` が無いとき（退避） ---------------------------------------------


NO_COMPILER = {"VOICEDOCK_CC": "definitely-not-a-compiler"}
"""**`PATH` から `cc` を消す形では作れない。**CI のランナーには本物の `cc` が在り、
偽物を消すと `/usr/bin/cc` が見つかってしまう（`shutil.which` で同じ誤りをして CI で落ちた）。
`install.sh` の差し替え口を使う。
"""


def test_install_succeeds_without_a_compiler(sandbox: dict[str, Path]) -> None:
    """**陰性対照。**`cc` が無くてもインストールは成功する。

    Helper が入らないより、TCC 未解決でも入っているほうがましである。
    """
    result = run(sandbox, **NO_COMPILER)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_missing_compiler_is_warned_about_loudly(sandbox: dict[str, Path]) -> None:
    """**黙って通さない。**何をすれば直るかまで書く。"""
    result = run(sandbox, **NO_COMPILER)
    combined = result.stdout + result.stderr
    assert "cc が見つかりません" in combined, combined
    assert "xcode-select --install" in combined, "直し方を示す"
    assert "TCC" in combined, "何が起きるかを示す"


def test_the_plist_falls_back_to_the_script(sandbox: dict[str, Path]) -> None:
    """ラッパが無いのに plist が指すと、**launchd が起動に失敗して何も動かない。**

    「取り込めない」より悪い状態なので、退避時はスクリプト直接にする。
    """
    run(sandbox, **NO_COMPILER)
    args = program_arguments(sandbox)
    assert len(args) == 1, args
    assert args[0].endswith("voicedock-ingest"), args


def test_a_failed_signature_leaves_no_launcher(sandbox: dict[str, Path]) -> None:
    """**署名に失敗したラッパを残さない。**残すと plist がそれを指し、TCC で読めない。"""
    write_stub(sandbox["bin"], "codesign", "exit 1")
    result = run(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = sandbox["home"] / "VoiceDock" / "bin" / "voicedock-ingest-launcher"
    assert not launcher.exists(), "署名できなかったラッパが残っている"
    assert len(program_arguments(sandbox)) == 1


# --- plist が壊れていないこと（#95 で実際に壊した） ----------------------


def test_a_broken_plist_aborts_the_install(sandbox: dict[str, Path]) -> None:
    """**空の plist を bootstrap すると LaunchAgent ごと死ぬ。**

    `awk -v` に改行を含む値を渡そうとして実際に空にした（#95）。生成後に検める。
    """
    template = sandbox["repo"] / "helper" / "com.voicedock.ingest.plist"
    template.write_text("", encoding="utf-8")
    result = run(sandbox)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "plist" in result.stderr


# --- ソースの形（静的） ---------------------------------------------------


def test_the_launcher_spawns_instead_of_exec(sandbox: dict[str, Path]) -> None:
    """**`execv` で自分自身を置き換えてはならない。**

    TCC の responsible process を自分のまま保つため `posix_spawn` で子として起動する。
    **実機で通ったのはこの形である**（§3.4(7)）。
    """
    source = (HELPER_DIR / "voicedock-ingest-launcher.c").read_text(encoding="utf-8")
    assert "posix_spawn" in source
    assert not re.search(r"\bexecv?[lp]?\s*\(", source), "execv を使っている"
    assert "waitpid" in source, "子の終了を待たないと launchd が先に終わったと見なす"
