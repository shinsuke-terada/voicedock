"""`voicedock.discover.discover` が SPEC §10.2 / §9.3 Part 表のとおりに登録することを固定する。

**二重処理防止がこのモジュールの存在理由である。**`launchd` は 300 秒ごとに Helper を
起動し、inbox には 1 日 32 件が残りうる（§10.1 / §5.1）。同じ Part を 2 回登録すると
**同じ音声を 2 回文字起こしし、2 本の Raw ノートを書き、§14.1 の削除条件が
どちらのノートを根拠にしているのか分からなくなる。**
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.fake_tree import (
    DEFAULT_RECORDINGS,
    DEVICE_ID,
    ORIG,
    FakeRecording,
    build_fake_inbox,
)
from tests.fixtures.make_wav import Content
from voicedock import audio, discover
from voicedock.config import Config
from voicedock.db import Database, EntityType
from voicedock.discover import ALREADY_KNOWN, DiscoverResult
from voicedock.discover import discover as run_discover
from voicedock.log import EVENTS, Logger
from voicedock.states import PartStatus

ORIG_RECORDINGS = tuple(r for r in DEFAULT_RECORDINGS if ORIG in r.variants)
"""`_orig` を持つ録音だけが Part になる（§5.3）。fixture では 3 件中 2 件。"""


class SpyProber:
    """`probe()` の差し替え。**何回・どのファイルに対して起動されたかを数える。**"""

    def __init__(self, result: audio.ProbeResult | None = None) -> None:
        self.calls: list[Path] = []
        self._result = result if result is not None else success()

    def __call__(self, path: Path) -> audio.ProbeResult:
        self.calls.append(path)
        return self._result


def success(duration: float | None = 1.5) -> audio.ProbeResult:
    return audio.ProbeResult(
        format=audio.AudioFormat(
            duration_seconds=duration,
            sample_rate=48000,
            channels=2,
            sample_fmt="s32",
            codec_name="pcm_s24le",
        ),
        attempts=1,
        error=None,
    )


def failure(attempts: int = 3) -> audio.ProbeResult:
    return audio.ProbeResult(format=None, attempts=attempts, error="終了コード 1: boom")


@pytest.fixture
def logger() -> tuple[Logger, io.StringIO]:
    stream = io.StringIO()
    return Logger(level="DEBUG", fmt="text", stream=stream), stream


@pytest.fixture
def cfg(make_config: Callable[..., Config], fake_inbox: Path) -> Config:
    return make_config({"import": {"inbox_root": str(fake_inbox)}})


@pytest.fixture
def go(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], frozen_now: datetime
) -> Callable[..., DiscoverResult]:
    def call(**kwargs: Any) -> DiscoverResult:
        kwargs.setdefault("prober", SpyProber())
        return run_discover(database, cfg=cfg, log=logger[0], now=frozen_now, **kwargs)

    return call


def lines(stream: io.StringIO) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if line]


# --- 新規検出（§9.3 Part 表の 1 行目） ----------------------------------


def test_registers_every_orig_part_as_discovered(
    go: Callable[..., DiscoverResult], database: Database
) -> None:
    result = go()
    assert len(result.registered) == len(ORIG_RECORDINGS)
    assert database.counts().recordings == len(ORIG_RECORDINGS)
    for partkey in result.registered:
        row = database.get_recording(partkey)
        assert row is not None
        assert row.status == PartStatus.DISCOVERED


def test_fills_every_column_the_scan_can_know(
    go: Callable[..., DiscoverResult], database: Database, frozen_now: datetime
) -> None:
    """§8.2 のうち走査時点で分かる列がすべて埋まること。"""
    go(prober=SpyProber(success(90.0)))
    recording = ORIG_RECORDINGS[0]
    row = database.get_recording(recording.partkey())
    assert row is not None

    assert row.device_id == DEVICE_ID
    assert row.source_folder == recording.folder
    assert row.transmitter_id == recording.stem.split("_")[0]
    assert row.mic_index == int(recording.stem.split("_")[1].removeprefix("MIC"))
    assert row.started_at.startswith("2026-09-12T12:09:50")
    assert row.source_path == recording.relpath(ORIG)
    assert row.source_size is not None and row.source_size > 0
    assert row.source_mtime is not None
    assert row.sha256_helper is not None and len(row.sha256_helper) == 64
    assert row.inbox_path is not None and row.inbox_path.endswith(recording.filename(ORIG))
    assert row.updated_at == frozen_now.isoformat(timespec="seconds")


def test_duration_and_ended_at_come_from_ffprobe(
    go: Callable[..., DiscoverResult], database: Database
) -> None:
    """`ended_at = started_at + duration`（§10.5(1)）。"""
    go(prober=SpyProber(success(90.0)))
    row = database.get_recording(ORIG_RECORDINGS[0].partkey())
    assert row is not None
    assert row.duration_seconds == pytest.approx(90.0)
    assert row.ended_at is not None
    assert row.ended_at.startswith("2026-09-12T12:11:20")


def test_the_partkey_is_the_one_device_py_built(
    go: Callable[..., DiscoverResult], database: Database
) -> None:
    """**`partkey` を組み直さない**（§8.5 の唯一の禁則）。

    `device.py` が `paths.partkey_for()` で作った値をそのまま運ぶ。ここで連結すると
    算出規則の出所が 2 つになる。
    """
    result = go()
    assert set(result.registered) == {r.partkey() for r in ORIG_RECORDINGS}
    for partkey in result.registered:
        row = database.get_recording(partkey)
        assert row is not None
        assert partkey == f"{row.device_id}/{row.source_path}"


def test_registers_in_started_at_order(
    go: Callable[..., DiscoverResult], database: Database
) -> None:
    """**`events` が時系列に読めること。**`list_inbox_parts()` はディレクトリ名順である。"""
    result = go()
    started = [database.get_recording(k).started_at for k in result.registered]  # type: ignore[union-attr]
    assert started == sorted(started)


def test_columns_owned_by_later_tickets_stay_null(
    go: Callable[..., DiscoverResult], database: Database
) -> None:
    """`sha256`（#18）と `session_key`（#17）はここでは埋めない。

    **`sha256` を先に埋めると §10.5 の「コピー検証」が意味を失う。**照合の対象は
    「Helper が算出した値」と「コンテナが変換しながら再計算した値」であり、
    走査時点でコンテナは原本を読んでいない。
    """
    go()
    row = database.get_recording(ORIG_RECORDINGS[0].partkey())
    assert row is not None
    assert row.sha256 is None
    assert row.session_key is None
    assert row.normalized_path is None
    assert row.transcript_path is None
    assert database.counts().sessions == 0


def test_the_row_creation_is_recorded_in_events(
    go: Callable[..., DiscoverResult], database: Database
) -> None:
    """**行の誕生も状態遷移である**（§8.4）。`from_status` は `NULL`。"""
    result = go()
    assert database.counts().events == len(result.registered)
    events = database.events_for(EntityType.RECORDING, result.registered[0])
    assert len(events) == 1
    assert events[0].from_status is None
    assert events[0].to_status == PartStatus.DISCOVERED


# --- 既知（§9.3 Part 表の 2 行目） --------------------------------------


def test_a_second_scan_adds_nothing(go: Callable[..., DiscoverResult], database: Database) -> None:
    first = go()
    second = go()
    assert second.registered == ()
    assert set(second.already_known) == set(first.registered)
    assert database.counts().recordings == len(ORIG_RECORDINGS)
    assert database.counts().events == len(ORIG_RECORDINGS), "events も増えない"


def test_a_second_scan_does_not_launch_ffprobe(go: Callable[..., DiscoverResult]) -> None:
    """**既知の Part には ffprobe すら起動しない**（§10.2）。

    1 日 32 件 × 300 秒ごとで、素直に毎回 probe すると**5 分ごとに 32 回の
    プロセス起動**になる。
    """
    first = SpyProber()
    go(prober=first)
    assert len(first.calls) == len(ORIG_RECORDINGS)

    second = SpyProber()
    go(prober=second)
    assert second.calls == []


def test_already_known_is_logged_as_part_skipped(
    go: Callable[..., DiscoverResult], logger: tuple[Logger, io.StringIO]
) -> None:
    """**`already_known` という名前のイベントを作らない**（v5.2→v5.3 の変更 O-1）。

    §16.4 の 28 件に無く、`log.py` は未登録の名前に `ValueError` を投げる。
    """
    first = go()
    logger[1].truncate(0)
    logger[1].seek(0)
    go()

    known = [line for line in lines(logger[1]) if ALREADY_KNOWN in line]
    assert len(known) == len(first.registered)
    for line in known:
        assert "part_skipped" in line
        assert f"reason={ALREADY_KNOWN}" in line
        assert " DEBUG " in line, "既知は DEBUG である（§10.2）"
    assert ALREADY_KNOWN not in EVENTS, "イベント名にしてはならない"


def test_a_concurrent_insert_falls_back_to_already_known(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], frozen_now: datetime
) -> None:
    """**主キー制約が二重処理防止の本体である**（§10.2）。

    事前の `SELECT` は ffprobe を省くための最適化にすぎず、`SELECT` と `INSERT` の
    間に別プロセスが入りうる。`IntegrityError` を握って `already_known` に倒す。
    """

    def race(path: Path) -> audio.ProbeResult:
        # probe の最中に「別プロセス」が同じ行を入れてしまった状況を作る
        run_discover(database, cfg=cfg, log=logger[0], now=frozen_now, prober=SpyProber())
        return success()

    result = run_discover(database, cfg=cfg, log=logger[0], now=frozen_now, prober=race)
    assert result.registered == ()
    assert len(result.already_known) == len(ORIG_RECORDINGS)
    assert database.counts().recordings == len(ORIG_RECORDINGS)


# --- ffprobe 失敗（§10.5 / §15.1） --------------------------------------


def test_a_probe_failure_still_registers_the_part(
    go: Callable[..., DiscoverResult], database: Database
) -> None:
    """**記録を残すことを優先する**（§1.3 の優先順位 1）。

    `duration_seconds = NULL` は §10.5 が明示的に支える恒久状態であり、Timeline は
    whisper のセグメント終端で代用する。
    """
    result = go(prober=SpyProber(failure()))
    assert len(result.registered) == len(ORIG_RECORDINGS)
    assert set(result.probe_failed) == set(result.registered)
    row = database.get_recording(result.registered[0])
    assert row is not None
    assert row.status == PartStatus.DISCOVERED
    assert row.duration_seconds is None
    assert row.ended_at is None


def test_a_probe_failure_does_not_set_error_code(
    go: Callable[..., DiscoverResult], database: Database
) -> None:
    """**`recordings.error_code` を設定しない**（v5.2→v5.3 の変更 O-2）。

    行は `DISCOVERED` として健全である。`error_code` を置くと §15.2 のリトライが
    **終わらない再試行対象**として拾い続ける。
    """
    result = go(prober=SpyProber(failure()))
    row = database.get_recording(result.registered[0])
    assert row is not None
    assert row.error_code is None
    assert row.error_message is None
    assert row.retry_count == 0


def test_a_probe_failure_is_logged_as_part_discovered(
    go: Callable[..., DiscoverResult], logger: tuple[Logger, io.StringIO]
) -> None:
    """**`audio_probe_failed` という名前のイベントを作らない**（v5.2→v5.3 の変更 O-2）。"""
    go(prober=SpyProber(failure()))
    warnings = [line for line in lines(logger[1]) if " WARNING " in line]
    assert warnings
    for line in warnings:
        assert "part_discovered" in line
        assert "error_code=AUDIO_PROBE_FAILED" in line
    assert "audio_probe_failed" not in EVENTS


def test_a_successful_probe_logs_the_duration(
    go: Callable[..., DiscoverResult], logger: tuple[Logger, io.StringIO]
) -> None:
    go(prober=SpyProber(success(1800.0)))
    discovered = [line for line in lines(logger[1]) if "part_discovered" in line]
    assert len(discovered) == len(ORIG_RECORDINGS)
    assert all("duration=1800.0" in line for line in discovered)
    assert all(" INFO " in line for line in discovered)


# --- 候補にならなかったもの（§10.2） ------------------------------------


def test_skipped_entries_are_reported_and_not_registered(
    go: Callable[..., DiscoverResult], database: Database
) -> None:
    """`.meta.json` の無い `.wav` と denoised は登録されない（§10.2 / §5.3）。"""
    result = go()
    reasons = {entry.reason for entry in result.skipped}
    assert "variant_not_orig" in reasons
    assert database.counts().recordings == len(ORIG_RECORDINGS)


def test_only_unparsable_filenames_are_warnings(
    go: Callable[..., DiscoverResult], logger: tuple[Logger, io.StringIO]
) -> None:
    """**`variant_not_orig` は異常ではなく日常である**（§5.3）。WARN にしない。"""
    go()
    for line in lines(logger[1]):
        if "reason=variant_not_orig" in line:
            assert " INFO " in line
        if "unparsable_filename" in line:
            assert " WARNING " in line


def test_an_empty_inbox_is_not_an_error(
    database: Database,
    make_config: Callable[..., Config],
    logger: tuple[Logger, io.StringIO],
    tmp_path: Path,
    frozen_now: datetime,
) -> None:
    empty = tmp_path / "empty-inbox"
    empty.mkdir()
    cfg = make_config({"import": {"inbox_root": str(empty)}})
    result = run_discover(database, cfg=cfg, log=logger[0], now=frozen_now, prober=SpyProber())
    assert result == DiscoverResult((), (), (), ())
    assert database.counts().recordings == 0


def test_a_new_part_appearing_later_is_registered(
    go: Callable[..., DiscoverResult], database: Database, fake_inbox: Path
) -> None:
    """**その日のうちに Part が増える**（§10.7 は 1 日 32 回の再生成を想定している）。"""
    go()
    later = FakeRecording(
        folder="TX_MIC001_20260912_180000",
        stem="TX00_MIC001_20260912_180000",
        variants=(ORIG,),
        seconds=1.0,
        content=Content.SPEECH,
    )
    build_fake_inbox(fake_inbox, recordings=(later,), with_noise=False)
    second = go()
    assert len(second.registered) == 1
    assert database.counts().recordings == len(ORIG_RECORDINGS) + 1


# --- 設定の既定 ---------------------------------------------------------


def test_uses_the_configured_inbox_root_by_default(
    database: Database, cfg: Config, logger: tuple[Logger, io.StringIO], frozen_now: datetime
) -> None:
    """`inbox_root` を渡さなければ `cfg.import_.inbox_root` を見る。"""
    result = run_discover(database, cfg=cfg, log=logger[0], now=frozen_now, prober=SpyProber())
    assert len(result.registered) == len(ORIG_RECORDINGS)


def test_probe_for_passes_the_configured_values(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`probe_for()` が設定の 3 つの値をそのまま `audio.probe()` へ運ぶこと。

    `audio.ffprobe` / `audio.ffprobe_timeout_seconds` / `retry.max_attempts`。
    """
    seen: dict[str, object] = {}

    def spy(path: Path, **kwargs: object) -> audio.ProbeResult:
        seen.update(kwargs)
        return success()

    monkeypatch.setattr(audio, "probe", spy)
    discover.probe_for(cfg)(Path("/inbox/a.wav"))
    assert seen == {
        "ffprobe": cfg.audio.ffprobe,
        "timeout_seconds": cfg.audio.ffprobe_timeout_seconds,
        "max_attempts": cfg.retry.max_attempts,
    }
