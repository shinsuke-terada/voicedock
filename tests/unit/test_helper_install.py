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
REAPER = "voicedock-reaper"


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
        # **stdin を空のパイプにする。**確認入力を求める経路（--enable-deletion）が
        # 端末を掴んで固まらないようにする
        input="",
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


# --- 削除の有効化 / 無効化（§14.2 / #39）--------------------------------


def conf_of(sandbox: dict[str, Path]) -> dict[str, str]:
    body = (sandbox["home"] / "VoiceDock" / "helper.conf").read_text(encoding="utf-8")
    values: dict[str, str] = {}
    for line in body.splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values


@pytest.fixture
def installed(sandbox: dict[str, Path]) -> dict[str, Path]:
    """Helper を入れ、reaper の原本も置いた状態。"""
    shutil.copy(HELPER_DIR / REAPER, sandbox["repo"] / "helper" / REAPER)
    assert run(sandbox).returncode == 0
    return sandbox


def test_enable_deletion_releases_all_three_host_locks(installed: dict[str, Path]) -> None:
    """**ホスト側の 3 つを 1 度に解除する**（§14.2）。

    1 つずつ手で直すと**どれか 1 つ忘れた状態**に落ちやすく、ロック 1 だけの解除は
    **削除もされずセッションも進まない**（#145）。
    """
    result = run(installed, "--enable-deletion", "--yes")
    assert result.returncode == 0, result.stderr

    conf = conf_of(installed)
    assert conf["DELETE_SOURCE_AUDIO"] == "true"
    assert conf["MOUNT_MODE"] == "rw"
    assert (installed["home"] / "VoiceDock" / "bin" / REAPER).is_file()


def test_enable_deletion_says_the_container_side_is_left(installed: dict[str, Path]) -> None:
    """**コンテナ側は別の系統である**（§7.4）。黙って終わらない。"""
    result = run(installed, "--enable-deletion", "--yes")
    output = result.stdout + result.stderr
    assert "config/config.yaml" in output
    assert "V-30" in output, "片方だけの解除が止まることを伝えていない"


def test_enable_deletion_needs_confirmation_without_yes(installed: dict[str, Path]) -> None:
    """**`--yes` が無ければ確認入力を求める。**空の stdin では中止する。"""
    result = run(installed, "--enable-deletion")
    assert result.returncode != 0
    assert "中止しました" in result.stderr

    conf = conf_of(installed)
    assert conf["DELETE_SOURCE_AUDIO"] == "false", "中止したのに書き換えた"
    assert conf["MOUNT_MODE"] == "ro"
    assert not (installed["home"] / "VoiceDock" / "bin" / REAPER).exists()


def test_enable_deletion_refuses_without_the_reaper(sandbox: dict[str, Path]) -> None:
    """**黙って半分だけ解除しない。**reaper の原本が無ければ 1 つも変えない。"""
    assert run(sandbox).returncode == 0
    result = run(sandbox, "--enable-deletion", "--yes")

    assert result.returncode != 0
    assert "voicedock-reaper" in result.stderr
    conf = conf_of(sandbox)
    assert conf["DELETE_SOURCE_AUDIO"] == "false", "reaper が無いのにロック 1 を外した"
    assert conf["MOUNT_MODE"] == "ro"


def test_disable_deletion_puts_every_lock_back(installed: dict[str, Path]) -> None:
    """**戻せること。**戻せない変更は怖くて実行できない。"""
    assert run(installed, "--enable-deletion", "--yes").returncode == 0
    result = run(installed, "--disable-deletion")
    assert result.returncode == 0, result.stderr

    conf = conf_of(installed)
    assert conf["DELETE_SOURCE_AUDIO"] == "false"
    assert conf["MOUNT_MODE"] == "ro"
    assert not (installed["home"] / "VoiceDock" / "bin" / REAPER).exists()


def test_enable_deletion_keeps_the_other_settings(installed: dict[str, Path]) -> None:
    """**2 行だけ差し替える。**利用者が編集した他の値を壊さない。"""
    conf_path = installed["home"] / "VoiceDock" / "helper.conf"
    before = conf_path.read_text(encoding="utf-8")
    assert run(installed, "--enable-deletion", "--yes").returncode == 0
    after = conf_path.read_text(encoding="utf-8")

    changed = [
        (a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b
    ]
    assert len(changed) == 2, changed
    assert {b for _a, b in changed} == {"MOUNT_MODE=rw", "DELETE_SOURCE_AUDIO=true"}
