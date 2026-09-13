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
    """スクリプト冒頭の 5 つの定数を読む。"""
    body = REPORT.read_text(encoding="utf-8")
    found = dict(re.findall(r"^([A-Z_]+)=(\d+)\s+#", body, re.M))
    return {name: int(value) for name, value in found.items()}


def test_the_thresholds_come_from_the_spec() -> None:
    """**§21.2 / §20.3 の数値と 1 対 1。**SPEC を直したらここで落ちる。"""
    assert script_constants() == spec_phase_acceptance()


# --- 判定 ---------------------------------------------------------------


def test_a_fast_run_passes() -> None:
    logs = "\n".join(asr_line(f"D/T/{i}_orig.wav", elapsed=331.4) for i in range(4))
    result = run(logs, "--asr")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "✅ **PASS**" in result.stdout


def test_a_slow_run_fails() -> None:
    logs = asr_line("D/T/a_orig.wav", elapsed=1200)
    result = run(logs, "--asr")
    assert result.returncode == EXIT_BELOW_TARGET
    assert "✗ **FAIL**" in result.stdout


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(899.0, 0), (901.0, EXIT_BELOW_TARGET)],
)
def test_the_eight_hour_boundary_flips(elapsed: float, expected: int) -> None:
    """**境界のすぐ上と下で反転すること。**`900 * 32 / 3600 = 8.0` 時間。"""
    result = run(asr_line("D/T/a_orig.wav", elapsed=elapsed), "--asr")
    assert result.returncode == expected, result.stdout


def test_the_judgement_does_not_use_the_average_rtf() -> None:
    """**平均 `rtf` からの換算では PASS になるが、実 `elapsed_s` では FAIL になる形。**

    `rtf=0.1` なら 16 時間 × 0.1 = 1.6 時間で悠々 PASF に見えるが、
    実際に 1 Part へ 1200 秒かけているので 32 Part では 10.7 時間かかる。
    **短い Part ばかり測ったときに楽観へ倒れる**のがこの換算の危うさである。
    """
    logs = "\n".join(asr_line(f"D/T/{i}_orig.wav", elapsed=1200, rtf=0.1) for i in range(2))
    result = run(logs, "--asr")
    assert result.returncode == EXIT_BELOW_TARGET, result.stdout
    assert "10.67 時間" in result.stdout


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
    assert text.returncode == js.returncode == 0, text.stdout + js.stdout
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
