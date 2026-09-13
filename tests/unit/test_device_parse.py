"""`voicedock.device` のファイル名パーサが SPEC §5.2 / §20.1 と一致することを固定する。"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.spec_sync import spec_section_code
from voicedock import device
from voicedock.device import (
    DENOISED,
    ORIG,
    RECORDING_FILENAME_RE,
    RECORDING_FOLDER_RE,
    is_recording_folder,
    parse_filename,
)

JST = ZoneInfo("Asia/Tokyo")


# --- SPEC 整合 -----------------------------------------------------------


def test_patterns_match_spec() -> None:
    """§5.2 の 2 つの正規表現が SPEC の写しであること。

    **パターンは削除時の同定にも使う**（§14.1 / reaper 検証 7・8）。SPEC と実装が
    食い違うと、**ノートに載った Part とデバイス上のファイルの対応が崩れる。**
    """
    block = spec_section_code("5.2", "python")
    namespace: dict[str, object] = {"re": re}
    exec(compile(block, "<spec 5.2>", "exec"), namespace)  # noqa: S102
    expected_filename = namespace["RECORDING_FILENAME_RE"]
    expected_folder = namespace["RECORDING_FOLDER_RE"]
    assert isinstance(expected_filename, re.Pattern)
    assert isinstance(expected_folder, re.Pattern)
    assert RECORDING_FILENAME_RE.pattern == expected_filename.pattern
    assert RECORDING_FOLDER_RE.pattern == expected_folder.pattern


# --- §20.1 の境界値 -----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tx", "mic", "variant"),
    [
        ("TX01_MIC002_20260829_071204.wav", "TX01", 2, DENOISED),
        ("TX01_MIC002_20260829_071204_orig.wav", "TX01", 2, ORIG),
        ("TX01_MIC002_20260829_071204.WAV", "TX01", 2, DENOISED),
        ("TX01_MIC002_20260829_071204_orig.WAV", "TX01", 2, ORIG),
        # 境界値（§20.1）
        ("TX00_MIC000_20260829_071204_orig.wav", "TX00", 0, ORIG),
        ("TX99_MIC999_20260829_071204_orig.wav", "TX99", 999, ORIG),
        # 実機で実在した形（docs/POC.md §6.2 は TX00 だった）
        ("TX00_MIC001_20260912_120950_orig.wav", "TX00", 1, ORIG),
    ],
)
def test_parses_valid_names(name: str, tx: str, mic: int, variant: str) -> None:
    parsed = parse_filename(name, tz=JST)
    assert parsed is not None, name
    assert parsed.transmitter_id == tx
    assert parsed.mic_index == mic
    assert parsed.variant == variant


def test_started_at_is_timezone_aware() -> None:
    """`started_at` に `config.timezone` が付くこと（§5.2）。

    naive な datetime を返すと §10.4 のセッション分組（日単位）が
    **サーバのローカル時刻で日境界を切ることになり、日付がずれる。**
    """
    parsed = parse_filename("TX01_MIC002_20260829_071204_orig.wav", tz=JST)
    assert parsed is not None
    assert parsed.started_at.tzinfo is not None
    assert parsed.started_at == datetime(2026, 8, 29, 7, 12, 4, tzinfo=JST)


def test_timezone_is_applied_not_converted() -> None:
    """ファイル名の時刻は**現地時刻として解釈する**（変換しない）。

    DJI はローカル時刻でファイル名を付ける。UTC と解釈してから変換すると 9 時間ずれる。
    """
    jst = parse_filename("TX01_MIC002_20260829_071204_orig.wav", tz=JST)
    utc = parse_filename("TX01_MIC002_20260829_071204_orig.wav", tz=ZoneInfo("UTC"))
    assert jst is not None
    assert utc is not None
    assert (jst.started_at.hour, jst.started_at.minute) == (7, 12)
    assert (utc.started_at.hour, utc.started_at.minute) == (7, 12)
    assert jst.started_at != utc.started_at, "同じ現地時刻で別の瞬間を指すこと"


@pytest.mark.parametrize(
    "name",
    [
        "",
        "NOTES.txt",
        "TX1_MIC002_20260829_071204.wav",  # tx が 1 桁
        "TX001_MIC002_20260829_071204.wav",  # tx が 3 桁
        "TX01_MIC02_20260829_071204.wav",  # mic が 2 桁
        "TX01_MIC0002_20260829_071204.wav",  # mic が 4 桁
        "TX01_MIC002_2026829_071204.wav",  # date が 7 桁
        "TX01_MIC002_20260829_07120.wav",  # time が 5 桁
        "TX01_MIC002_20260829_071204.mp3",  # 拡張子違い
        "TX01_MIC002_20260829_071204_ORIG.wav",  # `_orig` の大文字（規定に無い）
        "TX01_MIC002_20260829_071204_orig_orig.wav",
        "._TX01_MIC002_20260829_071204_orig.wav",  # AppleDouble
        "prefix_TX01_MIC002_20260829_071204.wav",
        "TX01_MIC002_20260829_071204.wav.partial",  # Helper のコピー中
        "TX01_MIC002_20260829_071204_orig.wav.meta.json",
    ],
)
def test_rejects_invalid_names(name: str) -> None:
    """パース失敗は `None`。**例外にしない**（§5.2）。

    デバイスには DJI 以外のファイルが必ず混ざる（`docs/POC.md` §6.3）。
    """
    assert parse_filename(name, tz=JST) is None


@pytest.mark.parametrize(
    "name",
    [
        "TX01_MIC002_20260230_071204_orig.wav",  # 2 月 30 日
        "TX01_MIC002_20261301_071204_orig.wav",  # 13 月
        "TX01_MIC002_20260829_251204_orig.wav",  # 25 時
        "TX01_MIC002_20260829_076104_orig.wav",  # 61 分
        "TX01_MIC002_00000000_000000_orig.wav",  # 0 年 0 月 0 日
    ],
)
def test_impossible_datetimes_are_rejected_without_raising(name: str) -> None:
    """**桁は合っていても存在しない日時は `None` にする。**

    正規表現は桁数しか見ないので、`datetime` の構築で初めて分かる。ここで
    `ValueError` が漏れると、**1 個の壊れたファイル名でその日の走査全体が止まる**
    （§1.3 の優先順位に反する）。
    """
    assert parse_filename(name, tz=JST) is None


def test_leap_day_is_accepted() -> None:
    """存在する閏日は通ること（2028 年は閏年）。"""
    assert parse_filename("TX01_MIC002_20280229_071204_orig.wav", tz=JST) is not None


# --- フォルダ名 ---------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["TX_MIC001_20260829_071201", "TX_MIC000_20260101_000000", "TX_MIC999_20991231_235959"],
)
def test_accepts_valid_folders(name: str) -> None:
    assert is_recording_folder(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "TX_MIC01_20260829_071201",
        "TX01_MIC001_20260829_071201",  # ファイル名の形
        "TX_MIC001_20260829",
        ".Trashes",
        ".Spotlight-V100",
        "TX_MIC001_20260829_071201_extra",
    ],
)
def test_rejects_invalid_folders(name: str) -> None:
    assert not is_recording_folder(name)


# --- §5.3 `_orig` 固定 --------------------------------------------------


def test_orig_and_denoised_are_distinguished() -> None:
    """`_orig` の有無だけが Variant を決める（§5.2 / §5.3）。

    v5.0 以降 **`denoised` は取り込まない**が、パーサは両方を見分けられなければ
    「取り込まない」判断ができない。
    """
    orig = parse_filename("TX01_MIC002_20260829_071204_orig.wav", tz=JST)
    denoised = parse_filename("TX01_MIC002_20260829_071204.wav", tz=JST)
    assert orig is not None
    assert denoised is not None
    assert orig.variant is ORIG
    assert denoised.variant is DENOISED
    # `_orig` 以外は同じ Part を指す（§5.3 の「`_orig` を除いたファイル名が一致」）
    assert (orig.transmitter_id, orig.mic_index, orig.started_at) == (
        denoised.transmitter_id,
        denoised.mic_index,
        denoised.started_at,
    )


def test_variant_literals_are_the_only_two() -> None:
    """`ORIG` / `DENOISED` が §5.2 の `Literal` と一致すること。"""
    assert {ORIG, DENOISED} == {"orig", "denoised"}
    assert device.Variant.__args__ == (ORIG, DENOISED)  # type: ignore[attr-defined]
