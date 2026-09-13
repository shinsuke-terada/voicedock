"""`voicedock.audio.probe` が SPEC §10.5(1) のとおりに動くことを固定する。

**最重要の性質は「例外を投げない」ことである。**§10.5 は失敗時に
`duration_seconds = NULL` のまま続行すると規定しており、例外にすると全呼び手が
try/except を持つことになる。**1 箇所忘れた時点でその Part の記録が失われる**
（§1.3 の優先順位 1）。

実物の `ffprobe` を使うテストは `needs_ffmpeg` を付ける（CI には無い）。
それ以外は**偽の ffprobe スクリプト**で動かすので CI でも走る。
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from tests.fixtures.make_wav import Content, WavFormat, write_wav
from tests.spec_sync import spec_section_code
from voicedock.audio import FFPROBE_ARGS, ProbeResult, parse_output, probe

SPEECH_SECONDS = 1.5


def fake_ffprobe(tmp_path: Path, body: str, *, name: str = "ffprobe") -> Path:
    """`sh` で書いた偽 ffprobe。**呼ばれた回数を `calls` ファイルへ追記する。**"""
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\necho call >> '{tmp_path}/calls'\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def call_count(tmp_path: Path) -> int:
    log = tmp_path / "calls"
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def ffprobe_json(**overrides: object) -> str:
    """実物の ffprobe が返す形（**数値は文字列である**）。"""
    document: dict[str, object] = {
        "streams": [
            {
                "codec_name": "pcm_s24le",
                "sample_rate": "48000",
                "channels": 2,
                "sample_fmt": "s32",
            }
        ],
        "format": {"duration": "1.500000"},
    }
    document.update(overrides)
    return json.dumps(document)


def run(
    script: Path, target: Path, *, max_attempts: int = 1, timeout_seconds: int = 5
) -> ProbeResult:
    return probe(target, ffprobe=script, timeout_seconds=timeout_seconds, max_attempts=max_attempts)


# --- SPEC 整合 -----------------------------------------------------------


def test_arguments_match_the_spec_command() -> None:
    """§10.5(1) の `ffprobe` コマンドラインと `FFPROBE_ARGS` が一致すること。

    **SPEC のオプションを 1 つ変えたらここが落ちる。**`-select_streams a:0` を落とすと
    映像ストリームを持つファイルで別のストリームを読み、`sample_rate` が音声のものでなくなる。
    """
    block = spec_section_code("10.5", "bash")
    # 末尾の `<inbox のファイル>` はプレースホルダなので落とす（空白を含む点に注意）
    command, _sep, _placeholder = block.partition("<inbox")
    tokens = command.replace("\\\n", " ").split()
    assert tokens[0] == "ffprobe"
    assert tuple(tokens[1:]) == FFPROBE_ARGS


# --- 正常系 --------------------------------------------------------------


def test_reads_duration_and_format(tmp_path: Path) -> None:
    script = fake_ffprobe(tmp_path, f"cat <<'EOF'\n{ffprobe_json()}\nEOF")
    result = run(script, tmp_path / "a.wav")
    assert result.ok
    assert result.format is not None
    assert result.format.duration_seconds == pytest.approx(1.5)
    assert result.format.sample_rate == 48000
    assert result.format.channels == 2
    assert result.format.sample_fmt == "s32"
    assert result.format.codec_name == "pcm_s24le"
    assert result.error is None
    assert result.attempts == 1


def test_a_string_duration_becomes_a_float(tmp_path: Path) -> None:
    """**ffprobe は数値を文字列で返す。**`"1.500000"` をそのまま DB へ入れてはならない。"""
    script = fake_ffprobe(tmp_path, f"cat <<'EOF'\n{ffprobe_json()}\nEOF")
    duration = run(script, tmp_path / "a.wav").duration_seconds
    assert isinstance(duration, float)


def test_succeeds_on_the_first_attempt_without_retrying(tmp_path: Path) -> None:
    script = fake_ffprobe(tmp_path, f"cat <<'EOF'\n{ffprobe_json()}\nEOF")
    run(script, tmp_path / "a.wav", max_attempts=3)
    assert call_count(tmp_path) == 1


# --- duration が取れない場合（**probe 自体は成功**） ---------------------


@pytest.mark.parametrize("duration", ["N/A", "", "abc", None, "-1.0"])
def test_an_unusable_duration_is_none_but_the_probe_succeeds(
    tmp_path: Path, duration: str | None
) -> None:
    """`duration` だけが読めない場合、**probe は成功扱いで duration が `None`** になる。

    sample_rate などは取れているので、#18 の検証（16000 Hz / 1 ch / `s16`）は実行できる。
    負の duration は壊れたヘッダで、**採ると `ended_at` が開始より前になる。**
    """
    payload = {} if duration is None else {"duration": duration}
    script = fake_ffprobe(tmp_path, f"cat <<'EOF'\n{ffprobe_json(format=payload)}\nEOF")
    result = run(script, tmp_path / "a.wav")
    assert result.ok, "duration だけが読めないのは probe の失敗ではない"
    assert result.duration_seconds is None


def test_a_missing_format_object_is_tolerated(tmp_path: Path) -> None:
    script = fake_ffprobe(tmp_path, f"cat <<'EOF'\n{ffprobe_json(format=None)}\nEOF")
    result = run(script, tmp_path / "a.wav")
    assert result.ok
    assert result.duration_seconds is None


# --- 失敗系: **例外を投げない** -----------------------------------------


def test_no_audio_stream_is_a_failure(tmp_path: Path) -> None:
    """音声ストリームが 1 本も無いものは失敗にする。

    そのまま進めば #18 の ffmpeg が必ず落ちる。ここで失敗にすれば
    `duration_seconds = NULL` として**記録だけは残る。**
    """
    script = fake_ffprobe(tmp_path, f"cat <<'EOF'\n{ffprobe_json(streams=[])}\nEOF")
    result = run(script, tmp_path / "a.wav")
    assert not result.ok
    assert result.duration_seconds is None


@pytest.mark.parametrize(
    "body",
    [
        "exit 1",
        "echo 'not json'",
        "echo '[]'",
        "echo ''",
        ">&2 echo 'boom'; exit 3",
    ],
)
def test_a_failure_returns_a_result_instead_of_raising(tmp_path: Path, body: str) -> None:
    script = fake_ffprobe(tmp_path, body)
    result = run(script, tmp_path / "a.wav")  # 例外を投げない
    assert not result.ok
    assert result.error


def test_a_missing_ffprobe_returns_a_result(tmp_path: Path) -> None:
    """**`ffprobe` が無くても落ちない。**`audio.ffprobe` の設定ミスは V-* が起動時に弾くが、
    ここで例外にすると **1 件の設定ミスがその日の記録全体を止める。**
    """
    result = run(tmp_path / "does-not-exist", tmp_path / "a.wav")
    assert not result.ok
    assert result.error is not None
    assert "FileNotFoundError" in result.error


def test_a_timeout_returns_a_result(tmp_path: Path) -> None:
    script = fake_ffprobe(tmp_path, "sleep 5")
    result = run(script, tmp_path / "a.wav", timeout_seconds=1)
    assert not result.ok
    assert result.error is not None
    assert "1 秒" in result.error


# --- リトライ（§15.1 AUDIO_PROBE_FAILED は「可（3 回）」） --------------


def test_retries_up_to_max_attempts(tmp_path: Path) -> None:
    script = fake_ffprobe(tmp_path, "exit 1")
    result = run(script, tmp_path / "a.wav", max_attempts=3)
    assert call_count(tmp_path) == 3
    assert result.attempts == 3
    assert not result.ok


def test_stops_retrying_once_it_succeeds(tmp_path: Path) -> None:
    """**成功したらそこで止める。**失敗 → 成功の順で返す偽 ffprobe を使う。"""
    body = (
        f"if [ -f '{tmp_path}/once' ]; then cat <<'EOF'\n{ffprobe_json()}\nEOF\n"
        f"else touch '{tmp_path}/once'; exit 1; fi"
    )
    script = fake_ffprobe(tmp_path, body)
    result = run(script, tmp_path / "a.wav", max_attempts=3)
    assert result.ok
    assert result.attempts == 2
    assert call_count(tmp_path) == 2


@pytest.mark.parametrize("max_attempts", [0, -1])
def test_a_nonsense_max_attempts_still_runs_once(tmp_path: Path, max_attempts: int) -> None:
    """**0 回で「失敗」を返してはならない。**probe を 1 回も試さずに
    `duration_seconds = NULL` になると、設定ミスが静かに全 Part の duration を消す。
    """
    script = fake_ffprobe(tmp_path, f"cat <<'EOF'\n{ffprobe_json()}\nEOF")
    assert run(script, tmp_path / "a.wav", max_attempts=max_attempts).ok
    assert call_count(tmp_path) == 1


# --- 子プロセスの標準入力 -----------------------------------------------


def test_the_child_does_not_inherit_stdin(tmp_path: Path) -> None:
    """**`stdin` は `DEVNULL` である。**常駐サービスの標準入力を子が奪わないようにする。

    §10.5 の「`-nostdin` を付けてはならない」は **ffmpeg が `pipe:0` から WAV を
    受け取る**ための規定であり、ファイルパスを読む ffprobe には当たらない。
    """
    body = f"cat > '{tmp_path}/stdin.txt'\ncat <<'EOF'\n{ffprobe_json()}\nEOF"
    script = fake_ffprobe(tmp_path, body)
    run(script, tmp_path / "a.wav")
    assert (tmp_path / "stdin.txt").read_text(encoding="utf-8") == ""


# --- 出力パーサ単体 -----------------------------------------------------


@pytest.mark.parametrize("stdout", ["", "null", "[]", "{}", '{"streams": {}}', '{"streams":[1]}'])
def test_parse_output_returns_none_for_unusable_output(stdout: str) -> None:
    assert parse_output(stdout) is None


def test_parse_output_ignores_a_boolean_sample_rate() -> None:
    """`True` は Python では `int` の一種である。**`bool` を数値として採らない。**"""
    parsed = parse_output(json.dumps({"streams": [{"sample_rate": True}], "format": {}}))
    assert parsed is not None
    assert parsed.sample_rate is None


# --- 実物の ffprobe -----------------------------------------------------


@pytest.mark.needs_ffmpeg
def test_reads_a_real_wav(tmp_path: Path) -> None:
    """fixture が作る 48 kHz / 24 bit WAV を実物の ffprobe で読む（§8 の実測形式）。"""
    wav = write_wav(
        tmp_path / "TX00_MIC001_20260912_120950_orig.wav",
        seconds=SPEECH_SECONDS,
        fmt=WavFormat.PCM24,
        content=Content.SPEECH,
    )
    result = probe(wav, ffprobe=Path(_which("ffprobe")), timeout_seconds=30)
    assert result.ok
    assert result.format is not None
    assert result.format.sample_rate == 48000
    assert result.format.duration_seconds == pytest.approx(SPEECH_SECONDS, abs=0.05)


def _which(name: str) -> str:
    import shutil

    found = shutil.which(name)
    assert found is not None, f"{name} が無い"
    return found


def test_calls_file_is_isolated_per_test(tmp_path: Path) -> None:
    """`fake_ffprobe` の呼び出し記録が `tmp_path` 配下に閉じていること（§20.2）。"""
    assert not (Path.cwd() / "calls").exists()
