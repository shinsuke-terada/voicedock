"""`voicedock.log` が SPEC §16 と一致することを固定する。

§16.2 の出力例 10 行と json 例 1 行は、**SPEC から読み出して 1 バイト単位で突き合わせる**。
§16.3 の遮断（N-8）は、本文を渡しても出力に現れないことで確認する。
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Mapping
from datetime import UTC, timedelta, timezone
from pathlib import Path

import pytest

from tests.spec_sync import spec_event_names, spec_log_json_example, spec_log_text_examples
from voicedock import log
from voicedock.log import EVENTS, MAX_VALUE_CHARS, REDACTED, Logger, get_logger

JST = timezone(timedelta(hours=9))

# §8.1 の自然キー。**ログにも識別子をそのまま出す**（§16.2）。短い別名を作ると
# 「ログの識別子」と「ノートの識別子」が 2 系統になり、障害時の突き合わせが増える。
RECORDING_KEY = "DJIMIC3/TX_MIC001_20260829_071201/TX01_MIC002_20260829_071204_orig.wav"
OTHER_KEY = "DJIMIC3/TX_MIC001_20260829_074201/TX01_MIC002_20260829_074210_orig.wav"
SESSION_KEY = "DJIMIC3:20260829"

# §16.2 の text 例を再現するための引数。SPEC の行と 1 対 1 に対応する。
TEXT_EXAMPLE_FIELDS: dict[str, dict[str, log.LogValue]] = {
    "service_started": {"version": "0.1.0"},
    "part_discovered": {
        "recording_key": RECORDING_KEY,
        "tx": "TX01",
        "mic": 2,
        "started_at": "2026-08-29T07:12:04+09:00",
        "duration": 1800.0,
    },
    "part_skipped": {"recording_key": OTHER_KEY, "reason": "variant_not_orig"},
    "normalize_completed": {
        "recording_key": RECORDING_KEY,
        "in_bytes": 345600044,
        "out_bytes": 57600044,
        "elapsed_s": 43.1,
    },
    "transcription_completed": {
        "recording_key": RECORDING_KEY,
        "elapsed_s": 331.4,
        "chars": 8421,
        "rtf": 0.18,
        "speech_ratio": 0.31,
    },
    "raw_note_saved": {"session_key": SESSION_KEY, "parts": 1, "bytes": 18402},
    "session_merged": {
        "session_key": SESSION_KEY,
        "parts": 32,
        "excluded": 1,
        "chars": 348210,
    },
    "llm_completed": {"session_key": SESSION_KEY, "chunks": 18, "elapsed_s": 1337.0},
    "obsidian_saved": {
        "session_key": SESSION_KEY,
        "path": "Daily/Voice/Wiki/20260829/2026-08-29 Voice.md",
        "bytes": 12844,
    },
    "source_delete_skipped": {
        "session_key": SESSION_KEY,
        "reason": "delete_source_audio_disabled",
    },
}

TS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


def _logger(
    buf: io.StringIO,
    *,
    fmt: log.LogFormat = "text",
    level: log.LogLevel = "INFO",
    unsafe_log_content: bool = False,
) -> Logger:
    return get_logger(
        fmt=fmt, level=level, tz=JST, unsafe_log_content=unsafe_log_content, stream=buf
    )


def _emit(
    event: str,
    fields: Mapping[str, log.LogValue] | None = None,
    *,
    fmt: log.LogFormat = "text",
) -> str:
    """1 イベントを出力し、末尾の改行を落とした 1 行を返す。

    フィールドは `**kwargs` ではなく Mapping で受ける。`**kwargs` にすると
    `fmt` へ誤って束縛され得ることを mypy が正しく指摘する。
    """
    buf = io.StringIO()
    _logger(buf, fmt=fmt).info(event, **dict(fields or {}))
    out = buf.getvalue()
    assert out.endswith("\n"), "1 イベント 1 行で終端すること"
    return out[:-1]


# --- §16.4 レジストリ ----------------------------------------------------


def test_event_registry_matches_spec() -> None:
    """§16.4 の一覧と EVENTS が 1 対 1 であること（過不足の両方を検出する）。"""
    assert frozenset(spec_event_names()) == EVENTS


def test_event_order_follows_spec() -> None:
    """_EVENT_ORDER の並びが §16.4 の行の並びと同じであること。"""
    assert list(log._EVENT_ORDER) == spec_event_names()


def test_spec_examples_cover_every_text_line() -> None:
    """§16.2 の text 例の全行をテストが再現していること。

    SPEC に例が 1 行足されたらこのテストが落ち、再現の追加を強制する。
    """
    assert set(TEXT_EXAMPLE_FIELDS) == set(spec_log_text_examples())


# --- §16.2 形式 ----------------------------------------------------------


@pytest.mark.parametrize("event", sorted(TEXT_EXAMPLE_FIELDS))
def test_text_format_matches_spec_example(event: str) -> None:
    line = _emit(event, TEXT_EXAMPLE_FIELDS[event])
    ts, rest = line.split(" ", 1)
    assert TS_PATTERN.match(ts), ts
    assert rest == spec_log_text_examples()[event]


def test_json_format_matches_spec_example() -> None:
    expected = spec_log_json_example()
    payload = json.loads(expected)
    fields = {k: v for k, v in payload.items() if k not in ("ts", "level", "event")}

    actual = _emit(payload["event"], fields, fmt="json")
    got = json.loads(actual)

    assert list(got) == list(payload), "キー順も ts / level / event 先頭で固定する"
    assert TS_PATTERN.match(got["ts"]), got["ts"]
    # 区切り（`,` / `:`）とエスケープまで同形であることを、ts だけ差し替えて全文で見る
    mask = re.compile(r'"ts":"[^"]+"')
    assert mask.sub('"ts":"X"', actual) == mask.sub('"ts":"X"', expected)


def test_text_without_fields_has_no_trailing_space() -> None:
    line = _emit("service_stopping")
    assert not line.endswith(" ")
    assert line.split(" ", 1)[1] == "INFO  service_stopping"


def test_text_quotes_only_when_needed() -> None:
    line = _emit(
        "session_merged",
        {
            "session_key": "DJI_MIC:20260829",
            "reason": "a b",
            "note": "say=yes",
            "quoted": 'he said "hi"',
            "japanese": "日本語",
            "empty": "",
        },
    )
    assert "session_key=DJI_MIC:20260829" in line
    assert 'reason="a b"' in line
    assert 'note="say=yes"' in line
    assert 'quoted="he said \\"hi\\""' in line
    assert 'japanese="日本語"' in line
    assert 'empty=""' in line


def test_text_spells_bool_and_none_like_json() -> None:
    line = _emit("helper_recovered", {"writable": False, "readable": True, "label": None})
    assert "writable=false readable=true label=null" in line


def test_json_keeps_non_ascii_readable() -> None:
    line = _emit("session_merged", {"reason": "日本語"}, fmt="json")
    assert '"reason":"日本語"' in line


# --- §16.3 / N-8 遮断 ----------------------------------------------------


def test_content_fields_are_redacted() -> None:
    """transcript / summary 本文を渡しても出力に現れないこと（N-8）。"""
    line = _emit(
        "session_merged",
        {
            "session_key": SESSION_KEY,
            "transcript": "これは本文です。" * 20,
            "summary": "秘密の要約",
            "tasks": "買い物に行く",
        },
    )
    assert "これは本文です" not in line
    assert "秘密の要約" not in line
    assert "買い物に行く" not in line
    assert f"transcript={REDACTED}" in line
    assert f"summary={REDACTED}" in line
    assert f"tasks={REDACTED}" in line
    assert f"session_key={SESSION_KEY}" in line, "本文以外のフィールドは残ること"


def test_filename_and_title_are_redacted() -> None:
    """§16.3「ファイル名も内容を含み得る」。v3.0 の漏洩経路を塞ぐ。"""
    line = _emit(
        "obsidian_saved", {"filename": "2026-08-29 給与交渉の相談.md", "title": "給与交渉"}
    )
    assert "給与交渉" not in line
    assert f"filename={REDACTED}" in line


def test_content_fields_are_redacted_in_json_too() -> None:
    payload = json.loads(_emit("session_merged", {"transcript": "本文" * 200}, fmt="json"))
    assert payload["transcript"] == REDACTED


@pytest.mark.parametrize("length", [MAX_VALUE_CHARS + 1, MAX_VALUE_CHARS + 1000])
def test_long_strings_are_redacted_regardless_of_key(length: int) -> None:
    """キー名のデニーリストに無いキーでも、長さで止まること。"""
    line = _emit("session_merged", {"detail": "x" * length})
    assert f"detail={REDACTED}" in line


def test_value_at_the_limit_is_kept() -> None:
    value = "x" * MAX_VALUE_CHARS
    assert f"detail={value}" in _emit("session_merged", {"detail": value})


def test_unsafe_allows_content_at_debug_only() -> None:
    buf = io.StringIO()
    logger = _logger(buf, level="DEBUG", unsafe_log_content=True)

    logger.debug("session_merged", transcript="本文")
    assert 'transcript="本文"' in buf.getvalue()

    buf.truncate(0)
    buf.seek(0)
    logger.info("session_merged", transcript="本文")
    assert f"transcript={REDACTED}" in buf.getvalue()
    assert "本文" not in buf.getvalue()


def test_debug_without_unsafe_is_still_redacted() -> None:
    buf = io.StringIO()
    _logger(buf, level="DEBUG").debug("session_merged", transcript="本文")
    assert f"transcript={REDACTED}" in buf.getvalue()


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1},
        ["a"],
        ("a",),
        Path("/inbox/x.wav"),
        object(),
        b"bytes",
    ],
)
def test_rejects_non_scalar_values(value: object) -> None:
    buf = io.StringIO()
    with pytest.raises(TypeError, match="str / int / float / bool / None"):
        _logger(buf).info("session_merged", detail=value)  # type: ignore[arg-type]
    assert buf.getvalue() == "", "型違反のときは 1 バイトも出さないこと"


# --- レベルとイベント名の検証 --------------------------------------------


def test_unknown_event_raises() -> None:
    buf = io.StringIO()
    with pytest.raises(ValueError, match="未登録のイベント名"):
        _logger(buf).info("transcription_complete")  # typo
    assert buf.getvalue() == ""


def test_unknown_event_raises_even_when_the_level_is_filtered() -> None:
    """検証はレベルフィルタより先に行う。typo が INFO 運用で見逃されないようにする。"""
    buf = io.StringIO()
    with pytest.raises(ValueError, match="未登録のイベント名"):
        _logger(buf, level="INFO").debug("no_such_event")


@pytest.mark.parametrize("name", ["ts", "level"])
def test_rejects_reserved_field_names(name: str) -> None:
    """json 形式の先頭キーと同名のフィールドは拒む。

    許すと `level` が実際のレベルと食い違う行を作れてしまう。
    残りの予約名 `event` は引数名そのものなので、Python が呼び出しの時点で
    `TypeError`（多重指定）にする（次のテスト）。
    """
    buf = io.StringIO()
    with pytest.raises(ValueError, match="フィールド名に使えない名前"):
        _logger(buf).info("service_started", **{name: "x"})
    assert buf.getvalue() == ""


def test_event_field_collides_at_the_call_site() -> None:
    buf = io.StringIO()
    with pytest.raises(TypeError, match="event"):
        _logger(buf).info("service_started", **{"event": "x"})
    assert buf.getvalue() == ""


def test_level_filtering() -> None:
    buf = io.StringIO()
    logger = _logger(buf, level="WARNING")
    logger.debug("service_started")
    logger.info("service_started")
    assert buf.getvalue() == ""
    logger.warning("helper_heartbeat_stale")
    logger.error("normalize_failed")
    assert buf.getvalue().count("\n") == 2
    assert "WARNING helper_heartbeat_stale" in buf.getvalue()
    assert "ERROR normalize_failed" in buf.getvalue()


def test_level_column_is_five_wide() -> None:
    """レベル列は 5 桁左詰め。§16.2 の `INFO  service_started`（空白 2 つ）が幅を決める。

    `WARNING` は 7 桁なのでこの列からあふれる。SPEC の例に合わせる方を採る。
    """
    assert _emit("service_started").split(" ", 1)[1].startswith("INFO  service_started")

    buf = io.StringIO()
    _logger(buf, level="DEBUG").debug("service_started")
    assert "DEBUG service_started" in buf.getvalue()


def test_rejects_unknown_format_and_level() -> None:
    with pytest.raises(ValueError, match="未知のログ形式"):
        get_logger(fmt="yaml")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="未知のログレベル"):
        get_logger(level="TRACE")  # type: ignore[arg-type]


def test_timezone_is_respected() -> None:
    assert _emit("service_started").split(" ", 1)[0].endswith("+09:00")

    buf = io.StringIO()
    get_logger(stream=buf, tz=UTC).info("service_started")
    assert buf.getvalue().split(" ", 1)[0].endswith("+00:00")


def test_timestamp_has_no_microseconds() -> None:
    ts = _emit("service_started").split(" ", 1)[0]
    assert "." not in ts, "秒精度で出す（§16.2）"


def test_defaults_to_stdout_late_bound(capsys: pytest.CaptureFixture[str]) -> None:
    """stream 未指定なら出力のたびに sys.stdout を見ること。"""
    get_logger().info("service_started", version="0.1.0")
    assert "service_started version=0.1.0" in capsys.readouterr().out
