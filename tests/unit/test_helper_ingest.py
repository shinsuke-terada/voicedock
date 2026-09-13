"""`helper/voicedock-ingest` が SPEC §5.4 / §7.4 / §7.5 / §10.1〜§10.3 に従うことを固定する。

**§20.1 の「デバイス判定」「安定性判定」は Helper 層で検証する。**v4.0 でこの処理が
コンテナからホストへ移ったので（§4.2）、Python のテストでは動かせない。偽ボリュームを
`tmp_path` へ組み立て、`bash helper/voicedock-ingest` を `subprocess` で実行する。

**`mount_readonly: true` の経路はここでは検証できない。**`diskutil` が Linux に無いため
再マウントは必ず失敗し、§14.2 のとおり `false` が書かれる（これが正しい挙動である）。
`true` になることの確認は実機でしか行えない（#3 / P0-8、`mount | grep -i dji`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.fixtures.fake_tree import DEVICE_ID, build_fake_volumes
from tests.helpers import REPO_ROOT
from tests.spec_sync import spec_json_block

INGEST = REPO_ROOT / "helper" / "voicedock-ingest"

pytestmark = pytest.mark.skipif(not INGEST.is_file(), reason="helper/ がマウントされていない")

DEFAULT_CONF: dict[str, str] = {
    "INCLUDE_VOLUMES": "()",
    "EXCLUDE_VOLUMES": '("Macintosh HD" "com.apple.TimeMachine.*" ".*")',
    "MOUNT_MODE": "ro",
    "DELETE_SOURCE_AUDIO": "false",
    "STABILITY_FAST_PATH_SECONDS": "60",
    "STABILITY_INTERVAL_SECONDS": "1",
    "STABILITY_CHECKS": "1",
    "MAX_SCAN_DEPTH": "3",
}

OLD_MTIME = time.time() - 3600
"""fixture の mtime を 1 時間前にする。

`STABILITY_FAST_PATH_SECONDS` は既定の 60 秒のまま使い、**即断の経路でテストを速く保つ。**
`0` にして検査を無効化すると、本番で使ってはならない値をテストが正当化してしまう。
"""


def write_conf(tmp_path: Path, **overrides: str) -> Path:
    """`helper.conf` を組み立てる。`VOLUMES_ROOT` / `VOICEDOCK_HOME` は `tmp_path` 配下。"""
    values = dict(DEFAULT_CONF)
    values.update(overrides)
    values.setdefault("VOLUMES_ROOT", str(tmp_path / "volumes"))
    values.setdefault("VOICEDOCK_HOME", str(tmp_path / "home"))
    conf = tmp_path / "helper.conf"
    conf.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    for name in ("state", "log"):
        (tmp_path / "home" / name).mkdir(parents=True, exist_ok=True)
    return conf


def age_files(root: Path) -> None:
    """デバイス上の全ファイルの mtime を過去にする（即断の経路に載せる）。"""
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (OLD_MTIME, OLD_MTIME))


BASH = shutil.which("bash") or "/bin/bash"


def run_ingest(conf: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["VOICEDOCK_HELPER_CONF"] = str(conf)
    return subprocess.run(  # noqa: S603
        [BASH, str(INGEST), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )


@pytest.fixture
def volumes(tmp_path: Path) -> Path:
    root = build_fake_volumes(tmp_path / "volumes")
    age_files(root)
    return root


def inbox_files(tmp_path: Path) -> list[str]:
    inbox = tmp_path / "home" / "inbox"
    if not inbox.is_dir():
        return []
    return sorted(str(p.relative_to(inbox)) for p in inbox.rglob("*") if p.is_file())


def read_json(path: Path) -> dict[str, object]:
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return document


# --- 取り込み -----------------------------------------------------------


def test_copies_only_orig(tmp_path: Path, volumes: Path) -> None:
    """`_orig` だけを inbox へ置く（§5.3。v5.0 の `_orig` 固定）。"""
    result = run_ingest(write_conf(tmp_path))
    assert result.returncode == 0, result.stderr
    names = inbox_files(tmp_path)
    assert names, result.stdout
    wavs = [n for n in names if n.endswith(".wav")]
    assert wavs
    for name in wavs:
        assert name.endswith("_orig.wav"), f"denoised を取り込んでいる: {name}"
    # 1 wav につき 1 meta
    assert len([n for n in names if n.endswith(".meta.json")]) == len(wavs)


def test_meta_matches_spec_shape(tmp_path: Path, volumes: Path) -> None:
    """`.meta.json` が §10.2 の全キーを持ち、`sha256` が実ファイルと一致すること。"""
    run_ingest(write_conf(tmp_path))
    inbox = tmp_path / "home" / "inbox"
    metas = sorted(inbox.rglob("*.meta.json"))
    assert metas
    for meta_path in metas:
        meta = read_json(meta_path)
        assert set(meta) == {
            "schema",
            "device_id",
            "relpath",
            "size",
            "mtime",
            "sha256",
            "copied_at",
            "helper_version",
        }
        wav = meta_path.with_name(meta_path.name[: -len(".meta.json")])
        assert wav.is_file()
        assert meta["size"] == wav.stat().st_size
        assert meta["sha256"] == hashlib.sha256(wav.read_bytes()).hexdigest()
        # **relpath は相対パスで、絶対パスも `..` も含まない**（§14.1.1 / N-17）
        relpath = meta["relpath"]
        assert isinstance(relpath, str)
        assert not relpath.startswith("/")
        assert ".." not in relpath.split("/")
        assert meta["device_id"] == DEVICE_ID


def test_source_device_is_never_modified(tmp_path: Path, volumes: Path) -> None:
    """**デバイスへ書き込まない・改名しない・移動しない**（§14.4 N-11 / N-18）。

    デバイス側を書き込み不可にしても取り込みが成功し、かつ実行前後で
    ファイル一覧・サイズ・内容が完全に一致すること。
    """

    def snapshot() -> dict[str, tuple[int, str]]:
        return {
            str(p.relative_to(volumes)): (
                p.stat().st_size,
                hashlib.sha256(p.read_bytes()).hexdigest(),
            )
            for p in sorted(volumes.rglob("*"))
            if p.is_file()
        }

    before = snapshot()
    assert before
    for path in sorted(volumes.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    volumes.chmod(0o555)
    try:
        result = run_ingest(write_conf(tmp_path))
        assert result.returncode == 0, result.stderr
        assert inbox_files(tmp_path), "読み取り専用のデバイスから取り込めていない"
    finally:
        volumes.chmod(0o755)
        for path in sorted(volumes.rglob("*")):
            path.chmod(0o755 if path.is_dir() else 0o644)
    assert snapshot() == before, "デバイス側が変わっている（N-18 違反）"


# --- 墓標（§10.2） -----------------------------------------------------


def test_second_run_does_not_recopy(tmp_path: Path, volumes: Path) -> None:
    """2 回目の実行でコピーしないこと。

    **記憶が無いと launchd の 300 秒ごとの起動で毎回全件コピーになる**
    （3 日分なら 5 分ごとに約 50 GB。§10.2）。
    """
    conf = write_conf(tmp_path)
    run_ingest(conf)
    wav = next((tmp_path / "home" / "inbox").rglob("*_orig.wav"))
    before = wav.stat().st_mtime_ns
    result = run_ingest(conf)
    assert "candidates=0" in result.stdout, result.stdout
    assert wav.stat().st_mtime_ns == before


def test_tombstone_alone_blocks_recopy(tmp_path: Path, volumes: Path) -> None:
    """**`.meta.json` だけが残っていれば再コピーしない**（§10.2）。

    コンテナは変換後に `.wav` だけを消す（§10.5）。その状態が「処理済み」である。
    """
    conf = write_conf(tmp_path)
    run_ingest(conf)
    wav = next((tmp_path / "home" / "inbox").rglob("*_orig.wav"))
    wav.unlink()
    result = run_ingest(conf)
    assert "candidates=0" in result.stdout, result.stdout
    assert not wav.exists()
    assert wav.with_name(wav.name + ".meta.json").is_file()


def test_removing_the_tombstone_requests_a_recopy(tmp_path: Path, volumes: Path) -> None:
    """**墓標を消すと再コピーされる**（§10.2。コンテナが再コピーを要求する唯一の手段）。"""
    conf = write_conf(tmp_path)
    run_ingest(conf)
    wav = next((tmp_path / "home" / "inbox").rglob("*_orig.wav"))
    meta = wav.with_name(wav.name + ".meta.json")
    wav.unlink()
    meta.unlink()
    result = run_ingest(conf)
    assert result.returncode == 0, result.stderr
    assert wav.is_file(), result.stdout
    assert meta.is_file()


def test_wav_without_tombstone_is_recopied(tmp_path: Path, volumes: Path) -> None:
    """`.wav` だけがある状態（コピー中に落ちた痕跡）はコピーし直す（§10.2）。"""
    conf = write_conf(tmp_path)
    run_ingest(conf)
    wav = next((tmp_path / "home" / "inbox").rglob("*_orig.wav"))
    wav.with_name(wav.name + ".meta.json").unlink()
    wav.write_bytes(b"truncated")
    result = run_ingest(conf)
    assert result.returncode == 0, result.stderr
    assert wav.read_bytes() != b"truncated", "壊れたコピーが残っている"


# --- state（§7.5） -----------------------------------------------------


def test_heartbeat_matches_spec_shape(tmp_path: Path, volumes: Path) -> None:
    """`heartbeat.json` が §7.5 の全キーを持つこと。

    **`mount_readonly` は `false` である。**`diskutil` が Linux に無いため再マウントは
    必ず失敗し、§14.2 のとおり `false` を書く（**失敗は安全側へ倒れる** — reaper は
    検証 2 で削除を拒否する）。`true` の確認は実機でしか行えない（#3 / P0-8）。
    """
    run_ingest(write_conf(tmp_path))
    heartbeat = read_json(tmp_path / "home" / "state" / "heartbeat.json")
    assert set(heartbeat) == {
        "schema",
        "updated_at",
        "helper_version",
        "mount_mode",
        "mount_readonly",
        "delete_source_audio",
        "reaper_installed",
        "include_volumes",
        "exclude_volumes",
        "config_error",
    }
    assert heartbeat["mount_mode"] == "ro"
    assert heartbeat["mount_readonly"] is False
    assert heartbeat["delete_source_audio"] is False
    assert heartbeat["reaper_installed"] is False
    assert heartbeat["config_error"] is None
    assert heartbeat["include_volumes"] == []
    assert heartbeat["exclude_volumes"] == [
        "Macintosh HD",
        "com.apple.TimeMachine.*",
        ".*",
    ]


def test_inventory_matches_spec_shape(tmp_path: Path, volumes: Path) -> None:
    """`inventory.json` が §7.5 の形で、`devices` がソート済みであること。"""
    run_ingest(write_conf(tmp_path))
    inventory = read_json(tmp_path / "home" / "state" / "inventory.json")
    assert set(inventory) == {"schema", "generated_at", "mount_readonly", "devices"}
    devices = inventory["devices"]
    assert isinstance(devices, dict)
    assert list(devices) == [DEVICE_ID]
    files = devices[DEVICE_ID]
    assert isinstance(files, list)
    assert files == sorted(files), "relpath がソートされていない（差分が読めない）"
    assert files


def test_state_files_match_the_spec_examples(tmp_path: Path, volumes: Path) -> None:
    """`heartbeat.json` / `inventory.json` / `.meta.json` のキー集合が SPEC の例と一致すること。

    §7.5 の 2 例と §10.2 の 1 例を**SPEC から読んで**突き合わせる。
    SPEC にキーを足して Helper に写し忘れる事故、その逆の事故が落ちる
    （`config.example.yaml` と §7.2 の関係と同じ）。
    """
    run_ingest(write_conf(tmp_path))
    state = tmp_path / "home" / "state"
    # §7.5 の 1 つめが heartbeat、2 つめが inventory
    assert set(read_json(state / "heartbeat.json")) == set(spec_json_block("7.5", 0))
    assert set(read_json(state / "inventory.json")) == set(spec_json_block("7.5", 1))
    meta = next((tmp_path / "home" / "inbox").rglob("*.meta.json"))
    assert set(read_json(meta)) == set(spec_json_block("10.2", 0))


def test_inventory_includes_denoised(tmp_path: Path, volumes: Path) -> None:
    """**inventory は `_orig` に限らず全件載せる**（§7.5）。

    「denoised がデバイスに溜まっているか」を答えられる唯一の場所である（§5.3 の代償）。
    取り込み（`_orig` だけ）とは範囲が違う。
    """
    run_ingest(write_conf(tmp_path))
    inventory = read_json(tmp_path / "home" / "state" / "inventory.json")
    devices = inventory["devices"]
    assert isinstance(devices, dict)
    files = devices[DEVICE_ID]
    assert isinstance(files, list)
    assert any(not name.endswith("_orig.wav") for name in files), files


def test_inventory_excludes_dotfiles_and_noise(tmp_path: Path, volumes: Path) -> None:
    """`.` 始まり（AppleDouble / `.Trashes`）と非 DJI ファイルを載せないこと（§10.2）。"""
    run_ingest(write_conf(tmp_path))
    inventory = read_json(tmp_path / "home" / "state" / "inventory.json")
    devices = inventory["devices"]
    assert isinstance(devices, dict)
    files = devices[DEVICE_ID]
    assert isinstance(files, list)
    for name in files:
        assert not any(part.startswith(".") for part in name.split("/")), name
        assert name.endswith((".wav", ".WAV")), name
    assert not any("Trashes" in name for name in files)
    assert not any("NOTES" in name for name in files)


def test_state_files_are_valid_json_with_unicode_volume_name(tmp_path: Path) -> None:
    """ボリューム名に空白と非 ASCII があっても有効な JSON を書くこと（判断 8）。"""
    name = 'マイク "DJI" 1'
    root = build_fake_volumes(tmp_path / "volumes")
    (root / DEVICE_ID).rename(root / name)
    age_files(root)
    run_ingest(write_conf(tmp_path))
    inventory = read_json(tmp_path / "home" / "state" / "inventory.json")
    devices = inventory["devices"]
    assert isinstance(devices, dict)
    assert name in devices


# --- §7.4 の起動時検証 1〜7 ---------------------------------------------

BROKEN_CONFS: list[tuple[str, dict[str, str], str]] = [
    ("1", {"VOLUMES_ROOT": "/nonexistent/volumes"}, "VOLUMES_ROOT"),
    ("2", {"VOICEDOCK_HOME": "/nonexistent/home"}, "VOICEDOCK_HOME"),
    ("3", {"EXCLUDE_VOLUMES": '"Macintosh HD .*"'}, "EXCLUDE_VOLUMES"),
    ("3", {"INCLUDE_VOLUMES": '"DJIMIC3"'}, "INCLUDE_VOLUMES"),
    ("4", {"EXCLUDE_VOLUMES": '("Macintosh HD" "")'}, "empty pattern"),
    ("5", {"MOUNT_MODE": "readonly"}, "MOUNT_MODE"),
    ("6", {"DELETE_SOURCE_AUDIO": "yes"}, "DELETE_SOURCE_AUDIO"),
    ("7", {"STABILITY_CHECKS": "0"}, "STABILITY_CHECKS"),
    ("7", {"STABILITY_INTERVAL_SECONDS": "0"}, "STABILITY_INTERVAL_SECONDS"),
    ("7", {"MAX_SCAN_DEPTH": "0"}, "MAX_SCAN_DEPTH"),
]


@pytest.mark.parametrize(
    ("check", "overrides", "expected"),
    BROKEN_CONFS,
    ids=[f"{check}-{next(iter(o))}" for check, o, _ in BROKEN_CONFS],
)
def test_broken_conf_imports_nothing(
    tmp_path: Path, volumes: Path, check: str, overrides: dict[str, str], expected: str
) -> None:
    """**検証に違反したら取り込まずに終了する**（§7.4）。

    設定ミスで無言のまま 1 本も取り込まれない状態を作らないため、`config_error` を
    `heartbeat.json` へ残す（コンテナの H-8 / D-18 が拾う。§22 R-23）。
    """
    conf = write_conf(tmp_path, **overrides)
    result = run_ingest(conf)
    assert result.returncode != 0, result.stdout
    assert inbox_files(tmp_path) == [], "違反があるのに取り込んでいる"
    assert expected in result.stderr, result.stderr
    heartbeat_path = tmp_path / "home" / "state" / "heartbeat.json"
    if heartbeat_path.is_file():  # 検証 2 の違反では書けない場合がある
        heartbeat = read_json(heartbeat_path)
        error = heartbeat["config_error"]
        assert isinstance(error, str)
        assert error.startswith(f"{check}:"), error


def test_check_3_catches_a_string_instead_of_an_array(tmp_path: Path, volumes: Path) -> None:
    """**検査 3 の本命。**v4.0 の例は空白区切りの文字列で、除外がまったく働かなかった（§7.4）。"""
    result = run_ingest(write_conf(tmp_path, EXCLUDE_VOLUMES='"Macintosh HD .*"'))
    assert "must be a bash array" in result.stderr, result.stderr


def test_check_config_does_not_import(tmp_path: Path, volumes: Path) -> None:
    """`--check-config` は検証だけを行い、取り込まない（§7.4）。"""
    conf = write_conf(tmp_path)
    ok = run_ingest(conf, "--check-config")
    assert ok.returncode == 0, ok.stderr
    assert inbox_files(tmp_path) == []

    bad = run_ingest(write_conf(tmp_path, MOUNT_MODE="nope"), "--check-config")
    assert bad.returncode != 0
    assert inbox_files(tmp_path) == []


# --- §5.4 デバイス判定 --------------------------------------------------


def test_include_empty_lets_everything_through(tmp_path: Path, volumes: Path) -> None:
    """`INCLUDE_VOLUMES=()`（既定）なら制限なし（§5.4 規則 1）。"""
    result = run_ingest(write_conf(tmp_path, INCLUDE_VOLUMES="()"))
    assert f"device_detected name={DEVICE_ID}" in result.stdout


def test_include_glob_matches(tmp_path: Path, volumes: Path) -> None:
    result = run_ingest(write_conf(tmp_path, INCLUDE_VOLUMES='("DJIMIC*")'))
    assert f"device_detected name={DEVICE_ID}" in result.stdout


def test_include_that_matches_nothing_imports_nothing(tmp_path: Path, volumes: Path) -> None:
    """綴りを間違えると 1 台も通らない。**だから DH-13 が実効値を表示する**（§5.4）。"""
    result = run_ingest(write_conf(tmp_path, INCLUDE_VOLUMES='("NOPE")'))
    assert result.returncode == 0, result.stderr
    assert "not in INCLUDE_VOLUMES" in result.stdout
    assert inbox_files(tmp_path) == []


def test_patterns_are_globs_not_regexes(tmp_path: Path, volumes: Path) -> None:
    """**`.*` は glob として評価する**（§5.4）。

    正規表現と解釈すると全ボリューム名に一致し、**DJI が 1 台も検出されないまま無言で
    停止する。**この症状は P0-6（ホットプラグ伝播の失敗）と区別がつかない。
    """
    result = run_ingest(write_conf(tmp_path, INCLUDE_VOLUMES='(".*")'))
    assert "not in INCLUDE_VOLUMES" in result.stdout, result.stdout
    assert inbox_files(tmp_path) == []


def test_a_name_with_a_space_is_one_pattern(tmp_path: Path) -> None:
    """**空白を含むボリューム名が 1 パターンとして扱われること**（§5.4）。

    空白区切りの文字列にすると `"Macintosh HD"` が 2 つのパターンへ分解され、
    **除外が働かない。**配列でなければならない理由がこれである。
    """
    root = build_fake_volumes(tmp_path / "volumes")
    (root / DEVICE_ID).rename(root / "My Device")
    age_files(root)

    excluded = run_ingest(write_conf(tmp_path, EXCLUDE_VOLUMES='("My Device")'))
    assert "in EXCLUDE_VOLUMES" in excluded.stdout, excluded.stdout
    assert inbox_files(tmp_path) == []

    shutil.rmtree(tmp_path / "home", ignore_errors=True)
    passed = run_ingest(write_conf(tmp_path, EXCLUDE_VOLUMES='("My")'))
    assert "device_detected name=My Device" in passed.stdout, passed.stdout


def test_symlinked_volume_root_is_skipped(tmp_path: Path, volumes: Path) -> None:
    """**ボリュームルートが symlink ならスキップ**（§5.4 規則 3）。

    macOS には `/Volumes/Macintosh HD -> /` が実在し、辿ると起動ディスク全体が
    走査対象になる（§22 R-19。`docs/POC.md` §5.1 で実証済み）。
    """
    (volumes / "Escape").symlink_to(volumes / DEVICE_ID)
    result = run_ingest(write_conf(tmp_path, EXCLUDE_VOLUMES="()"))
    assert "name=Escape reason=symlink" in result.stdout, result.stdout
    assert f"device_detected name={DEVICE_ID}" in result.stdout


def test_directory_without_recordings_is_not_a_device(tmp_path: Path, volumes: Path) -> None:
    """録音を持たないディレクトリは DJI と判定しない（§5.4 規則 5）。"""
    (volumes / "Backup SSD" / "Documents").mkdir(parents=True)
    result = run_ingest(write_conf(tmp_path, EXCLUDE_VOLUMES="()"))
    assert "name=Backup SSD reason=no DJI recordings" in result.stdout, result.stdout


def test_scan_depth_is_bounded(tmp_path: Path, volumes: Path) -> None:
    """`MAX_SCAN_DEPTH` より深い録音は見ない（§5.4 規則 6）。"""
    deep = volumes / DEVICE_ID / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    buried = deep / "TX00_MIC009_20260101_000000_orig.wav"
    buried.write_bytes(b"deep")
    os.utime(buried, (OLD_MTIME, OLD_MTIME))
    run_ingest(write_conf(tmp_path, MAX_SCAN_DEPTH="2"))
    assert not any("MIC009" in name for name in inbox_files(tmp_path))


def test_multiple_devices(tmp_path: Path, volumes: Path) -> None:
    build_fake_volumes(tmp_path / "second")
    shutil.move(str(tmp_path / "second" / DEVICE_ID), str(volumes / "DJIMIC4"))
    age_files(volumes)
    run_ingest(write_conf(tmp_path))
    inventory = read_json(tmp_path / "home" / "state" / "inventory.json")
    devices = inventory["devices"]
    assert isinstance(devices, dict)
    assert sorted(devices) == ["DJIMIC3", "DJIMIC4"]


# --- §10.3 安定性判定 ---------------------------------------------------


def test_unstable_file_is_deferred(tmp_path: Path, volumes: Path) -> None:
    """書き込み中のファイルはコピーせず、次回に回す（§10.3。`FILE_NOT_STABLE` は保留）。"""
    target = next(volumes.rglob("*_orig.wav"))
    os.utime(target, None)  # mtime を「いま」にして即断の経路から外す
    result = run_ingest(write_conf(tmp_path, STABILITY_CHECKS="2", STABILITY_INTERVAL_SECONDS="1"))
    assert result.returncode == 0, result.stderr
    # mtime が新しいので即断されず、2 回の一致で安定と判定される（内容は変わらないため）
    assert "stability_pending" in result.stdout, result.stdout


def test_waiting_time_does_not_scale_with_file_count(tmp_path: Path, volumes: Path) -> None:
    """**待機時間がファイル数に比例しないこと**（§10.3 の要点）。

    ファイル単位で直列に待つと 64 ファイルで約 6 分止まる。バッチで待つ設計である。
    """
    folder = volumes / DEVICE_ID / "TX_MIC001_20260914_100000"
    folder.mkdir(parents=True)
    for index in range(24):
        extra = folder / f"TX00_MIC{index:03d}_20260914_100000_orig.wav"
        extra.write_bytes(b"x" * 16)
        os.utime(extra, None)  # すべて「いま」= 即断されない

    started = time.monotonic()
    result = run_ingest(write_conf(tmp_path, STABILITY_CHECKS="2", STABILITY_INTERVAL_SECONDS="1"))
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    # 待機は 1 秒 × 2 回。ファイル 25 件を直列に待てば 50 秒かかる
    assert elapsed < 20, f"待機がファイル数に比例している: {elapsed:.1f}s\n{result.stdout}"


# --- 排他（§10.1 手順 1） -----------------------------------------------


def test_a_live_lock_holder_makes_it_exit_quietly(tmp_path: Path, volumes: Path) -> None:
    conf = write_conf(tmp_path)
    lock = tmp_path / "home" / "state" / ".ingest.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = run_ingest(conf)
    assert result.returncode == 0, result.stderr
    assert "already_running" in result.stdout
    assert inbox_files(tmp_path) == []


def test_a_dead_lock_holder_is_stolen(tmp_path: Path, volumes: Path) -> None:
    """**死んだ PID のロックは奪う。**

    奪う規則が無いと、ingest が途中で落ちた時点から**永久に 1 本も取り込まれない**
    （§22 R-23。症状は「何も起きない」なので気づけない）。
    """
    conf = write_conf(tmp_path)
    lock = tmp_path / "home" / "state" / ".ingest.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text("999999\n", encoding="utf-8")
    result = run_ingest(conf)
    assert result.returncode == 0, result.stderr
    assert "lock_stolen" in result.stderr, result.stderr
    assert inbox_files(tmp_path), "ロックを奪ったのに取り込んでいない"


def test_lock_is_released_on_exit(tmp_path: Path, volumes: Path) -> None:
    conf = write_conf(tmp_path)
    run_ingest(conf)
    assert not (tmp_path / "home" / "state" / ".ingest.lock").exists()


# --- ログ（§10.1 手順 2） -----------------------------------------------


def test_log_is_rotated(tmp_path: Path, volumes: Path) -> None:
    conf = write_conf(tmp_path)
    log = tmp_path / "home" / "log" / "ingest.log"
    log.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    result = run_ingest(conf)
    assert "log_rotated" in result.stdout, result.stdout
    assert (tmp_path / "home" / "log" / "ingest.log.1").is_file()


def test_small_log_is_not_rotated(tmp_path: Path, volumes: Path) -> None:
    conf = write_conf(tmp_path)
    (tmp_path / "home" / "log" / "ingest.log").write_bytes(b"x" * 1024)
    result = run_ingest(conf)
    assert "log_rotated" not in result.stdout
    assert not (tmp_path / "home" / "log" / "ingest.log.1").exists()


def test_heartbeat_is_valid_json_even_with_a_broken_boolean(tmp_path: Path, volumes: Path) -> None:
    """**検証 6 に違反した値でも壊れた JSON を書かないこと。**

    `DELETE_SOURCE_AUDIO=yes` をそのまま JSON の真偽値として書くと文書が壊れ、
    **まさに `config_error` を読ませたい場面でコンテナが読めなくなる。**
    §7.5 の「不明は安全側へ倒す」に従い、`true` 以外は `false` にする
    （削除を許可しない側）。
    """
    run_ingest(write_conf(tmp_path, DELETE_SOURCE_AUDIO="yes"))
    heartbeat = read_json(tmp_path / "home" / "state" / "heartbeat.json")
    assert heartbeat["delete_source_audio"] is False
    error = heartbeat["config_error"]
    assert isinstance(error, str)
    assert error.startswith("6:")
