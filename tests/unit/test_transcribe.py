"""`voicedock.transcribe` が SPEC §10.6 のとおりに whisper.cpp を呼ぶことを固定する。

**whisper.cpp の知識はこのモジュールの中だけに閉じる**（§11.1(2)）。CPU 実行が実用に
満たず Mac ネイティブ実行へ差し替えるとき、変更するのはここの中身だけである。
だから**外から見える契約（argv・正規化形式・戻り値）をテストで固定する。**

実物の `whisper-cli` を使うテストは `needs_whisper` を付ける（CI には無い）。
それ以外は**偽 whisper** で動かすので CI でも走る。
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.fake_whisper import (
    HELP_WITHOUT_VAD,
    Utterance,
    raw_document,
    recorded_argv,
    write_fake_whisper,
)
from tests.helpers import example_document, parsed
from tests.spec_sync import spec_section_code
from voicedock import paths, transcribe
from voicedock.config import Config
from voicedock.errors import ErrorCode
from voicedock.paths import PartKey, StagingPath
from voicedock.transcribe import (
    MAX_THREADS,
    STDERR_TAIL_CHARS,
    Transcript,
    TranscriptSegment,
    build_argv,
    is_available,
    metrics,
    normalize,
    resolve_threads,
    timeout_for,
)

JST = ZoneInfo("Asia/Tokyo")
PARTKEY = PartKey("DJIMIC3/TX_MIC001_20260829_071201/TX01_MIC002_20260829_071204_orig.wav")
STARTED_AT = "2026-08-29T07:12:04+09:00"
DURATION = 1800.0


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`/data` を `tmp_path` へ寄せる。

    `paths.safe_unlink_staging()` は `/data` 配下しか消さない（§11.2）。**本番のコードに
    上書きの入口を作らない**方針なので、差し替えはテスト側で行う（`test_paths.py` と同じ）。
    """
    root = tmp_path / "data"
    (root / "transcripts" / "parts").mkdir(parents=True)
    monkeypatch.setattr(paths, "DATA_ROOT", root)
    monkeypatch.setattr(paths, "TRANSCRIPTS_ROOT", root / "transcripts" / "parts")
    return root


@pytest.fixture
def staging(tmp_path: Path, _data_root: Path) -> Path:
    folder = _data_root / "staging" / paths.key_slug(PARTKEY)
    folder.mkdir(parents=True)
    audio = folder / "audio16k.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")
    return audio


@pytest.fixture
def fake_dir(tmp_path: Path) -> Path:
    """偽 whisper の置き場。

    **`tmp_path` 直下に置いてはならない。**`tests/helpers.complete_tree()` が
    `tmp_path/whisper-cli` に「`exit 0` だけのスクリプト」を書くので、そこに置くと
    **実行可能ビットだけ残った空スクリプトに差し替わり、成功したのに何も出力しない**
    という一番分かりにくい失敗になる（実際に踏んだ）。
    """
    folder = tmp_path / "fake"
    folder.mkdir()
    return folder


@pytest.fixture
def make_cfg(make_config: Callable[..., Config]) -> Callable[..., Config]:
    def factory(executable: Path, **patch: object) -> Config:
        transcription: dict[str, object] = {"executable": str(executable)}
        transcription.update(patch)
        return make_config({"transcription": transcription})

    return factory


def run_transcribe(
    cfg: Config, audio: Path, *, duration: float | None = DURATION
) -> transcribe.TranscribeResult:
    return transcribe.transcribe(
        StagingPath(audio),
        partkey=PARTKEY,
        started_at=STARTED_AT,
        duration_seconds=duration,
        cfg=cfg,
    )


# --- argv が §10.6 と逐語一致すること -----------------------------------


def test_argv_matches_the_spec_command_line() -> None:
    """**§10.6 の bash ブロックと逐語一致すること。**

    `build_argv()` を独立した純関数にしているのはこのためである。埋め込みで組み立てると、
    **SPEC のフラグが 1 つ変わっても何も落ちない。**
    """
    block = spec_section_code("10.6", "bash")
    expected = block.replace("\\\n", " ").split()

    # **`make_config` を使わない。**あれは実在検査を通すためにパスを `tmp_path` へ
    # 差し替えるので、SPEC の `/usr/local/bin/whisper-cli` と一致しなくなる。
    # ここで見たいのは「SPEC の値をそのまま並べたときに同じ argv になるか」である
    cfg = parsed(example_document())
    argv = build_argv(
        cfg.transcription,
        audio=Path("/data/staging/<slug>/audio16k.wav"),
        out_base=Path("/data/transcripts/parts/<slug>"),
        threads=6,
    )
    assert argv == expected


def test_every_vad_flag_comes_from_the_configuration(
    make_config: Callable[..., Config],
) -> None:
    """VAD の 6 フラグが §7.2 の設定から渡ること。"""
    cfg = make_config(
        {
            "transcription": {
                "vad": {
                    "enabled": True,
                    "model": "/models/whisper/ggml-silero-v5.1.2.bin",
                    "threshold": 0.42,
                    "min_speech_duration_ms": 111,
                    "min_silence_duration_ms": 222,
                    "speech_pad_ms": 333,
                }
            }
        }
    )
    argv = build_argv(cfg.transcription, audio=Path("a.wav"), out_base=Path("b"))
    for flag, value in (
        ("--vad-threshold", "0.42"),
        ("--vad-min-speech-duration-ms", "111"),
        ("--vad-min-silence-duration-ms", "222"),
        ("--vad-speech-pad-ms", "333"),
    ):
        assert argv[argv.index(flag) + 1] == value


def test_vad_disabled_passes_no_vad_flag(make_config: Callable[..., Config]) -> None:
    """`vad.enabled: false` で **6 フラグが 1 つも渡らない**（§10.6）。"""
    cfg = make_config({"transcription": {"vad": {"enabled": False}}})
    argv = build_argv(cfg.transcription, audio=Path("a.wav"), out_base=Path("b"))
    assert not [arg for arg in argv if arg.startswith("--vad")]


@pytest.mark.parametrize(("configured", "expected"), [(6, 6), (1, 1), (64, 64)])
def test_threads_come_from_the_configuration(configured: int, expected: int) -> None:
    assert resolve_threads(configured) == expected


def test_zero_threads_is_capped_at_eight() -> None:
    """`threads: 0` なら `min(os.cpu_count(), 8)`（§10.6）。"""
    assert resolve_threads(0) == min(os.cpu_count() or 1, MAX_THREADS)
    assert resolve_threads(0) <= MAX_THREADS


# --- タイムアウト（§10.6） ----------------------------------------------


def test_timeout_is_clamped(make_config: Callable[..., Config]) -> None:
    """`clamp(duration × timeout_factor, min, max)`（§10.6）。"""
    cfg = make_config().transcription
    assert timeout_for(1800.0, cfg) == int(1800.0 * cfg.timeout_factor)
    assert timeout_for(1.0, cfg) == cfg.min_timeout_seconds, "下限"
    assert timeout_for(1_000_000.0, cfg) == cfg.max_timeout_seconds, "上限"


def test_a_null_duration_uses_the_maximum(make_config: Callable[..., Config]) -> None:
    """**`duration_seconds` が `None` なら `max_timeout_seconds`。**

    ffprobe 失敗で NULL のまま続行するので（§10.5）実際に存在する。`min` 側に倒すと
    **長い録音が必ずタイムアウトする。**
    """
    cfg = make_config().transcription
    assert timeout_for(None, cfg) == cfg.max_timeout_seconds


# --- 正常系 --------------------------------------------------------------


def test_writes_the_normalized_schema(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """保存される JSON が §10.6 の正規化形式であること。"""
    cfg = make_cfg(write_fake_whisper(fake_dir / "whisper-cli"))
    result = run_transcribe(cfg, staging)
    assert result.ok, result.error_message

    document = json.loads(paths.transcript_path_for(PARTKEY).read_text(encoding="utf-8"))
    assert set(document) == {
        "partkey",
        "language",
        "duration_seconds",
        "started_at",
        "text",
        "segments",
    }
    assert document["partkey"] == PARTKEY
    assert document["language"] == "ja"
    assert document["duration_seconds"] == DURATION
    assert document["started_at"] == STARTED_AT
    assert document["text"] == "おはようございます。今日の予定を確認します。"
    assert document["segments"][0] == {"start": 0.0, "end": 3.2, "text": "おはようございます。"}


def test_offsets_are_converted_from_milliseconds(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """**whisper.cpp の `offsets` はミリ秒である。**

    割り忘れると実機で時刻がすべて 1000 倍になる（テストだけでは気づけない種類の事故）。
    """
    script = write_fake_whisper(
        fake_dir / "whisper-cli", utterances=(Utterance(12.5, 20.25, " 正午すぎ"),)
    )
    result = run_transcribe(make_cfg(script), staging)
    assert result.transcript is not None
    assert result.transcript.segments == (
        TranscriptSegment(start=12.5, end=20.25, text="正午すぎ"),
    )


def test_segment_times_are_relative_to_the_part(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """`segments[].start` / `end` は **Part の先頭からの相対秒**（§10.6）。

    絶対時刻にしない。§10.8 が `started_at + start` で求める（#17 の `session.py`）。
    """
    cfg = make_cfg(write_fake_whisper(fake_dir / "whisper-cli"))
    result = run_transcribe(cfg, staging)
    assert result.transcript is not None
    assert result.transcript.segments[0].start == 0.0, "絶対時刻になっている"


def test_the_raw_json_is_not_left_in_the_transcripts_directory(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config], _data_root: Path
) -> None:
    """**生 JSON は残さない**（§10.6）。

    同じディレクトリに 2 種類のスキーマが混在すると、**§9.4 の冪等判定がどちらを読むか
    決まらなくなる。**
    """
    cfg = make_cfg(write_fake_whisper(fake_dir / "whisper-cli"))
    run_transcribe(cfg, staging)

    written = sorted(p.name for p in (_data_root / "transcripts" / "parts").iterdir())
    assert written == [f"{paths.key_slug(PARTKEY)}.json"]
    assert not list(staging.parent.glob("whisper*"))


def test_whisper_writes_its_raw_json_into_staging(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config], _data_root: Path
) -> None:
    """**`-of` は staging を指す。**`/data/transcripts/parts/` を指してはならない（§10.6）。

    消してから終わるので結果だけ見れば同じだが、**whisper の直後に落ちた場合に
    生 JSON が transcripts ディレクトリへ残る。**そうなると §9.4 の冪等判定が
    「2 種類のスキーマが混在するディレクトリ」を読むことになる。§10.6 が
    「生 JSON は残さない」と書いているのはこの状態を作らないためである。
    """
    script = write_fake_whisper(fake_dir / "whisper-cli")
    run_transcribe(make_cfg(script), staging)

    argv = recorded_argv(script)
    out_base = Path(argv[argv.index("-of") + 1])
    assert out_base.parent == staging.parent, "生 JSON の置き場が staging の外にある"
    assert out_base.parent != paths.TRANSCRIPTS_ROOT


def test_the_argv_is_passed_as_a_list(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """**`shell=True` と文字列連結を使わない**（§14.4）。

    偽 whisper が `"$@"` を 1 行ずつ記録するので、**1 本の文字列として渡っていれば
    行数が 1 になる。**ファイル名は外部入力であり、利用者が付けた名前が入る。
    """
    script = write_fake_whisper(fake_dir / "whisper-cli")
    run_transcribe(make_cfg(script), staging)

    argv = recorded_argv(script)
    assert len(argv) > 10, "argv が 1 本の文字列として渡っている"
    assert "-f" in argv
    assert str(staging) in argv


def test_no_module_passes_shell_to_subprocess() -> None:
    """**`src/` のどこにも `shell=` を渡す呼び出しが無いこと**（§14.4）。

    文字列検索ではなく構文木で見る。散文の `` `shell=True` `` に引っかからず、
    **`shell=cond` のような書き方も捕まる。**ファイル名は外部入力であり、
    デバイス上のファイル名は利用者が付けたものである。
    """
    offenders: list[str] = []
    for module in sorted(Path(transcribe.__file__).parent.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(
                keyword.arg == "shell" for keyword in node.keywords
            ):
                offenders.append(f"{module.name}:{node.lineno}")
    assert offenders == []


# --- 冪等性（§10.6 / §9.4） ---------------------------------------------


def test_an_existing_transcript_is_reused(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """出力 JSON が在り `text` が `min_chars` 以上なら**再実行しない**（§10.6）。"""
    script = write_fake_whisper(fake_dir / "whisper-cli")
    cfg = make_cfg(script)
    run_transcribe(cfg, staging)
    Path(f"{script}.argv").unlink()

    again = run_transcribe(cfg, staging)
    assert again.reused
    assert again.ok
    assert recorded_argv(script) == [], "whisper が再実行されている"


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        "not json",
        '{"text": "ある"}',
        '{"partkey":"x","language":"ja","duration_seconds":1,"started_at":"x","text":"y"}',
    ],
)
def test_a_broken_transcript_is_regenerated(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config], content: str
) -> None:
    """**正規化スキーマでパースできないものは「無い」と同じ扱い**（§10.6）。

    whisper の生 JSON がここに残っていた場合もこれで再生成される。
    """
    paths.transcript_path_for(PARTKEY).write_text(content, encoding="utf-8")
    cfg = make_cfg(write_fake_whisper(fake_dir / "whisper-cli"))
    result = run_transcribe(cfg, staging)
    assert result.ok
    assert not result.reused


def test_a_short_transcript_is_regenerated(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """`text` が `min_chars` 未満なら再実行する（§10.6）。"""
    cfg = make_cfg(write_fake_whisper(fake_dir / "whisper-cli"), min_chars=100)
    run_transcribe(cfg, staging)
    again = run_transcribe(cfg, staging)
    assert not again.reused


# --- 失敗（§15.1） ------------------------------------------------------


def test_a_nonzero_exit_is_whisper_failed(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    script = write_fake_whisper(
        fake_dir / "whisper-cli", exit_code=2, stderr="boom", write_output=False
    )
    result = run_transcribe(make_cfg(script), staging)
    assert result.error_code is ErrorCode.WHISPER_FAILED
    assert result.error_message is not None
    assert "boom" in result.error_message
    assert result.transcript is None


def test_the_stderr_tail_is_kept(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """stderr の**末尾** 1000 文字（§10.6）。先頭ではない — 原因は最後に出る。"""
    noise = "x" * 3000 + "REAL_CAUSE"
    script = write_fake_whisper(
        fake_dir / "whisper-cli", exit_code=1, stderr=noise, write_output=False
    )
    result = run_transcribe(make_cfg(script), staging)
    assert result.error_message is not None
    assert "REAL_CAUSE" in result.error_message
    assert len(result.error_message) < STDERR_TAIL_CHARS + 100


def test_a_missing_executable_is_reported(
    staging: Path, make_cfg: Callable[..., Config], fake_dir: Path
) -> None:
    """**例外を投げない**（§1.3）。設定ミスがその日の記録全体を止めない。"""
    result = run_transcribe(make_cfg(fake_dir / "nope"), staging)
    assert result.error_code is ErrorCode.WHISPER_EXEC_MISSING
    assert result.transcript is None


def test_unreadable_output_is_whisper_failed(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """終了コード 0 なのに JSON が無い場合。"""
    script = write_fake_whisper(fake_dir / "whisper-cli", write_output=False)
    result = run_transcribe(make_cfg(script), staging)
    assert result.error_code is ErrorCode.WHISPER_FAILED


# --- タイムアウト（§10.6） ----------------------------------------------


def test_a_timeout_kills_the_process_group(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """タイムアウトで `WHISPER_TIMEOUT` になり、**プロセスが残らない**（§10.6）。

    `start_new_session=True` + `os.killpg()` にしているのは、`subprocess.run(timeout=)` の
    既定が直接の子だけを殺すためである。**whisper が子を持つ実装に変わった日に孤児が残る。**
    """
    marker = fake_dir / "grandchild-finished"
    script = write_fake_whisper(fake_dir / "whisper-cli", sleep_seconds=3, grandchild_marker=marker)
    cfg = make_cfg(script, min_timeout_seconds=1, max_timeout_seconds=1, timeout_factor=0.001)

    began = time.monotonic()
    result = run_transcribe(cfg, staging, duration=1.0)
    elapsed = time.monotonic() - began

    assert result.error_code is ErrorCode.WHISPER_TIMEOUT
    assert result.transcript is None
    assert elapsed < 10, "タイムアウトが効いていない"

    # **孫プロセスが生きていれば 3 秒後に marker を作る。**`process.kill()` だけでは
    # `sh` しか死なないのでここが真になる。`os.killpg()` ならグループごと落ちる
    time.sleep(3.5)
    assert not marker.exists(), "プロセスグループが生き残っている（孤児が残る）"


def test_a_timeout_removes_the_partial_output(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config], _data_root: Path
) -> None:
    """部分出力を消す（§10.6）。**残すと次回「変換済み」と誤認する。**"""
    script = write_fake_whisper(fake_dir / "whisper-cli", sleep_seconds=30)
    cfg = make_cfg(script, min_timeout_seconds=1, max_timeout_seconds=1, timeout_factor=0.001)
    run_transcribe(cfg, staging, duration=1.0)
    assert not list(staging.parent.glob("whisper*.json"))


# --- 発話なし（§10.6） --------------------------------------------------


def test_no_speech_is_not_a_failure(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """`NO_SPEECH_DETECTED` は失敗ではなく `SKIPPED` である（§10.6）。"""
    script = write_fake_whisper(fake_dir / "whisper-cli", utterances=())
    result = run_transcribe(make_cfg(script), staging)
    assert result.error_code is ErrorCode.NO_SPEECH_DETECTED
    assert result.transcript is not None, "何が返ったかを捨てない"
    assert result.transcript.text == ""


def test_no_speech_still_writes_the_transcript(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """**発話なしでも正規化 JSON を書く。**

    whisper が何を返したかが残らないと、幻覚の有無も「本当に無音だったか」も後から
    確かめられない（§10.6 が VAD を標準にした理由そのもの）。冪等判定は `text` が
    `min_chars` 以上かで行うので、書いてあっても再実行の判断は変わらない。
    """
    script = write_fake_whisper(fake_dir / "whisper-cli", utterances=())
    run_transcribe(make_cfg(script), staging)
    assert paths.transcript_path_for(PARTKEY).is_file()


def test_min_chars_is_respected(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    script = write_fake_whisper(fake_dir / "whisper-cli", utterances=(Utterance(0, 1, " あ"),))
    assert run_transcribe(make_cfg(script, min_chars=1), staging).ok
    assert (
        run_transcribe(make_cfg(script, min_chars=2), staging).error_code
        is ErrorCode.NO_SPEECH_DETECTED
    )


# --- 変換の堅牢性 -------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"transcription": "not a list"},
        {"transcription": [1, 2, 3]},
        {"transcription": [{"offsets": {"from": "x", "to": 1}, "text": "a"}]},
        {"transcription": [{"text": "offsets が無い"}]},
        None,
        "string",
    ],
)
def test_normalize_never_raises(document: object) -> None:
    """**1 区間の不良で Part 全体を失わない**（§1.3）。"""
    result = normalize(
        document,
        partkey=PARTKEY,
        started_at=STARTED_AT,
        duration_seconds=DURATION,
        fallback_language="ja",
    )
    assert isinstance(result, Transcript)
    assert result.language == "ja"


def test_normalize_skips_only_the_bad_entry() -> None:
    document = raw_document((Utterance(0, 1, " よい"), Utterance(2, 3, " もよい")))
    entries = document["transcription"]
    assert isinstance(entries, list)
    entries.insert(1, {"offsets": {"from": None, "to": None}, "text": "壊れている"})

    result = normalize(
        document,
        partkey=PARTKEY,
        started_at=STARTED_AT,
        duration_seconds=DURATION,
        fallback_language="ja",
    )
    assert [s.text for s in result.segments] == ["よい", "もよい"]


def test_normalize_uses_the_reported_language() -> None:
    result = normalize(
        raw_document(language="en"),
        partkey=PARTKEY,
        started_at=STARTED_AT,
        duration_seconds=DURATION,
        fallback_language="ja",
    )
    assert result.language == "en"


# --- metrics（§16.2） ---------------------------------------------------


def test_metrics_match_the_spec_fields(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """`transcription_completed` のフィールド（§16.2）。"""
    cfg = make_cfg(write_fake_whisper(fake_dir / "whisper-cli"))
    result = run_transcribe(cfg, staging)
    fields = metrics(result, duration_seconds=DURATION)
    assert set(fields) == {"elapsed_s", "chars", "rtf", "speech_ratio"}
    assert fields["chars"] == 22
    speech = result.transcript.speech_seconds if result.transcript else 0.0
    assert fields["speech_ratio"] == pytest.approx(round(speech / DURATION, 3))


def test_metrics_do_not_divide_by_zero(
    fake_dir: Path, staging: Path, make_cfg: Callable[..., Config]
) -> None:
    """`duration_seconds` が `None` / 0 でも落ちない（§10.5 の NULL。§1.3）。"""
    cfg = make_cfg(write_fake_whisper(fake_dir / "whisper-cli"))
    result = run_transcribe(cfg, staging, duration=None)
    for total in (None, 0.0):
        fields = metrics(result, duration_seconds=total)
        assert fields["rtf"] is None
        assert fields["speech_ratio"] is None


# --- is_available（§19.2 D-7） ------------------------------------------


def test_is_available_detects_vad_support(
    fake_dir: Path, make_config: Callable[..., Config]
) -> None:
    cfg = make_config({"transcription": {"executable": str(fake_dir / "w")}})
    write_fake_whisper(fake_dir / "w")
    found = is_available(cfg.transcription)
    assert found.ok
    assert found.vad_supported is True


def test_is_available_detects_missing_vad_support(
    fake_dir: Path, make_config: Callable[..., Config]
) -> None:
    """**`--help` の出力で判定する。**バージョン文字列では判定しない（§10.6 の注記）。

    VAD はビルド設定で落ちうる。
    """
    cfg = make_config({"transcription": {"executable": str(fake_dir / "w")}})
    write_fake_whisper(fake_dir / "w", help_text=HELP_WITHOUT_VAD)
    found = is_available(cfg.transcription)
    assert found.ok
    assert found.vad_supported is False


def test_is_available_reports_a_missing_executable(
    fake_dir: Path, make_config: Callable[..., Config]
) -> None:
    cfg = make_config({"transcription": {"executable": str(fake_dir / "absent")}})
    found = is_available(cfg.transcription)
    assert not found.ok
    assert found.vad_supported is None


# --- 実物の whisper-cli -------------------------------------------------


@pytest.mark.needs_whisper
def test_the_real_whisper_reports_vad_support(make_config: Callable[..., Config]) -> None:
    """**§10.6 の注記が要求する確認。**`WHISPER_CPP_REF` を上げたらここで分かる。

    **`make_config()` の `executable` を使わない。**あれは `complete_tree()` が置く偽物で、
    ここで見たいのは**イメージに焼かれた本物**である（§18.5）。
    """
    real = shutil.which("whisper-cli")
    assert real is not None
    cfg = make_config({"transcription": {"executable": real}})
    found = is_available(cfg.transcription)
    assert found.ok, found.detail
    assert found.vad_supported is True, "whisper.cpp の VAD フラグが消えている（§22 R-5）"


def test_started_at_of_uses_seconds_precision() -> None:
    moment = datetime(2026, 8, 29, 7, 12, 4, 123456, tzinfo=JST)
    assert transcribe.started_at_of(moment) == STARTED_AT
