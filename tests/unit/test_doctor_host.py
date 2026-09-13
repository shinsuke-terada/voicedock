"""`scripts/doctor.sh` が §19.2 の DH 表 5 件を実施することを固定する。

**コンテナ内からは確認できない前提だけを見る。**とくに DH-1（Apple Virtualization
Framework）は致命的で、Docker VMM では `/Volumes` 配下の bind mount が失敗する。
**ローカルディスクの bind mount は動く**ため、Obsidian Vault は読み書きできるのに
DJI Mic だけ見えないという切り分けの難しい症状になる（docker/for-mac#7480）。

`docker` / `launchctl` は `PATH` の先頭へ偽物を置いて差し替える。**本物を呼ばない** —
CI にも開発機にも「起動中の VoiceDock」は無い。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT

DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"
INGEST = REPO_ROOT / "helper" / "voicedock-ingest"

pytestmark = pytest.mark.skipif(
    not DOCTOR.is_file() or not INGEST.is_file(),
    reason="scripts/ または helper/ がマウントされていない",
)

EXIT_DOCTOR_FATAL = 4

COMPOSE_CONFIG = """\
services:
  voicedock:
    image: voicedock:latest
    volumes:
      - type: bind
        source: {vault}
        target: /obsidian
      - type: bind
        source: {inbox}
        target: /inbox
"""


def write_stub(directory: Path, name: str, body: str) -> Path:
    """`PATH` に置く偽コマンド。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def host(tmp_path: Path) -> dict[str, Path]:
    """DH-1〜DH-15 が全部通る状態のホストを組み立てる。"""
    vault = tmp_path / "Obsidian Vault"  # **空白を含む**（§19.2 の引用の確認）
    vault.mkdir()
    home = tmp_path / "VoiceDock"
    (home / "bin").mkdir(parents=True)
    (home / "inbox").mkdir()

    settings = tmp_path / "settings"
    settings.mkdir()
    (settings / "settings-store.json").write_text(
        '{\n  "UseVirtualizationFramework": true,\n  "AutoStart": false\n}\n', encoding="utf-8"
    )

    ingest = home / "bin" / "voicedock-ingest"
    ingest.write_bytes(INGEST.read_bytes())
    ingest.chmod(0o755)
    volumes = tmp_path / "volumes"
    volumes.mkdir()
    (home / "helper.conf").write_text(
        f"VOLUMES_ROOT={volumes}\n"
        "INCLUDE_VOLUMES=()\n"
        'EXCLUDE_VOLUMES=("Macintosh HD" ".*")\n'
        "MOUNT_MODE=ro\n"
        "DELETE_SOURCE_AUDIO=false\n"
        f'VOICEDOCK_HOME="{home}"\n'
        "STABILITY_FAST_PATH_SECONDS=60\n"
        "STABILITY_INTERVAL_SECONDS=3\n"
        "STABILITY_CHECKS=2\n"
        "MAX_SCAN_DEPTH=3\n",
        encoding="utf-8",
    )
    for name in ("state", "log", "queue"):
        (home / name).mkdir(exist_ok=True)

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "doctor.sh").write_bytes(DOCTOR.read_bytes())
    (repo / "scripts" / "doctor.sh").chmod(0o755)
    (repo / ".env").write_text(f"OBSIDIAN_VAULT={vault}\nVOICEDOCK_HOME={home}\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    config = COMPOSE_CONFIG.format(vault=vault, inbox=home / "inbox")
    (tmp_path / "compose-config.txt").write_text(config, encoding="utf-8")
    write_stub(
        bin_dir,
        "docker",
        'case "$*" in\n'
        f'  *"compose config"*) cat "{tmp_path}/compose-config.txt" ;;\n'
        "  *'compose ps'*) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac",
    )
    write_stub(bin_dir, "launchctl", "exit 0")
    return {
        "repo": repo,
        "home": home,
        "vault": vault,
        "settings": settings,
        "bin": bin_dir,
        "tmp": tmp_path,
    }


def run(host: dict[str, Path], **env: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{host['bin']}:{environment['PATH']}",
            "VOICEDOCK_DOCKER_SETTINGS_DIR": str(host["settings"]),
            "HOME": str(host["tmp"]),
        }
    )
    environment.update(env)
    return subprocess.run(  # noqa: S603 - argv はリスト。shell=True を使わない
        ["/bin/bash", str(host["repo"] / "scripts" / "doctor.sh")],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        check=False,
    )


def rows(output: str) -> list[str]:
    """DH の行だけ。**サマリより後（コンテナ内の 12 行）は数えない。**"""
    head = output.split("checks passed", 1)[0]
    return [line for line in head.splitlines() if line.startswith(("[✓]", "[!]", "[✗]", "[-]"))]


# --- 全体（§19.2 の出力の形） -------------------------------------------


def test_the_host_checks_are_five_rows(host: dict[str, Path]) -> None:
    """**DH は 5 件である**（§19.2）。削った検査を勝手に足さない。"""
    result = run(host)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(rows(result.stdout)) == 5, result.stdout


LABELS: tuple[str, ...] = (
    "Virtualization",
    "Compose config",
    "Helper config",
    "LaunchAgent",
    "Compose safety",
)
"""DH の 5 行のラベル。**順序も §19.2 の実行順（DH-1 → 10 → 13 → 12 → 15）である。**"""


def test_the_row_format_matches_the_spec(host: dict[str, Path]) -> None:
    """`[<記号>] <ラベル（21 桁左詰め）><詳細>`（§19.2）。

    **`doctor.py` と同じ桁で出す。**片方だけ変えると 17 行が揃わない。

    **詳細が始まる桁を直接見る。**「ラベルを切り出して右端を詰めたら同じ」だけだと、
    桁をずらしても詰め直した結果が一致してしまい**通る**（意図的に壊して確かめたら
    実際に通った）。
    """
    from voicedock.doctor import DETAIL_INDENT, LABEL_WIDTH, SEPARATOR

    result = run(host)
    printed = rows(result.stdout)
    assert [_label_of(line) for line in printed] == list(LABELS), printed

    for line in printed:
        assert line[3] == " ", line
        label = _label_of(line)
        padding = line[4 + len(label) : 4 + LABEL_WIDTH]
        assert padding == " " * (LABEL_WIDTH - len(label)), f"ラベルの桁が違う: {line!r}"
        detail_at = 4 + LABEL_WIDTH
        assert line[detail_at] != " ", f"詳細が {detail_at} 桁目から始まらない: {line!r}"

    assert SEPARATOR in result.stdout
    continuation = [
        line for line in result.stdout.splitlines() if line.startswith(" " * DETAIL_INDENT)
    ]
    assert continuation, "続き行が 1 つも無い"
    for line in continuation:
        assert line[DETAIL_INDENT] != " ", f"続き行のインデントが違う: {line!r}"


def _label_of(line: str) -> str:
    for label in LABELS:
        if line[4:].startswith(label):
            return label
    raise AssertionError(f"未知のラベル: {line!r}")


def test_the_summary_counts_the_rows(host: dict[str, Path]) -> None:
    result = run(host)
    assert "5 checks passed, 0 failed, 0 notices" in result.stdout, result.stdout


# --- DH-1: Apple Virtualization Framework(§3.2) -------------------------


def test_dh1_fails_and_aborts_when_virtualization_is_off(host: dict[str, Path]) -> None:
    """**致命的（中止）。**以降の検査を実行しない（§19.2 の実行順）。"""
    (host["settings"] / "settings-store.json").write_text(
        '{"UseVirtualizationFramework": false}\n', encoding="utf-8"
    )
    result = run(host)
    assert result.returncode == EXIT_DOCTOR_FATAL
    assert len(rows(result.stdout)) == 1, "中止せずに先へ進んでいる"
    assert "UseVirtualizationFramework=false" in result.stdout


def test_dh1_reads_the_legacy_settings_file(host: dict[str, Path]) -> None:
    """古い Docker Desktop は `settings.json` に書く。"""
    (host["settings"] / "settings-store.json").unlink()
    (host["settings"] / "settings.json").write_text(
        '{"UseVirtualizationFramework": true}\n', encoding="utf-8"
    )
    assert run(host).returncode == 0


def test_dh1_aborts_when_the_settings_are_missing(host: dict[str, Path]) -> None:
    (host["settings"] / "settings-store.json").unlink()
    result = run(host)
    assert result.returncode == EXIT_DOCTOR_FATAL
    assert len(rows(result.stdout)) == 1


# --- DH-10: compose の変数解決 ------------------------------------------


def test_dh10_fails_when_compose_config_fails(host: dict[str, Path]) -> None:
    """`${OBSIDIAN_VAULT:?}` が未設定ならここで落ちる（§19.2）。"""
    write_stub(
        host["bin"],
        "docker",
        'case "$*" in\n'
        '  *"compose config"*) echo "required variable OBSIDIAN_VAULT is missing" >&2; exit 1 ;;\n'
        "  *) exit 0 ;;\n"
        "esac",
    )
    result = run(host)
    assert result.returncode == EXIT_DOCTOR_FATAL
    assert "docker compose config が失敗しました" in result.stdout


def test_dh10_fails_when_the_vault_does_not_exist(host: dict[str, Path]) -> None:
    """**存在しないパスを bind mount すると空のディレクトリが作られ、Vault が空だと誤認する。**"""
    missing = host["tmp"] / "gone"
    (host["tmp"] / "compose-config.txt").write_text(
        COMPOSE_CONFIG.format(vault=missing, inbox=host["home"] / "inbox"), encoding="utf-8"
    )
    result = run(host)
    assert result.returncode == EXIT_DOCTOR_FATAL
    assert "存在しないディレクトリ" in result.stdout


def test_dh10_handles_a_vault_path_with_spaces(host: dict[str, Path]) -> None:
    """**空白を含む Vault パスで壊れない**（§19.2）。

    `~/Library/Mobile Documents/...` は実際に空白を含む。`awk '{print $2}'` で切ると切れる。
    """
    result = run(host)
    assert result.returncode == 0, result.stdout
    assert f"OBSIDIAN_VAULT={host['vault']} (exists)" in result.stdout


# --- DH-13: helper.conf（§7.4 の起動時検証 1〜7） -----------------------


def test_dh13_shows_the_effective_helper_settings(host: dict[str, Path]) -> None:
    result = run(host)
    assert "MOUNT_MODE=ro, DELETE_SOURCE_AUDIO=false" in result.stdout
    assert "INCLUDE_VOLUMES=" in result.stdout
    assert "EXCLUDE_VOLUMES=" in result.stdout


def test_dh13_warns_when_the_device_is_mounted_read_write(host: dict[str, Path]) -> None:
    """`MOUNT_MODE=rw` は**安全ロック 2-B が外れている**状態である（§14.2）。"""
    conf = host["home"] / "helper.conf"
    conf.write_text(conf.read_text(encoding="utf-8").replace("MOUNT_MODE=ro", "MOUNT_MODE=rw"))
    result = run(host)
    assert "[!]" in result.stdout
    assert "安全ロック 2-B" in result.stdout
    assert "1 notices" in result.stdout


def test_dh13_fails_when_the_home_disagrees(host: dict[str, Path]) -> None:
    """**`helper.conf` の `VOICEDOCK_HOME` が ingest の置き場所と食い違うと致命的**（§19.2）。

    heartbeat がコンテナの見ない場所へ書かれ、**H-8 が「Helper が止まっている」と
    報告し続ける**（実際は動いている）。
    """
    elsewhere = host["tmp"] / "Elsewhere"
    (elsewhere / "state").mkdir(parents=True)
    (elsewhere / "log").mkdir()
    conf = host["home"] / "helper.conf"
    conf.write_text(
        conf.read_text(encoding="utf-8").replace(
            f'VOICEDOCK_HOME="{host["home"]}"', f'VOICEDOCK_HOME="{elsewhere}"'
        )
    )
    result = run(host)
    assert result.returncode == EXIT_DOCTOR_FATAL
    assert "食い違います" in result.stdout


def test_dh13_fails_when_the_config_is_invalid(host: dict[str, Path]) -> None:
    """§7.4 の起動時検証は **`voicedock-ingest --check-config` に委ねる**（§19.2）。

    **検査を 2 箇所に書くと片方だけ古くなる**（v3.1 の `setup.sh` / `doctor.sh` の事故）。
    """
    conf = host["home"] / "helper.conf"
    conf.write_text(
        conf.read_text(encoding="utf-8").replace("MOUNT_MODE=ro", "MOUNT_MODE=sideways")
    )
    result = run(host)
    assert result.returncode == EXIT_DOCTOR_FATAL
    assert "起動時検証に失敗" in result.stdout


def test_dh13_fails_when_the_home_is_missing(host: dict[str, Path]) -> None:
    (host["repo"] / ".env").write_text(
        f"OBSIDIAN_VAULT={host['vault']}\nVOICEDOCK_HOME={host['tmp']}/nope\n", encoding="utf-8"
    )
    result = run(host)
    assert result.returncode == EXIT_DOCTOR_FATAL
    assert "がありません" in result.stdout


# --- DH-12: LaunchAgent -------------------------------------------------


def test_dh12_fails_when_the_agent_is_not_loaded(host: dict[str, Path]) -> None:
    """**Helper が動かなければ録音は 1 本も取り込まれない**（§19.2）。"""
    write_stub(host["bin"], "launchctl", "exit 113")
    result = run(host)
    assert result.returncode == EXIT_DOCTOR_FATAL
    assert "com.voicedock.ingest が load されていません" in result.stdout


def test_dh12_is_skipped_without_launchctl(host: dict[str, Path], tmp_path: Path) -> None:
    """**macOS 以外では確認できない。**失敗ではなく skip にする（§19.2 の `[-]`）。"""
    (host["bin"] / "launchctl").unlink()
    result = run(host)
    assert "[-] LaunchAgent" in result.stdout
    assert result.returncode == 0


# --- DH-15: /Volumes と ports:（§14.4 N-3 / N-13） ----------------------


def test_dh15_rejects_a_volumes_mount(host: dict[str, Path]) -> None:
    """**コンテナは `/Volumes` を mount しない**（N-3）。"""
    (host["tmp"] / "compose-config.txt").write_text(
        COMPOSE_CONFIG.format(vault=host["vault"], inbox="/Volumes/DJIMIC3"), encoding="utf-8"
    )
    result = run(host)
    assert result.returncode == EXIT_DOCTOR_FATAL
    assert "/Volumes (N-3)" in result.stdout


def test_dh15_rejects_published_ports(host: dict[str, Path]) -> None:
    """**ポートを公開しない**（N-13 / §14.5）。"""
    (host["tmp"] / "compose-config.txt").write_text(
        COMPOSE_CONFIG.format(vault=host["vault"], inbox=host["home"] / "inbox")
        + "    ports:\n      - 8080:8080\n",
        encoding="utf-8",
    )
    result = run(host)
    assert result.returncode == EXIT_DOCTOR_FATAL
    assert "ports: (N-13)" in result.stdout


def test_dh15_is_skipped_when_compose_config_failed(host: dict[str, Path]) -> None:
    """前提が崩れたら `[-]`（§19.2）。**既に数えた失敗の結果なので数えない。**"""
    write_stub(
        host["bin"],
        "docker",
        'case "$*" in\n  *"compose config"*) exit 1 ;;\n  *) exit 0 ;;\nesac',
    )
    result = run(host)
    assert "[-] Compose safety" in result.stdout


# --- 副作用を持たないこと（§19.2） --------------------------------------


def test_the_doctor_changes_nothing(host: dict[str, Path]) -> None:
    """**検査のみを行い、ホスト設定は変更しない**（§19.2）。"""
    before = {
        path: path.read_bytes()
        for path in sorted(host["tmp"].rglob("*"))
        if path.is_file() and "compose-config" not in path.name
    }
    run(host)
    after = {
        path: path.read_bytes()
        for path in sorted(host["tmp"].rglob("*"))
        if path.is_file() and "compose-config" not in path.name
    }
    assert after == before


# --- §19.2 の DH 表との突き合わせ ---------------------------------------


def spec_dh_ids() -> set[str]:
    from tests.spec_sync import spec_text

    text = spec_text()
    start = text.index("### 19.2")
    section = text[start : text.index("## 20. ", start)]
    listed = set(re.findall(r"^\| \*?\*?(DH-\d+)\*?\*? \| ", section, re.M))
    # 「v5.0 で削除した DH 検査」の表と混ざらないよう、削除側は左列が `DH-N <名前>` である
    return {name for name in listed if re.fullmatch(r"DH-\d+", name)}


def test_the_host_checks_match_the_spec_count() -> None:
    """§19.2 の DH 表が 5 件で、`doctor.sh` が全部の ID に触れていること。

    **件数のずれが最も危ない**（#55 の振り返り）。SPEC に DH-16 を足して実装を忘れると、
    **検査が足りないまま「総仕上げ完了」になる。**
    """
    listed = spec_dh_ids()
    assert listed == {"DH-1", "DH-10", "DH-12", "DH-13", "DH-15"}, sorted(listed)

    body = DOCTOR.read_text(encoding="utf-8")
    missing = [name for name in listed if name not in body]
    assert missing == [], f"doctor.sh が触れていない検査: {missing}"


@pytest.mark.parametrize(
    "removed",
    ["DH-2", "DH-3", "DH-4", "DH-5", "DH-6", "DH-7", "DH-8", "DH-9", "DH-11", "DH-14"],
)
def test_the_removed_host_checks_stay_removed(removed: str) -> None:
    """**削った検査を勝手に足さない**（§19.2 の「v5.0 で削除した DH 検査 10 件」）。

    足すなら §19.2 の表を先に直すこと。
    """
    assert removed not in spec_dh_ids()


def test_the_doctor_writes_nothing(host: dict[str, Path]) -> None:
    """**検査のみを行い、ホスト設定は変更しない**（§19.2）。**静的に見る。**

    実行して差分が無いことを見るだけでは足りない。`rm -f` は対象が無ければ何も
    変えないので、**削除を書き足しても通ってしまう**（意図的に壊して確かめたら
    実際に通った）。

    v3.1 は `setup.sh` と `doctor.sh` に同じ検査を二重に書き、片方だけ更新して
    食い違う事故を起こした。**doctor が設定を直せるようにすると、また同じ形になる。**
    """
    body = DOCTOR.read_text(encoding="utf-8")
    lines = [re.sub(r"(^|\s)#.*$", "", line) for line in body.splitlines()]

    forbidden = {
        "rm": r"\brm\b",
        "mv": r"\bmv\b",
        "mkdir": r"\bmkdir\b",
        "chmod / chown": r"\bch(mod|own)\b",
        "defaults write": r"\bdefaults\s+write\b",
        "launchctl の load / bootstrap": r"\blaunchctl\s+(load|unload|bootstrap|bootout)\b",
        "diskutil": r"\bdiskutil\b",
        "docker compose up / down": r"\bcompose\s+(up|down|restart)\b",
        "tee": r"\btee\b",
        # `>/dev/null` と、文字列中の `Settings > General` は除く。
        # **行き先がパス（`/` か `$` 始まり）のものだけを拾う**
        "ファイルへのリダイレクト": r"(?<![0-9<>])>>?\s*(?!/dev/)[\"']?[$/]",
    }
    for label, pattern in forbidden.items():
        offenders = [line.strip() for line in lines if re.search(pattern, line)]
        assert offenders == [], f"doctor.sh がホストを変更しうる（{label}）: {offenders}"
