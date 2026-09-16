"""`scripts/probe.sh` が P0-8 の一致を機械判定することを固定する（#93）。

**`ro` で繋いだ瞬間は採り直しが効かない。**`heartbeat.json` は次の ingest で上書きされる。
だから判定を人の目視に委ねず、**食い違いを終了コードで出す。**

issue #3 は P0-8 を「`mount` が read-only であり、`heartbeat.json` の `mount_readonly` が
`true` であること。**両者が一致すること**」と規定し、**一致しなければ実装のバグである**と
している。ingest は `mount` の出力を `diskutil` とは独立に読んで `mount_readonly` を書くので
（`_is_mounted_readonly()`）、**両者は必ず一致する。**

`mount` は `PATH` ではなく `VOICEDOCK_MOUNT_CMD` で差し替える。**本物を呼ばない。**
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT

PROBE = REPO_ROOT / "scripts" / "probe.sh"

pytestmark = pytest.mark.skipif(not PROBE.is_file(), reason="scripts/ がマウントされていない")

EXIT_PROBE_MISMATCH = 5

DEVICE = "DJIMIC3"
FOLDER = "TX_MIC001_20260912_163444"
ORIG = f"{FOLDER}/TX00_MIC001_20260912_163444_orig.wav"
DENOISED = f"{FOLDER}/TX00_MIC001_20260912_163444.wav"


def write_mount_stub(tmp_path: Path, lines: list[str]) -> Path:
    """`mount` の偽物。**1 行の形は実機と同じにする**（`<dev> on <path> (<opts>)`）。"""
    path = tmp_path / "fake-mount"
    body = "\n".join(f'echo "{line}"' for line in lines)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def build_home(
    tmp_path: Path,
    *,
    mount_readonly: bool | None = True,
    devices: dict[str, list[str]] | None = None,
    heartbeat_text: str | None = None,
    mount_mode: str = "ro",
) -> Path:
    """`<VOICEDOCK_HOME>` を組み立てる。`mount_readonly=None` で heartbeat を置かない。"""
    home = tmp_path / "VoiceDock"
    (home / "state").mkdir(parents=True)
    (home / "log").mkdir()
    (home / "helper.conf").write_text(
        f'MOUNT_MODE="{mount_mode}"\n'
        "STABILITY_FAST_PATH_SECONDS=60\n"
        "STABILITY_INTERVAL_SECONDS=0\n"
        "STABILITY_CHECKS=2\n",
        encoding="utf-8",
    )

    if heartbeat_text is not None:
        (home / "state" / "heartbeat.json").write_text(heartbeat_text, encoding="utf-8")
    elif mount_readonly is not None:
        (home / "state" / "heartbeat.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "updated_at": "2026-09-13T07:00:12+09:00",
                    "helper_version": "5.9.0",
                    "mount_mode": "ro",
                    "mount_readonly": mount_readonly,
                    "delete_source_audio": False,
                    "reaper_installed": False,
                    "config_error": None,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    payload = {DEVICE: [ORIG]} if devices is None else devices
    (home / "state" / "inventory.json").write_text(_render_inventory(payload), encoding="utf-8")
    return home


def _render_inventory(devices: dict[str, list[str]]) -> str:
    """`voicedock-ingest` の `write_inventory()` と**同じ字面**で書く。

    probe は `jq` を使わずインデントで読む（macOS に `jq` が無い。§3.3）ので、
    **テストが本物と違う字面で書くと、probe の読み取りを検査したことにならない。**
    """
    lines = [
        "{",
        '  "schema": 1,',
        '  "generated_at": "2026-09-13T07:00:12+09:00",',
        '  "mount_readonly": true,',
        '  "device_free_bytes": {',
    ]
    free = [f'    "{name}": 4509715660' for name in devices]
    lines.append(",\n".join(free))
    lines.append("  },")
    lines.append('  "devices": {')
    blocks = []
    for name, relpaths in devices.items():
        entries = ",\n".join(f'      "{rel}"' for rel in relpaths)
        blocks.append(f'    "{name}": [\n{entries}\n    ]')
    lines.append(",\n".join(blocks))
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def run_probe(
    tmp_path: Path, *, home: Path, mount_lines: list[str], args: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    volumes = tmp_path / "Volumes"
    for name in (DEVICE,):
        (volumes / name / FOLDER).mkdir(parents=True, exist_ok=True)
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(PROBE), *args],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "VOICEDOCK_HOME": str(home),
            "VOICEDOCK_VOLUMES_ROOT": str(volumes),
            "VOICEDOCK_MOUNT_CMD": str(write_mount_stub(tmp_path, mount_lines)),
            "PROBE_SAMPLES": "2",
        },
    )


def mounted(tmp_path: Path, name: str, *, readonly: bool) -> str:
    options = "msdos, local, nodev, nosuid" + (", read-only" if readonly else "")
    return f"/dev/disk4s1 on {tmp_path / 'Volumes' / name} ({options}, noowners)"


def p0_8_section(stdout: str) -> str:
    return stdout.split("## P0-8", 1)[1].split("## P0-10", 1)[0]


# --- P0-8 の一致判定 -----------------------------------------------------


def test_agreement_passes(tmp_path: Path) -> None:
    """`ro` マウントと `mount_readonly: true` が一致すれば PASS。"""
    home = build_home(tmp_path, mount_readonly=True)
    result = run_probe(tmp_path, home=home, mount_lines=[mounted(tmp_path, DEVICE, readonly=True)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "✅ **PASS**" in p0_8_section(result.stdout)


def test_a_readonly_device_says_the_lock_is_in_effect(tmp_path: Path) -> None:
    """**一致と「ロックが効いている」は別の問いである。**

    v5.19 までは一致しさえすれば「ロック 2-B が実際に効いている」と書いていた。
    """
    home = build_home(tmp_path, mount_readonly=True)
    result = run_probe(tmp_path, home=home, mount_lines=[mounted(tmp_path, DEVICE, readonly=True)])
    section = p0_8_section(result.stdout)
    assert "✅ **PASS**" in section
    assert "ロック 2-B: **効いている**" in section


def test_rw_mode_says_the_lock_is_released_on_purpose(tmp_path: Path) -> None:
    """`MOUNT_MODE=rw` なら**外れているのが意図どおり**である（Phase 7 の状態。P0-8b）。"""
    home = build_home(tmp_path, mount_readonly=False, mount_mode="rw")
    result = run_probe(tmp_path, home=home, mount_lines=[mounted(tmp_path, DEVICE, readonly=False)])
    section = p0_8_section(result.stdout)
    assert result.returncode == 0
    assert "✅ **PASS**" in section
    assert "ロック 2-B: **外れている**" in section


def test_ro_mode_with_a_writable_device_says_the_lock_is_not_applied(tmp_path: Path) -> None:
    """**これが 2026-09-14 の 18:11 に実機で起きた形である**（`docs/POC.md` §10.1.2）。

    `MOUNT_MODE=ro` を意図したのにデバイスは書き込み可能。`mount` と `heartbeat` は
    **一致しているので PASS** だが、**保護はされていない。**
    v5.19 までは「ロック 2-B が実際に効いている」と表示していた —
    **保護されていないことを保護されていると読ませていた。**
    """
    home = build_home(tmp_path, mount_readonly=False, mount_mode="ro")
    result = run_probe(tmp_path, home=home, mount_lines=[mounted(tmp_path, DEVICE, readonly=False)])
    section = p0_8_section(result.stdout)
    assert result.returncode == 0, "一致はしているので FAIL ではない"
    assert "✅ **PASS**" in section
    assert "ロック 2-B: ⚠ **かかっていない**" in section
    # **意図した値そのものを見る。**文言だけを見ていたため、`%s` と引数が別の `printf` に
    # 分かれていて `MOUNT_MODE=` が空で出る不具合を通していた（実機で踏んだ）
    assert "`MOUNT_MODE=ro`" in section
    assert "効いている" not in section


def test_a_readwrite_mount_with_a_true_heartbeat_fails(tmp_path: Path) -> None:
    """**これが実装のバグの形である。**`mount` は rw なのに `heartbeat` は `true`。

    `_is_mounted_readonly()` が `diskutil` の終了コードを信用してしまうと、
    再マウントに失敗したのに `true` を書く。**そのまま Phase 7 に入ると、
    安全ロック 2-B が掛かっていないのに掛かっていると信じることになる。**
    """
    home = build_home(tmp_path, mount_readonly=True)
    result = run_probe(tmp_path, home=home, mount_lines=[mounted(tmp_path, DEVICE, readonly=False)])
    assert result.returncode == EXIT_PROBE_MISMATCH
    assert "✗ **FAIL**" in p0_8_section(result.stdout)


def test_a_readonly_mount_with_a_false_heartbeat_fails(tmp_path: Path) -> None:
    """**逆向きの食い違いも落とす。**片方向だけの検査では半分しか守れない。"""
    home = build_home(tmp_path, mount_readonly=False)
    result = run_probe(tmp_path, home=home, mount_lines=[mounted(tmp_path, DEVICE, readonly=True)])
    assert result.returncode == EXIT_PROBE_MISMATCH
    assert "✗ **FAIL**" in p0_8_section(result.stdout)


def test_a_device_missing_from_mount_is_undecidable(tmp_path: Path) -> None:
    """`inventory.json` に在るデバイスが `mount` に無ければ**判定しない。**

    2 つのスナップショットがずれているだけで、実装のバグとは限らない。
    **「分からない」を「FAIL」と言わない。**
    """
    home = build_home(tmp_path, mount_readonly=True)
    result = run_probe(tmp_path, home=home, mount_lines=["/dev/disk1s1 on / (apfs, local)"])
    assert result.returncode == 0
    assert "判定不能" in p0_8_section(result.stdout)
    assert "FAIL" not in p0_8_section(result.stdout)


def test_no_device_is_undecidable(tmp_path: Path) -> None:
    """デバイスが `inventory.json` に 1 つも無ければ判定しない。"""
    home = build_home(tmp_path, mount_readonly=True, devices={})
    result = run_probe(tmp_path, home=home, mount_lines=["/dev/disk1s1 on / (apfs, local)"])
    assert result.returncode == 0
    assert "判定不能" in p0_8_section(result.stdout)


def test_a_missing_heartbeat_is_undecidable(tmp_path: Path) -> None:
    """`heartbeat.json` が無ければ判定しない（Helper が未実行なだけかもしれない）。"""
    home = build_home(tmp_path, mount_readonly=None)
    result = run_probe(tmp_path, home=home, mount_lines=[mounted(tmp_path, DEVICE, readonly=True)])
    assert result.returncode == 0
    assert "判定不能" in p0_8_section(result.stdout)


def test_a_broken_heartbeat_does_not_crash(tmp_path: Path) -> None:
    """**壊れた JSON で落ちない**（§7.5）。Helper の書き込み途中を読む可能性がある。"""
    home = build_home(tmp_path, heartbeat_text='{"schema": 1, "mount_rea')
    result = run_probe(tmp_path, home=home, mount_lines=[mounted(tmp_path, DEVICE, readonly=True)])
    assert result.returncode == 0, result.stderr
    assert "判定不能" in p0_8_section(result.stdout)


def test_the_volume_name_is_matched_exactly(tmp_path: Path) -> None:
    """**部分一致で拾わない。**`DJIMIC3` の検査が `DJIMIC30` の行に当たってはならない。"""
    home = build_home(tmp_path, mount_readonly=True)
    result = run_probe(
        tmp_path,
        home=home,
        mount_lines=[
            mounted(tmp_path, DEVICE + "0", readonly=True),
            mounted(tmp_path, DEVICE, readonly=False),
        ],
    )
    assert result.returncode == EXIT_PROBE_MISMATCH


# --- P0-10 / P0-13 -------------------------------------------------------


def test_two_devices_are_counted_separately(tmp_path: Path) -> None:
    """**P0-10**: 送信機 2 台なら `devices` が 2 エントリ。**録音数はデバイスごとに数える。**"""
    home = build_home(
        tmp_path,
        mount_readonly=True,
        devices={DEVICE: [ORIG], "DJIMIC3B": [ORIG, DENOISED]},
    )
    (tmp_path / "Volumes" / "DJIMIC3B" / FOLDER).mkdir(parents=True)
    result = run_probe(
        tmp_path,
        home=home,
        mount_lines=[
            mounted(tmp_path, DEVICE, readonly=True),
            mounted(tmp_path, "DJIMIC3B", readonly=True),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    section = result.stdout.split("## P0-10", 1)[1].split("## P0-11", 1)[0]
    assert "**2 個**" in section
    rows = dict(re.findall(r"^\| `([^`]+)` \| (\d+) \|", section, re.M))
    assert rows == {DEVICE: "1", "DJIMIC3B": "2"}, section


def test_a_denoised_file_is_reported(tmp_path: Path) -> None:
    """**P0-13**: `_orig` 以外が在れば §5.3 の代償が現実になっている。"""
    home = build_home(tmp_path, mount_readonly=True, devices={DEVICE: [ORIG, DENOISED]})
    result = run_probe(tmp_path, home=home, mount_lines=[mounted(tmp_path, DEVICE, readonly=True)])
    section = result.stdout.split("## P0-13", 1)[1].split("## P0-15", 1)[0]
    assert "うち `_orig` 以外 : **1 件**" in section
    assert DENOISED in section


def test_only_orig_reports_nothing_accumulating(tmp_path: Path) -> None:
    """陰性対照。`_orig` だけなら「溜まっていない」と言い切れること。"""
    home = build_home(tmp_path, mount_readonly=True, devices={DEVICE: [ORIG]})
    result = run_probe(tmp_path, home=home, mount_lines=[mounted(tmp_path, DEVICE, readonly=True)])
    section = result.stdout.split("## P0-13", 1)[1].split("## P0-15", 1)[0]
    assert "うち `_orig` 以外 : **0 件**" in section
    assert "denoised は生成されていない" in section


# --- §14.4 の禁則（静的に見えるもの） ------------------------------------


def source_without_comments() -> str:
    return "\n".join(
        re.sub(r"(^|\s)#.*$", "", line) for line in PROBE.read_text(encoding="utf-8").splitlines()
    )


def test_probe_never_calls_diskutil() -> None:
    """**N-19 と同じ理由**: マウント操作は `voicedock-ingest` だけが行う。

    probe が `diskutil` を呼ぶと、**測ろうとしている状態そのものを変えてしまう。**
    """
    assert "diskutil" not in source_without_comments()


def test_probe_never_writes_to_the_device() -> None:
    """**N-18**: デバイス上のパスへ破壊的操作を書かないこと。"""
    body = source_without_comments()
    for line in body.splitlines():
        if not re.search(r"\b(rm|mv|rmdir|truncate|chmod|chown|touch)\b", line):
            continue
        assert "VOLUMES_ROOT" not in line, f"デバイスへ書いている: {line.strip()}"


def test_probe_does_not_run_ingest() -> None:
    """**probe は ingest を実行しない。**走らせると他の項目が採り直しになる。"""
    body = source_without_comments()
    assert not re.search(r"^[^#]*[^/\w]bin/voicedock-ingest[\"']?\s*$", body, re.M), (
        "probe が ingest を実行している"
    )
