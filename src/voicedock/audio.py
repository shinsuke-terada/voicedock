"""ffprobe による形式取得と 16 kHz への変換（SPEC §10.5）。

**コンテナはデバイスに到達できない**（§14.4 N-3）。Helper が inbox へ置いた原本を
読みながら SHA-256 を計算し、そのまま ffmpeg へ流して 16 kHz WAV を生成する。
**staging へ原本をコピーしない。**

whisper.cpp の WAV リーダは 16 kHz / モノラル / 16 bit PCM しか受け付けず、DJI Mic 3 は
48 kHz / 24 bit または 32 bit float で記録するため、**この変換なしでは必ず失敗する。**

**ここが「Helper のコピーが壊れていないことを確認する唯一の経路」である**（§10.5）。
読みながら算出した SHA-256 を `.meta.json` の値と照合する。

**`probe()` は例外を投げない。**§10.5 は「`max_attempts` 回リトライし、それでも失敗したら
`duration_seconds = NULL` のまま処理を続行する」と規定している。例外にすると全呼び手が
try/except を持つことになり、**1 箇所忘れた時点でその Part の記録が失われる**
（§1.3 の優先順位 1 に反する）。失敗は戻り値で表す。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from voicedock import paths
from voicedock.config import Config
from voicedock.errors import ErrorCode
from voicedock.paths import InboxPath, PartKey, StagingPath

FFPROBE_ARGS: Final[tuple[str, ...]] = (
    "-v",
    "error",
    "-select_streams",
    "a:0",
    "-show_entries",
    "format=duration:stream=sample_rate,channels,sample_fmt,codec_name",
    "-of",
    "json",
)
"""§10.5(1) のコマンドライン。**SPEC のコードブロックと 1 対 1 に保つこと。**

`tests/unit/test_probe.py` が §10.5 のコードブロックから読み取って突き合わせる。
"""

MAX_ERROR_CHARS: Final = 200
"""`stderr` の保持長。`db.truncate()` と同じ上限（§8.4）。"""


@dataclass(frozen=True)
class AudioFormat:
    """ffprobe が返した形式（§10.5(1)）。"""

    duration_seconds: float | None
    """**`None` でも probe は成功扱いである。**`format.duration` は壊れた WAV や
    ストリーミング由来のファイルで `N/A` になりうるが、sample_rate などは取れている。
    """

    sample_rate: int | None
    channels: int | None
    sample_fmt: str | None
    codec_name: str | None


@dataclass(frozen=True)
class ProbeResult:
    """`probe()` の結果。**失敗も戻り値で表す**（例外を投げない）。"""

    format: AudioFormat | None
    """`None` は「`max_attempts` 回試して取れなかった」。"""

    attempts: int
    error: str | None

    @property
    def ok(self) -> bool:
        return self.format is not None

    @property
    def duration_seconds(self) -> float | None:
        """取れなかった場合も `None`。**呼び手が `ok` と duration を区別せずに済む。**"""
        return None if self.format is None else self.format.duration_seconds


def probe(
    path: Path,
    *,
    ffprobe: Path,
    timeout_seconds: int,
    max_attempts: int = 1,
) -> ProbeResult:
    """音声ファイルの形式を取得する（§10.5(1)）。**例外を投げない。**

    v4.0 以降は inbox（ローカルディスク）を読むため、USB 越しの往復が無い。

    Args:
        path: **inbox 上の**ファイル。デバイス上のパスを渡してはならない（§14.4 N-3）。
        ffprobe: `audio.ffprobe`。
        timeout_seconds: `audio.ffprobe_timeout_seconds`。
        max_attempts: `retry.max_attempts`。`AUDIO_PROBE_FAILED` は再試行可（§15.1）。
    """
    attempts = 0
    error = "未実行"
    for attempts in range(1, max(1, max_attempts) + 1):
        completed, error = _run(path, ffprobe=ffprobe, timeout_seconds=timeout_seconds)
        if completed is None:
            continue
        parsed = parse_output(completed)
        if parsed is None:
            error = "ffprobe の出力を解釈できません"
            continue
        return ProbeResult(format=parsed, attempts=attempts, error=None)
    return ProbeResult(format=None, attempts=attempts, error=error)


def _run(path: Path, *, ffprobe: Path, timeout_seconds: int) -> tuple[str | None, str]:
    """ffprobe を 1 回だけ起動する。`(stdout, エラー説明)` を返す。

    **`stdin` を `DEVNULL` にする。**常駐サービスの標準入力を子プロセスが奪わないようにする。
    §10.5 の「`-nostdin` を付けてはならない」は **ffmpeg が `pipe:0` から WAV を受け取る**
    ための規定であり、ファイルパスを読む ffprobe には当たらない。
    """
    try:
        completed = subprocess.run(  # noqa: S603 - 引数は固定、path は呼び手が検証済み
            [str(ffprobe), *FFPROBE_ARGS, str(path)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return None, f"{timeout_seconds} 秒で応答しません"
    except OSError as exc:  # ffprobe が無い / 実行できない
        return None, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:MAX_ERROR_CHARS]
        return None, f"終了コード {completed.returncode}: {detail}"
    return completed.stdout, ""


def parse_output(stdout: str) -> AudioFormat | None:
    """ffprobe の JSON 出力を読む。解釈できなければ `None`。

    **音声ストリームが 1 本も無いものは失敗とする。**#18 の検証（16000 Hz / 1 ch / `s16`）が
    成り立たず、そのまま進めば ffmpeg が必ず落ちる。ここで失敗にしておけば
    `duration_seconds = NULL` として記録だけは残る。
    """
    try:
        document = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None

    streams = document.get("streams")
    if not isinstance(streams, list) or not streams:
        return None
    stream = streams[0]
    if not isinstance(stream, dict):
        return None

    container = document.get("format")
    duration = container.get("duration") if isinstance(container, dict) else None

    return AudioFormat(
        duration_seconds=_as_float(duration),
        sample_rate=_as_int(stream.get("sample_rate")),
        channels=_as_int(stream.get("channels")),
        sample_fmt=_as_str(stream.get("sample_fmt")),
        codec_name=_as_str(stream.get("codec_name")),
    )


def _as_float(value: object) -> float | None:
    """**ffprobe は数値を文字列で返す**（`"1.500000"`）。`"N/A"` と欠落は `None`。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    # 負の duration は壊れたヘッダ。`ended_at` が開始より前になるので採らない
    return None if parsed < 0 else parsed


def _as_int(value: object) -> int | None:
    parsed = _as_float(value)
    return None if parsed is None else int(parsed)


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# ============================================================
# 変換（§10.5(2)）
# ============================================================

BYTES_PER_SECOND: Final = 32_000
"""16 kHz / 1 ch / s16 のバイトレート（§10.5）。"""

DEFAULT_DURATION_SECONDS: Final = 1800.0
"""`duration_seconds` が `NULL` のときの仮定値（§10.5 の `duration_seconds or 1800`）。"""

NORMALIZED_FILENAME: Final = "audio16k.wav"
TARGET_SAMPLE_RATE: Final = 16000
TARGET_CHANNELS: Final = 1
TARGET_SAMPLE_FMT: Final = "s16"


def staging_dir_for(key: PartKey) -> StagingPath:
    """`/data/staging/<slug>`（§10.5 / §11.2）。`slug = paths.key_slug(partkey)`。"""
    return StagingPath(paths.DATA_ROOT / "staging" / paths.key_slug(key))


def normalized_path_for(key: PartKey) -> StagingPath:
    return StagingPath(Path(staging_dir_for(key)) / NORMALIZED_FILENAME)


def ffmpeg_argv(cfg: Config, *, out_path: Path) -> list[str]:
    """§10.5(2) のコマンドライン。**純関数**（SPEC のコードブロックと照合する）。

    **`-nostdin` を付けてはならない**（§10.5）。標準入力から WAV を受け取るため両立しない。
    サブプロセスが端末の標準入力を奪う問題は、こちらからパイプを与えることで回避されている。
    """
    audio = cfg.audio
    return [
        str(audio.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "wav",
        "-i",
        "pipe:0",
        "-map",
        "0:a:0",
        "-ac",
        str(audio.target_channels),
        "-ar",
        str(audio.target_sample_rate),
        "-c:a",
        audio.target_codec,
        "-f",
        "wav",
        str(out_path),
    ]


def convert_timeout(duration_seconds: float | None, cfg: Config) -> int:
    """`max(ffmpeg_min_timeout_seconds, duration × ffmpeg_timeout_factor)`（§10.5）。

    **`duration_seconds` が `None` なら下限を使う。**`probe()` の `timeout_for()` が上限へ
    倒すのと向きが逆である: あちらは「失敗すると記録が失われる」ので長く待つが、こちらは
    ffmpeg がヘッダから長さを知るので、**長さ不明のまま 6 時間待つ理由が無い。**
    """
    audio = cfg.audio
    if duration_seconds is None:
        return audio.ffmpeg_min_timeout_seconds
    scaled = duration_seconds * audio.ffmpeg_timeout_factor
    return int(max(audio.ffmpeg_min_timeout_seconds, scaled))


# --- 空き容量（§10.5） --------------------------------------------------


@dataclass(frozen=True)
class SpaceCheck:
    """空き容量の 2 条件（§10.5）。"""

    ok: bool
    detail: str
    expected_bytes: int
    free_bytes: int
    staging_bytes: int


def expected_bytes(duration_seconds: float | None) -> int:
    """16 kHz 出力の想定サイズ（§10.5）。`duration` 不明なら 30 分を仮定する。"""
    seconds = DEFAULT_DURATION_SECONDS if duration_seconds is None else duration_seconds
    return int(max(0.0, seconds) * BYTES_PER_SECOND)


def directory_bytes(root: Path) -> int:
    """`du` 相当。読めないものは飛ばす（**例外を投げない**）。"""
    total = 0
    if not root.is_dir():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def check_space(cfg: Config, *, duration_seconds: float | None) -> SpaceCheck:
    """§10.5 の 2 条件を両方見る。

    ```python
    required = expected * free_space_multiplier + free_space_margin_bytes
    if disk_usage(staging_root).free < required:        -> DISK_SPACE_LOW
    if du(staging_root) + expected > staging_max_bytes: -> DISK_SPACE_LOW
    ```

    **`DISK_SPACE_LOW` のときは処理を止めて待つ。元音声は削除しない**（§10.5）。
    """
    settings = cfg.import_
    expected = expected_bytes(duration_seconds)
    required = int(expected * settings.free_space_multiplier + settings.free_space_margin_bytes)
    root = Path(settings.staging_root)
    try:
        free = shutil.disk_usage(root if root.is_dir() else paths.DATA_ROOT).free
    except OSError as exc:
        return SpaceCheck(False, f"空き容量を取得できません: {exc}", expected, 0, 0)

    used = directory_bytes(root)
    if free < required:
        return SpaceCheck(
            False,
            f"空き {free} バイトが必要量 {required} バイトを下回る",
            expected,
            free,
            used,
        )
    if used + expected > settings.staging_max_bytes:
        return SpaceCheck(
            False,
            f"staging 使用量 {used} + 想定 {expected} が上限 {settings.staging_max_bytes} を超える",
            expected,
            free,
            used,
        )
    return SpaceCheck(True, "", expected, free, used)


# --- 変換本体（§10.5(2)） -----------------------------------------------


@dataclass(frozen=True)
class NormalizeResult:
    """`normalize()` の結果。**状態遷移と DB 更新は呼び手が行う**（#21）。"""

    path: StagingPath | None
    sha256: str | None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    elapsed_seconds: float = 0.0
    reused: bool = False
    in_bytes: int = 0
    out_bytes: int = 0

    @property
    def ok(self) -> bool:
        return self.path is not None and self.error_code is None


def normalize(
    source: InboxPath,
    *,
    partkey: PartKey,
    duration_seconds: float | None,
    sha256_helper: str | None,
    cfg: Config,
    claimed_by: str | None = None,
    duplicate_of: Callable[[str], str | None] | None = None,
) -> NormalizeResult:
    """inbox の原本を読みながら 16 kHz WAV を作る（§10.5(2)）。**例外を投げない。**

    Args:
        source: **inbox 上の `_orig`。**デバイス上のパスを渡してはならない（N-3）。
        claimed_by: `staging/<slug>/` を既に使っている行の `partkey`（無ければ `None`）。
            **自分と違えば `IMPORT_FAILED`** — 衝突したまま進むと**別 Part の音声を
            自分のものとして文字起こしし、その結果で §14.1 が真になって元音声を削除する。**
            `key_slug()` は 64 bit なので 3 年分で衝突確率 3.5e-11 だが、
            **確率で安全を担保しない**（§10.5）。
        duplicate_of: 算出した SHA-256 を持つ別の行の `partkey` を返す関数。
            在れば `DUPLICATE_CONTENT` → `SKIPPED`（§10.5 の二重処理防止）。

    **失敗時は部分出力を削除し、inbox の原本にもデバイス上の原本にも一切触らない**（§10.5）。
    """
    out_path = normalized_path_for(partkey)
    if claimed_by is not None and claimed_by != partkey:
        return NormalizeResult(
            path=None,
            sha256=None,
            error_code=ErrorCode.IMPORT_FAILED,
            error_message=(
                f"staging の slug が衝突しています（{paths.key_slug(partkey)} は "
                f"{claimed_by} が使用中）"
            ),
        )

    # **冪等性**: 出力が存在し検証を通れば再実行しない（§10.5）
    verified, _detail = verify_output(Path(out_path), duration_seconds=duration_seconds, cfg=cfg)
    if verified:
        return _reuse(
            Path(source),
            out_path,
            partkey=partkey,
            sha256_helper=sha256_helper,
            duplicate_of=duplicate_of,
            cfg=cfg,
        )

    space = check_space(cfg, duration_seconds=duration_seconds)
    if not space.ok:
        return NormalizeResult(
            path=None,
            sha256=None,
            error_code=ErrorCode.DISK_SPACE_LOW,
            error_message=space.detail,
        )

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    digest, in_bytes, failure = _stream(Path(source), out_path, cfg=cfg, duration=duration_seconds)
    elapsed = time.monotonic() - started

    if failure is not None:
        _discard_output(out_path)
        return NormalizeResult(
            path=None,
            sha256=None,
            error_code=failure[0],
            error_message=failure[1],
            elapsed_seconds=elapsed,
            in_bytes=in_bytes,
        )

    # **コピー検証**: Helper のコピーが壊れていないことを確認する唯一の経路（§10.5）
    if sha256_helper is not None and digest != sha256_helper:
        _discard_output(out_path)
        return NormalizeResult(
            path=None,
            sha256=digest,
            error_code=ErrorCode.SOURCE_HASH_MISMATCH,
            error_message=(
                f"再計算した SHA-256 が .meta.json と一致しません"
                f"（{digest[:16]}… ≠ {sha256_helper[:16]}…）"
            ),
            elapsed_seconds=elapsed,
            in_bytes=in_bytes,
        )

    verified, detail = verify_output(Path(out_path), duration_seconds=duration_seconds, cfg=cfg)
    if not verified:
        _discard_output(out_path)
        return NormalizeResult(
            path=None,
            sha256=digest,
            error_code=ErrorCode.NORMALIZE_VERIFY_FAILED,
            error_message=detail,
            elapsed_seconds=elapsed,
            in_bytes=in_bytes,
        )

    # **二重処理防止**: 内容が同一の既処理ファイル（§10.5）
    if duplicate_of is not None:
        other = duplicate_of(digest)
        if other is not None and other != partkey:
            _discard_output(out_path)
            return NormalizeResult(
                path=None,
                sha256=digest,
                error_code=ErrorCode.DUPLICATE_CONTENT,
                error_message=f"同じ内容の Part が既にあります: {other}",
                elapsed_seconds=elapsed,
                in_bytes=in_bytes,
            )

    return NormalizeResult(
        path=out_path,
        sha256=digest,
        elapsed_seconds=elapsed,
        in_bytes=in_bytes,
        out_bytes=Path(out_path).stat().st_size,
    )


def _reuse(
    source: Path,
    out_path: StagingPath,
    *,
    partkey: PartKey,
    sha256_helper: str | None,
    duplicate_of: Callable[[str], str | None] | None,
    cfg: Config,
) -> NormalizeResult:
    """既にある 16 kHz 出力を再利用する（§10.5 の冪等性）。

    **それでも入力の SHA-256 は出す**（v5.46→v5.47 の変更 BI-3）。v5.46 までここは
    `sha256=None` を返し、呼び手がそれを `recordings.sha256` へ**そのまま書いていた**。
    結果として、

    - `recording_by_sha256()` が二度と一致せず、**同じ音声の再コピーが 2 度処理される**
    - §10.5 の**コピー整合検査（`sha256_helper` との照合）が恒久的に飛ばされる**

    **「まだ計算していない」を「無い」として書き込んでいた。**

    到達するのは、正規化に成功してから `update_recording()` の前に落ちた場合である
    （`recover_interrupted()` は `normalized_path` が `NULL` なので消す対象を見つけられず、
    健全な出力が残ったまま行だけ `DISCOVERED` へ巻き戻る）。

    **入力は必ず在る。**呼び手が `_usable_source()` で確かめてから来る（§9.3）。
    """
    try:
        digest, in_bytes = _digest_file(source, cfg=cfg)
    except OSError as exc:
        return NormalizeResult(
            path=None,
            sha256=None,
            error_code=ErrorCode.IMPORT_FAILED,
            error_message=f"{type(exc).__name__}: {exc}",
        )

    # **コピー検証**（§10.5）。再実行した場合と同じ判定にする
    if sha256_helper is not None and digest != sha256_helper:
        _discard_output(out_path)
        return NormalizeResult(
            path=None,
            sha256=digest,
            error_code=ErrorCode.SOURCE_HASH_MISMATCH,
            error_message=(
                f"再計算した SHA-256 が .meta.json と一致しません"
                f"（{digest[:16]}… ≠ {sha256_helper[:16]}…）"
            ),
            in_bytes=in_bytes,
        )

    # **二重処理防止**（§10.5）。**飛ばせない** —— `idx_recordings_sha` は
    # `sha256` の部分 UNIQUE なので、衝突する値を書くと `update_recording()` が落ちる
    if duplicate_of is not None:
        other = duplicate_of(digest)
        if other is not None and other != partkey:
            _discard_output(out_path)
            return NormalizeResult(
                path=None,
                sha256=digest,
                error_code=ErrorCode.DUPLICATE_CONTENT,
                error_message=f"同じ内容の Part が既にあります: {other}",
                in_bytes=in_bytes,
            )

    return NormalizeResult(
        path=out_path,
        sha256=digest,
        reused=True,
        in_bytes=in_bytes,
        out_bytes=Path(out_path).stat().st_size,
    )


def _digest_file(path: Path, *, cfg: Config) -> tuple[str, int]:
    """ファイル全体の SHA-256 と読んだバイト数。**`_stream()` と同じ値を出すこと。**

    刻み幅は `_stream()` と同じ `import.hash_chunk_bytes` を使う。
    """
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as handle:
        while chunk := handle.read(cfg.import_.hash_chunk_bytes):
            digest.update(chunk)
            read += len(chunk)
    return digest.hexdigest(), read


def _stream(
    source: Path,
    out_path: StagingPath,
    *,
    cfg: Config,
    duration: float | None,
) -> tuple[str, int, tuple[ErrorCode, str] | None]:
    """読みながら SHA-256 を算出し、そのまま ffmpeg へ流す（§10.5(2)）。

    **USB が途中で切断された場合**は `OSError` / `FileNotFoundError` を捕まえて
    `IMPORT_FAILED` にする。**部分出力は呼び手が必ず削除する。**
    """
    argv = ffmpeg_argv(cfg, out_path=Path(out_path))
    timeout = convert_timeout(duration, cfg)
    digest = hashlib.sha256()
    read = 0

    try:
        process = subprocess.Popen(  # noqa: S603 - argv はリスト。shell=True を使わない
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return digest.hexdigest(), 0, (ErrorCode.IMPORT_FAILED, f"{type(exc).__name__}: {exc}")

    broken = None
    try:
        assert process.stdin is not None  # noqa: S101 - PIPE を指定しているので必ず在る
        with source.open("rb") as handle:
            while chunk := handle.read(cfg.import_.hash_chunk_bytes):
                digest.update(chunk)
                read += len(chunk)
                try:
                    process.stdin.write(chunk)
                except (BrokenPipeError, OSError, ValueError) as exc:
                    # ffmpeg が先に落ちた。**残りを読まない**（USB 越しの無駄な読み込みを避ける）
                    broken = f"{type(exc).__name__}: {exc}"
                    break
    except OSError as exc:
        _kill(process)
        return digest.hexdigest(), read, (ErrorCode.IMPORT_FAILED, f"{type(exc).__name__}: {exc}")
    finally:
        # **`ValueError` も飲む。**ffmpeg が先に落ちていると `close()` の flush が
        # 「flush of closed file」で失敗する。ここで例外にすると、元の失敗原因
        # （ffmpeg の stderr）が見えなくなる
        with contextlib.suppress(OSError, ValueError):
            if process.stdin is not None:
                process.stdin.close()
        # **`communicate()` に stdin を触らせない。**閉じたあとも参照が残っていると
        # `communicate()` が自分で `flush()` を呼び、`ValueError: flush of closed file`
        # で落ちる（実際に落ちた）。**ffmpeg の stderr を読む前に例外になるので、
        # 失敗の本当の原因が見えなくなる。**
        process.stdin = None

    try:
        _stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(process)
        process.communicate()
        return digest.hexdigest(), read, (ErrorCode.IMPORT_FAILED, f"{timeout} 秒を超えました")

    if process.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace").strip()[-MAX_ERROR_CHARS:]
        reason = f"ffmpeg 終了コード {process.returncode}: {tail}"
        return digest.hexdigest(), read, (ErrorCode.IMPORT_FAILED, reason)
    if broken is not None:
        return digest.hexdigest(), read, (ErrorCode.IMPORT_FAILED, broken)
    return digest.hexdigest(), read, None


def _kill(process: subprocess.Popen[bytes]) -> None:
    """プロセスグループごと落とす。ffmpeg は子を持ちうる。"""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


def verify_output(path: Path, *, duration_seconds: float | None, cfg: Config) -> tuple[bool, str]:
    """§10.5 の検証。`(合否, 説明)` を返す。**例外を投げない。**

    存在し `size > 0`、ffprobe で 16000 Hz / 1 ch / `s16`、かつ**出力の duration が
    入力の duration と ±`duration_tolerance_seconds` 以内。**

    **長さの照合を省くと、途中で切れた出力を「変換済み」として通してしまう。**
    そのまま文字起こしすると、**録音の後半が無いノートで §14.1 が真になる。**
    """
    if not path.is_file():
        return False, f"{path} がありません"
    if path.stat().st_size == 0:
        return False, f"{path} が 0 バイトです"

    result = probe(
        path,
        ffprobe=cfg.audio.ffprobe,
        timeout_seconds=cfg.audio.ffprobe_timeout_seconds,
        max_attempts=1,
    )
    if result.format is None:
        return False, f"出力を ffprobe で読めません: {result.error}"
    fmt = result.format
    if fmt.sample_rate != cfg.audio.target_sample_rate:
        return False, f"sample_rate が {fmt.sample_rate}（期待 {cfg.audio.target_sample_rate}）"
    if fmt.channels != cfg.audio.target_channels:
        return False, f"channels が {fmt.channels}（期待 {cfg.audio.target_channels}）"
    if fmt.sample_fmt != TARGET_SAMPLE_FMT:
        return False, f"sample_fmt が {fmt.sample_fmt}（期待 {TARGET_SAMPLE_FMT}）"

    if duration_seconds is None or fmt.duration_seconds is None:
        # **入力の長さが不明なら長さの照合を飛ばす。**ffprobe 失敗は §10.5 が
        # 支える恒久状態であり（duration = NULL）、ここで落とすと変換が永久にできない
        return True, ""
    gap = abs(fmt.duration_seconds - duration_seconds)
    if gap > cfg.audio.duration_tolerance_seconds:
        return False, (
            f"長さが入力と {gap:.2f} 秒ずれています"
            f"（許容 {cfg.audio.duration_tolerance_seconds} 秒）"
        )
    return True, ""


def _discard_output(path: StagingPath) -> None:
    """部分出力を削除する。**`paths.safe_unlink_staging()` を通す**（§14.4 N-10）。

    **残すと次回の再開で「変換済み」と誤認する**（§10.5）。
    """
    with contextlib.suppress(OSError, ValueError):
        paths.safe_unlink_staging(path, missing_ok=True)


def release_inbox(source: InboxPath, cfg: Config) -> bool:
    """変換成功後に inbox の原本を削除する（§10.5 の `inbox_retain`）。

    **`<name>.wav.meta.json` は残す**（§10.2 の墓標。消すと次の Helper 起動で再コピーされる）。

    **`normalize()` から呼ばない。**§9.3 はこれを `NORMALIZING → NORMALIZED` の副作用と
    しており、**DB 更新が成功して初めて `NORMALIZED` である。**先に消すと、DB 更新に
    失敗した場合に「原本も 16 kHz も無い」状態になりうる。呼び手（#21）が順序を決める。
    """
    if cfg.import_.inbox_retain != "normalized":
        return False
    try:
        paths.safe_unlink_inbox(source, missing_ok=True)
    except (OSError, ValueError):
        return False
    return True
