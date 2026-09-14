"""`scripts/perf-report.sh` が §21.2 Phase 2 / Phase 3 を判定することを固定する（#93）。

**判定の閾値は SPEC にしかない。**スクリプトに直書きした数値が §21.2 と食い違うと、
「8 時間以内」と表示しながら別の閾値で判定することになる。ここで突き合わせる。

**8 時間判定は平均 `rtf` からの換算ではなく、実 `elapsed_s` の合計と Part 数からの
外挿で行う。**平均 rtf からの換算は Part 長のばらつきを潰し、短い Part ばかり測った
ときに楽観へ倒れる。`test_the_judgement_does_not_use_the_average_rtf` がそれを固定する。
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from tests.helpers import REPO_ROOT
from tests.spec_sync import spec_phase_acceptance

REPORT = REPO_ROOT / "scripts" / "perf-report.sh"

pytestmark = pytest.mark.skipif(not REPORT.is_file(), reason="scripts/ がマウントされていない")

EXIT_BELOW_TARGET = 6
EXIT_NO_DATA = 7
EXIT_INSUFFICIENT = 8

DAY_SECONDS = 16 * 3600
"""1 日分の音声（§21.2 Phase 2）。判定に足るかの分母になる。"""


def sample(*, audio_s: float, ratio: float, parts: int = 1) -> str:
    """合計 `audio_s` 秒の音声を `parts` 本に分け、処理時間を `audio × ratio` にしたログ。

    **`ratio` がそのまま判定になる** — 1 日分への外挿は
    `elapsed 合計 / 音声合計 × 16 時間` なので、`ratio <= 0.5` が 8 時間以内である。
    """
    each = audio_s / parts
    return "\n".join(
        asr_line(f"D/T/{i}_orig.wav", elapsed=each * ratio, rtf=ratio) for i in range(parts)
    )


def run(logs: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(REPORT), *args],
        input=logs,
        capture_output=True,
        text=True,
        check=False,
    )


def asr_line(
    key: str,
    *,
    elapsed: float,
    chars: int = 8421,
    rtf: float | None = 0.18,
    speech: float | None = 0.31,
) -> str:
    """§16.2 の text 形式の 1 行。"""
    fields = f"recording_key={key} elapsed_s={elapsed} chars={chars}"
    fields += f" rtf={'null' if rtf is None else rtf}"
    fields += f" speech_ratio={'null' if speech is None else speech}"
    return f"2026-08-30T07:06:33+09:00 INFO  transcription_completed {fields}"


def llm_line(key: str, *, chunks: int, elapsed: float) -> str:
    return (
        f"2026-08-30T10:03:22+09:00 INFO  llm_completed "
        f"session_key={key} chunks={chunks} elapsed_s={elapsed}"
    )


def as_json(line: str) -> str:
    """同じ内容を §16.2 の json 形式にする。**両形式で同じ結果になること**を見る。"""
    head, _, rest = line.partition("INFO  ")
    event, _, fields = rest.partition(" ")
    payload: dict[str, object] = {"ts": head.strip(), "level": "INFO", "event": event}
    for token in fields.split():
        name, _, value = token.partition("=")
        if value == "null":
            payload[name] = None
        else:
            try:
                payload[name] = float(value) if "." in value else int(value)
            except ValueError:
                payload[name] = value
    return json.dumps(payload, ensure_ascii=False)


# --- SPEC との一致 -------------------------------------------------------


def script_constants() -> dict[str, int]:
    """スクリプトの **`# SPEC ` 印が付いた**定数だけを読む。

    印で選ぶのは、**この道具の方針にすぎない定数**（`MIN_COVERAGE_PERCENT` など）を
    SPEC との突き合わせに巻き込まないためである。`\\s+#` で拾っていた頃は、
    直後に註釈ブロックが来る無関係な定数まで拾っていた。
    """
    body = REPORT.read_text(encoding="utf-8")
    found = dict(re.findall(r"^([A-Z_]+)=(\d+)[ \t]+# SPEC ", body, re.M))
    return {name: int(value) for name, value in found.items()}


def test_the_thresholds_come_from_the_spec() -> None:
    """**§21.2 / §20.3 の数値と 1 対 1。**SPEC を直したらここで落ちる。"""
    assert script_constants() == spec_phase_acceptance()


# --- 判定 ---------------------------------------------------------------


def test_a_fast_run_passes() -> None:
    result = run(sample(audio_s=DAY_SECONDS * 0.5, ratio=0.12, parts=16), "--asr")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "✅ **PASS**" in result.stdout


def test_a_slow_run_fails() -> None:
    result = run(sample(audio_s=DAY_SECONDS * 0.5, ratio=1.2, parts=16), "--asr")
    assert result.returncode == EXIT_BELOW_TARGET
    assert "✗ **FAIL**" in result.stdout


# --- 判定に足りないデータ（**実機で踏んだ**） ----------------------------


def test_a_tiny_sample_is_not_judged() -> None:
    """**26 秒の録音 2 本で「8 時間以内」と答えてはならない**（2026-09-14 / #95）。

    Part 数で外挿すると「1 Part = 13 秒」として 32 倍し、**0.25 時間で PASS** と答える。
    音声長で外挿すると固定費（1 回あたりのモデル読み込み）が効いて **34 時間で FAIL** と答える。
    **どちらも当てにならない。**だから判定しない。
    """
    result = run(sample(audio_s=26.1, ratio=2.15, parts=2), "--asr")
    assert result.returncode == EXIT_INSUFFICIENT, result.stdout
    assert "⬜ **保留**" in result.stdout
    assert "PASS" not in result.stdout
    assert "FAIL" not in result.stdout


def test_a_sample_just_over_the_threshold_is_judged() -> None:
    """陰性対照。**閾値を超えれば判定する。**保留が既定になっては道具の意味が無い。"""
    result = run(sample(audio_s=DAY_SECONDS * 0.11, ratio=0.12, parts=4), "--asr")
    assert result.returncode == 0, result.stdout
    assert "✅ **PASS**" in result.stdout


def test_the_measured_audio_length_is_shown() -> None:
    """**測った音声の長さと 1 日分に対する割合を必ず出す。**

    読んだ人が「この数字はどれだけの実測に基づくか」を判断できないと、
    外挿を鵜呑みにするしかない。
    """
    result = run(sample(audio_s=DAY_SECONDS * 0.5, ratio=0.12, parts=16), "--asr")
    assert "測った音声の長さ" in result.stdout
    assert "50.00%" in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(0.49, 0), (0.51, EXIT_BELOW_TARGET)],
)
def test_the_eight_hour_boundary_flips(ratio: float, expected: int) -> None:
    """**境界のすぐ上と下で反転すること。**

    1 日分 16 時間を 8 時間で処理する ＝ **音声長あたり 0.5**。
    """
    result = run(sample(audio_s=DAY_SECONDS * 0.2, ratio=ratio, parts=8), "--asr")
    assert result.returncode == expected, result.stdout


def test_the_judgement_does_not_use_the_part_count() -> None:
    """**Part 数で外挿しないこと。**

    2 本で十分な音声長を測った場合と、同じ音声長を 64 本に割った場合で、
    **判定は変わってはならない。**Part 数で外挿していると、本数を変えただけで
    答えが動く（実機で踏んだ誤りがこれである。#95）。
    """
    few = run(sample(audio_s=DAY_SECONDS * 0.5, ratio=0.12, parts=2), "--asr")
    many = run(sample(audio_s=DAY_SECONDS * 0.5, ratio=0.12, parts=64), "--asr")
    assert few.returncode == many.returncode == 0, few.stdout + many.stdout
    for line in ("1 日分（16 時間）への外挿",):
        assert _line_with(few.stdout, line) == _line_with(many.stdout, line), (
            few.stdout + "\n---\n" + many.stdout
        )


def _line_with(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"{needle!r} が出力に無い:\n{text}")


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(5999.0, 0), (6001.0, EXIT_BELOW_TARGET)],
)
def test_the_thirty_minute_boundary_flips(elapsed: float, expected: int) -> None:
    """`18 チャンクで 1800 秒` が境界。ここでは 60 チャンク分を流して 1 チャンク単価で見る。"""
    result = run(llm_line("D:20260829", chunks=60, elapsed=elapsed), "--llm")
    assert result.returncode == expected, result.stdout


# --- 形式と欠損 ----------------------------------------------------------


def test_text_and_json_agree() -> None:
    """**`logging.format` が `text` でも `json` でも同じ表になること**（§16.2）。"""
    lines = [
        asr_line("D/T/a_orig.wav", elapsed=331.4),
        asr_line("D/T/b_orig.wav", elapsed=400.0, rtf=0.22, speech=0.40),
        llm_line("D:20260829", chunks=18, elapsed=1337.0),
    ]
    text = run("\n".join(lines))
    js = run("\n".join(as_json(line) for line in lines))
    # **判定そのものは問わない。**見たいのは「形式が変わっても同じ答えになる」こと
    assert text.returncode == js.returncode, text.stdout + js.stdout
    assert text.stdout == js.stdout


def test_a_null_speech_ratio_is_left_out_of_the_average() -> None:
    """`duration_seconds` が不明なら `rtf` / `speech_ratio` は `null`（`transcribe.metrics`）。

    **`null` を 0 として平均に入れると、VAD の効果が実際より小さく見える。**
    """
    logs = "\n".join(
        [
            asr_line("D/T/a_orig.wav", elapsed=100, speech=0.40),
            asr_line("D/T/b_orig.wav", elapsed=100, speech=None),
        ]
    )
    result = run(logs, "--asr")
    assert "平均 `speech_ratio` : 0.400" in result.stdout, result.stdout


def test_no_data_does_not_crash() -> None:
    """**ログ 0 件でも 0 除算で落ちない。**まだ何も回していない段階で必ず踏む。"""
    result = run("何も無いログ\n")
    assert result.returncode == EXIT_NO_DATA
    assert result.stderr == ""
    assert "1 件も無い" in result.stdout


def test_zero_chunks_are_ignored() -> None:
    """`chunks=0` の行で割らないこと。"""
    result = run(llm_line("D:20260829", chunks=0, elapsed=10.0), "--llm")
    assert result.returncode == EXIT_NO_DATA
    assert result.stderr == ""


# --- 引数 ---------------------------------------------------------------


def test_asr_and_llm_select_one_section() -> None:
    logs = asr_line("D/T/a_orig.wav", elapsed=100) + "\n" + llm_line("D:1", chunks=1, elapsed=1)
    assert "LLM" not in run(logs, "--asr").stdout
    assert "文字起こし" not in run(logs, "--llm").stdout


def test_an_unknown_argument_is_rejected() -> None:
    """**黙って無視しない。**`--strict` のつもりで打った引数が効かないと気づけない。"""
    result = run("", "--strict")
    assert result.returncode == 2
    assert "不明な引数" in result.stderr
