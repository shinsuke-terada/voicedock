"""`voicedock.device` の `/inbox` 走査と `inventory.json` の読み取りを固定する。

**コンテナはデバイスに到達しない**（§14.4 N-3）。見るのは Helper が書いた
`/inbox` と `/state` だけである。ここでテストするのは「Helper が置いたものを
正しく読むか」であり、**Helper 自身の挙動は `test_helper_ingest.py`** が見る。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.fake_tree import (
    DEFAULT_RECORDINGS,
    DENOISED,
    DEVICE_ID,
    NOISE_APPLEDOUBLE,
    NOISE_UNPARSABLE,
    ORIG,
    ORPHAN_FILE,
    ORPHAN_FOLDER,
    build_fake_inbox,
)
from voicedock import paths
from voicedock.device import (
    DeviceInventory,
    list_inbox_parts,
    read_inventory,
)
from voicedock.paths import DevicePath

JST = ZoneInfo("Asia/Tokyo")


def scan(inbox: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """`(partkey の一覧, (event, reason) の一覧)` を返す。"""
    result = list_inbox_parts(inbox, tz=JST)
    return (
        sorted(part.partkey for part in result.parts),
        sorted((entry.event, entry.reason) for entry in result.skipped),
    )


# --- §10.2 の 4 状態 ----------------------------------------------------


def test_returns_only_parts_with_both_files(fake_inbox: Path) -> None:
    """`.wav` と `.wav.meta.json` が揃ったものだけを返す（§10.2 の表）。"""
    result = list_inbox_parts(fake_inbox, tz=JST)
    assert result.parts
    for part in result.parts:
        assert part.source.inbox_path is not None
        assert Path(part.source.inbox_path).is_file()
        assert Path(str(part.source.inbox_path) + ".meta.json").is_file()


def test_orphan_wav_is_not_a_candidate(fake_inbox: Path) -> None:
    """**`.wav` だけがあるものは候補にしない**（§10.2）。

    コピー中か Helper が落ちた痕跡である。次回の Helper 起動で置き直される
    （墓標が無いので再コピーの対象になる）。
    """
    orphan = fake_inbox / DEVICE_ID / ORPHAN_FOLDER / ORPHAN_FILE
    assert orphan.is_file(), "fixture に .meta.json なしの .wav が無い"
    keys, _ = scan(fake_inbox)
    assert not any(ORPHAN_FOLDER in key for key in keys)


def test_tombstone_only_is_not_a_candidate(fake_inbox: Path) -> None:
    """**`.meta.json` だけがあるものは候補にしない**（§10.2 の墓標）。

    コンテナが変換後に `.wav` を消した状態である（§10.5）。**ログも出さない** —
    正常な「処理済み」であり、異常ではない。
    """
    before, skipped_before = scan(fake_inbox)
    assert before
    target = Path(str(list_inbox_parts(fake_inbox, tz=JST).parts[0].source.inbox_path))
    target.unlink()
    after, skipped_after = scan(fake_inbox)
    assert len(after) == len(before) - 1
    # **ログが 1 行も増えないこと。**墓標だけの状態は正常な「処理済み」であり、
    # 異常ではない（fixture には denoised があるので variant_not_orig は元から出る）
    assert skipped_after == skipped_before, (
        f"墓標だけの状態でログが増えている: {set(skipped_after) - set(skipped_before)}"
    )


def test_missing_both_yields_nothing(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    (inbox / DEVICE_ID / "TX_MIC001_20260912_120950").mkdir(parents=True)
    keys, skipped = scan(inbox)
    assert keys == []
    assert skipped == []


def test_empty_inbox_is_not_an_error(tmp_path: Path) -> None:
    """存在しない inbox でも例外にしない（初回起動時は空である）。"""
    result = list_inbox_parts(tmp_path / "nope", tz=JST)
    assert result.parts == ()
    assert result.skipped == ()


# --- §5.3 `_orig` 固定 --------------------------------------------------


def test_only_orig_is_a_candidate(fake_inbox: Path) -> None:
    """`_orig` でないものは `part_skipped reason=variant_not_orig`（§5.3）。

    **v5.0 で `_orig` 固定になった。**Helper は denoised をコピーしない設計だが、
    ここでも弾くのは**古い Helper が置いた denoised を黙って取り込まない**ためである。
    """
    result = list_inbox_parts(fake_inbox, tz=JST)
    for part in result.parts:
        assert str(part.source.relpath).endswith("_orig.wav"), part.partkey
    reasons = [e.reason for e in result.skipped if e.event == "part_skipped"]
    assert "variant_not_orig" in reasons, result.skipped


def test_denoised_only_recording_yields_no_part(fake_inbox: Path) -> None:
    """denoised しか無い録音は Part にならない（§5.3）。"""
    denoised_only = [r for r in DEFAULT_RECORDINGS if ORIG not in r.variants]
    assert denoised_only, "fixture に denoised のみの録音が無い"
    keys, _ = scan(fake_inbox)
    for recording in denoised_only:
        assert not any(recording.folder in key for key in keys)


# --- §10.2 のログ規則 ---------------------------------------------------


def test_dotfiles_are_silently_ignored(fake_inbox: Path) -> None:
    """**`.` 始まりにはログを 1 行も出さない**（§10.2）。

    macOS は FAT32 / exFAT へ書き込むたびに `._<名前>`（AppleDouble）を作り、
    `.Spotlight-V100` / `.fseventsd` / `.Trashes` も作られる（`docs/POC.md` §6.3）。
    素直に警告すると **Part ごとにログが埋まる。**
    """
    appledouble = fake_inbox / DEVICE_ID / DEFAULT_RECORDINGS[0].folder / NOISE_APPLEDOUBLE
    appledouble.parent.mkdir(parents=True, exist_ok=True)
    appledouble.write_bytes(b"x")
    (appledouble.parent / (NOISE_APPLEDOUBLE + ".meta.json")).write_text("{}", encoding="utf-8")
    _keys, skipped = scan(fake_inbox)
    assert not any("._" in relpath for relpath, _ in [(e[0], e[1]) for e in skipped])
    result = list_inbox_parts(fake_inbox, tz=JST)
    assert not any(entry.relpath.rsplit("/", 1)[-1].startswith(".") for entry in result.skipped)


def test_unparsable_filename_is_reported(tmp_path: Path) -> None:
    """パース失敗には `unparsable_filename` を出す（§5.2）。"""
    folder = tmp_path / "inbox" / DEVICE_ID / "TX_MIC001_20260912_120950"
    folder.mkdir(parents=True)
    (folder / NOISE_UNPARSABLE).write_text("memo", encoding="utf-8")
    (folder / (NOISE_UNPARSABLE + ".meta.json")).write_text("{}", encoding="utf-8")
    _keys, skipped = scan(tmp_path / "inbox")
    assert ("unparsable_filename", "filename") in skipped


# --- partkey（§8.1） ----------------------------------------------------


def test_partkey_comes_from_paths_helper(fake_inbox: Path) -> None:
    """`partkey` が `paths.partkey_for()` の結果と一致すること（§8.1 / §8.5）。

    **文字列を連結して作ってはならない。**算出規則の変更が §8.5 の唯一の禁則であり、
    連結した箇所があると `test_partkey_is_pinned` が守れなくなる。
    """
    result = list_inbox_parts(fake_inbox, tz=JST)
    assert result.parts
    for part in result.parts:
        assert part.partkey == paths.partkey_for(part.device_id, part.source.relpath)
        assert paths.device_id_of(part.partkey) == DEVICE_ID


def test_meta_relpath_is_used_not_the_inbox_path(fake_inbox: Path) -> None:
    """**`relpath` は `.meta.json` の値を使う**（inbox のパスから組み立てない）。

    `relpath` はデバイスのボリュームルートからの相対パスであり（§14.1.1）、
    inbox の階層と一致する保証は仕様のどこにも無い。**削除要求に載る値**なので、
    Helper が記録した値をそのまま運ぶ。
    """
    result = list_inbox_parts(fake_inbox, tz=JST)
    for part in result.parts:
        meta = json.loads(
            Path(str(part.source.inbox_path) + ".meta.json").read_text(encoding="utf-8")
        )
        assert str(part.source.relpath) == meta["relpath"]
        assert part.source.sha256_helper == meta["sha256"]


def test_unsafe_relpath_in_meta_is_rejected(tmp_path: Path) -> None:
    """`.meta.json` の `relpath` が危険なら候補にしない（§14.4 N-17 / §8.5）。

    **壊れた鍵をノートへ載せてしまうと、あとから直す手段が無い。**
    """
    folder = tmp_path / "inbox" / DEVICE_ID / "TX_MIC001_20260912_120950"
    folder.mkdir(parents=True)
    name = "TX00_MIC001_20260912_120950_orig.wav"
    (folder / name).write_bytes(b"x")
    (folder / (name + ".meta.json")).write_text(
        json.dumps({"relpath": "../escape.wav", "sha256": "x"}), encoding="utf-8"
    )
    _keys, skipped = scan(tmp_path / "inbox")
    assert ("part_skipped", "meta_unsafe_relpath") in skipped


def test_broken_meta_is_skipped_not_raised(tmp_path: Path) -> None:
    """壊れた `.meta.json` は当該エントリだけスキップする（§1.3）。

    **1 個の不良ファイルでその日の記録全体を止めない。**
    """
    folder = tmp_path / "inbox" / DEVICE_ID / "TX_MIC001_20260912_120950"
    folder.mkdir(parents=True)
    for index, body in enumerate(["{ broken", "[]", ""]):
        name = f"TX00_MIC00{index}_20260912_120950_orig.wav"
        (folder / name).write_bytes(b"x")
        (folder / (name + ".meta.json")).write_text(body, encoding="utf-8")
    good = "TX00_MIC009_20260912_120950_orig.wav"
    (folder / good).write_bytes(b"x")
    (folder / (good + ".meta.json")).write_text(
        json.dumps({"relpath": f"TX_MIC001_20260912_120950/{good}", "sha256": "x"}),
        encoding="utf-8",
    )
    keys, skipped = scan(tmp_path / "inbox")
    assert len(keys) == 1, "壊れた meta で全体が止まっている"
    assert skipped.count(("part_skipped", "meta_unreadable")) == 3


def test_parsed_fields_come_from_the_filename(fake_inbox: Path) -> None:
    result = list_inbox_parts(fake_inbox, tz=JST)
    part = next(p for p in result.parts if p.source_folder == DEFAULT_RECORDINGS[0].folder)
    assert part.transmitter_id == "TX00"
    assert part.mic_index == 1
    assert part.started_at == datetime(2026, 9, 12, 12, 9, 50, tzinfo=JST)
    assert part.device_id == DEVICE_ID


def test_scan_is_deterministic(fake_inbox: Path) -> None:
    """出力が決定的であること。

    `scandir` の順序は環境依存である。揺れると §10.5 の「古い順に 1 件ずつ直列」の
    再現性が失われ、テストも差分も読めなくなる。
    """
    first = [p.partkey for p in list_inbox_parts(fake_inbox, tz=JST).parts]
    second = [p.partkey for p in list_inbox_parts(fake_inbox, tz=JST).parts]
    assert first == second
    assert first == sorted(first)


def test_recording_directly_under_the_device_is_found(tmp_path: Path) -> None:
    """デバイス直下の録音も見る（§10.2 の「および device 直下」）。"""
    device_dir = tmp_path / "inbox" / DEVICE_ID
    device_dir.mkdir(parents=True)
    name = "TX00_MIC001_20260912_120950_orig.wav"
    (device_dir / name).write_bytes(b"x")
    (device_dir / (name + ".meta.json")).write_text(
        json.dumps({"relpath": name, "sha256": "x"}), encoding="utf-8"
    )
    result = list_inbox_parts(tmp_path / "inbox", tz=JST)
    assert len(result.parts) == 1
    assert result.parts[0].source_folder == ""


# --- §7.5 inventory.json ------------------------------------------------


def write_inventory(state: Path, document: object) -> Path:
    state.mkdir(parents=True, exist_ok=True)
    path = state / "inventory.json"
    path.write_text(
        document if isinstance(document, str) else json.dumps(document), encoding="utf-8"
    )
    return path


def test_reads_the_spec_shape(tmp_path: Path) -> None:
    write_inventory(
        tmp_path / "state",
        {
            "schema": 1,
            "generated_at": "2026-08-30T07:00:12+09:00",
            "mount_readonly": True,
            "devices": {DEVICE_ID: ["TX_MIC001_20260829_071201/TX01_MIC002_x_orig.wav"]},
        },
    )
    inventory = read_inventory(tmp_path / "state")
    assert inventory is not None
    assert inventory.mount_readonly is True
    assert inventory.generated_at == datetime(2026, 8, 30, 7, 0, 12, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert inventory.is_connected(DEVICE_ID)
    assert not inventory.is_connected("OTHER")
    assert inventory.contains(
        DEVICE_ID, DevicePath(PurePosixPath("TX_MIC001_20260829_071201/TX01_MIC002_x_orig.wav"))
    )
    assert not inventory.contains(DEVICE_ID, DevicePath(PurePosixPath("nope.wav")))
    assert not inventory.is_empty


@pytest.mark.parametrize(
    "document",
    ["{ broken", "[]", '"a string"', "null", "", '{"devices": "not a dict"}'],
)
def test_broken_inventory_never_raises(tmp_path: Path, document: str) -> None:
    """**例外を投げない**（§7.5）。Helper の書き込み途中を読む可能性がある。

    読めないときは「不明」として扱い、**不明は常に安全側へ倒す**
    （＝接続していない = 削除要求を書かない）。
    """
    write_inventory(tmp_path / "state", document)
    inventory = read_inventory(tmp_path / "state")
    if inventory is None:
        return
    assert not inventory.is_connected(DEVICE_ID)
    assert inventory.is_empty


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_inventory(tmp_path / "nope") is None


def test_missing_mount_readonly_is_unknown(tmp_path: Path) -> None:
    """`mount_readonly` が無い / 型が違うときは `None`（不明）にすること。

    **`False` で埋めてはならない。**「不明」と「読み取り専用ではない」を
    区別できなくなる（§7.5。reaper 検証 2 が読む値である）。
    """
    write_inventory(tmp_path / "state", {"devices": {}})
    inventory = read_inventory(tmp_path / "state")
    assert inventory is not None
    assert inventory.mount_readonly is None

    write_inventory(tmp_path / "state", {"mount_readonly": "true", "devices": {}})
    inventory = read_inventory(tmp_path / "state")
    assert inventory is not None
    assert inventory.mount_readonly is None


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    """未知のキーを無視すること（§7.5）。

    `config.yaml` が未知キーを拒否する（V-1）のと非対称だが、`config.yaml` は人が
    書くのでタイポが問題になり、`inventory.json` は Helper が書くので
    **フィールドが増えてもコンテナが壊れないこと**が優先される。
    """
    write_inventory(
        tmp_path / "state",
        {"schema": 99, "devices": {DEVICE_ID: []}, "future_field": {"x": 1}},
    )
    inventory = read_inventory(tmp_path / "state")
    assert inventory is not None
    assert inventory.is_connected(DEVICE_ID)


def test_an_empty_inventory_is_empty() -> None:
    inventory = DeviceInventory(generated_at=None, mount_readonly=None, devices={})
    assert inventory.is_empty
    assert not inventory.is_connected(DEVICE_ID)
    assert not inventory.contains(DEVICE_ID, DevicePath(PurePosixPath("x.wav")))


def test_reads_what_the_helper_actually_wrote(tmp_path: Path) -> None:
    """**#53 の Helper が実際に書いた `inventory.json` を読めること。**

    形式は §7.5 で定義したが、**書く側（bash）と読む側（Python）が別実装**である。
    `test_helper_ingest.py` が SPEC との一致を見ているので、ここでは
    「Helper の出力をそのまま食わせても読める」ことだけを確認する。
    """
    write_inventory(
        tmp_path / "state",
        '{\n  "schema": 1,\n  "generated_at": "2026-09-13T02:17:40+09:00",\n'
        '  "mount_readonly": false,\n  "devices": {\n'
        f'    "{DEVICE_ID}": [\n'
        '      "TX_MIC001_20260912_120950/TX00_MIC001_20260912_120950.wav",\n'
        '      "TX_MIC001_20260912_120950/TX00_MIC001_20260912_120950_orig.wav"\n'
        "    ]\n  }\n}\n",
    )
    inventory = read_inventory(tmp_path / "state")
    assert inventory is not None
    assert inventory.mount_readonly is False
    assert inventory.is_connected(DEVICE_ID)
    assert len(inventory.devices[DEVICE_ID]) == 2


def test_naive_generated_at_is_kept_as_is(tmp_path: Path) -> None:
    """オフセットの無い `generated_at` でも落ちないこと。

    Helper は必ずオフセットを付けるが（§7.5）、**壊れた入力で例外を投げない**
    という方針をここでも守る。
    """
    write_inventory(tmp_path / "state", {"generated_at": "not-a-date", "devices": {}})
    inventory = read_inventory(tmp_path / "state")
    assert inventory is not None
    assert inventory.generated_at is None


def test_utc_offset_is_preserved(tmp_path: Path) -> None:
    write_inventory(
        tmp_path / "state", {"generated_at": "2026-09-13T00:00:00+00:00", "devices": {}}
    )
    inventory = read_inventory(tmp_path / "state")
    assert inventory is not None
    assert inventory.generated_at == datetime(2026, 9, 13, tzinfo=UTC)


def test_denoised_appears_in_inventory_but_not_in_parts(fake_inbox: Path) -> None:
    """inventory は全件、Part 候補は `_orig` だけ（§7.5 / §5.3）。

    **範囲が違う**ことを 1 つのテストで並べて示す。inventory は
    「denoised がデバイスに溜まっているか」を答える唯一の場所である。
    """
    both = next(r for r in DEFAULT_RECORDINGS if ORIG in r.variants and DENOISED in r.variants)
    keys, _ = scan(fake_inbox)
    matching = [key for key in keys if both.folder in key]
    assert len(matching) == 1, f"denoised も Part になっている: {matching}"
    assert matching[0].endswith("_orig.wav")


def test_build_fake_inbox_is_the_helper_contract(tmp_path: Path) -> None:
    """fixture が `.meta.json` の形を守っていること（#53 との契約）。"""
    inbox = build_fake_inbox(tmp_path / "inbox", with_noise=False)
    result = list_inbox_parts(inbox, tz=JST)
    assert len(result.parts) == sum(1 for r in DEFAULT_RECORDINGS if ORIG in r.variants)
