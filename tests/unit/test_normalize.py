"""`voicedock.audio.normalize` が SPEC §10.5(2) のとおりに変換することを固定する。

**whisper.cpp の WAV リーダは 16 kHz / モノラル / 16 bit PCM しか受け付けない。**DJI Mic 3 は
48 kHz / 24 bit または 32 bit float で記録するので、**この変換なしでは必ず失敗する。**

**ここが「Helper のコピーが壊れていないことを確認する唯一の経路」である**（§10.5）。
読みながら算出した SHA-256 を `.meta.json` の値と照合する。ここを飛ばすと、**壊れた
コピーを文字起こしした結果で §14.1 が真になり、デバイス上の原本が消える。**

実物の `ffmpeg` が要るテストは `needs_ffmpeg`（CI には無い）。それ以外は偽 ffmpeg で動かす。
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

from tests.fixtures.make_wav import Content, WavFormat, write_wav
from tests.spec_sync import spec_section_code
from voicedock import audio, paths
from voicedock.audio import (
    BYTES_PER_SECOND,
    DEFAULT_DURATION_SECONDS,
    NormalizeResult,
    check_space,
    convert_timeout,
    expected_bytes,
    ffmpeg_argv,
    normalize,
    normalized_path_for,
    release_inbox,
    staging_dir_for,
    verify_output,
)
from voicedock.config import Config
from voicedock.errors import ErrorCode
from voicedock.paths import DevicePath, InboxPath, PartKey, partkey_for

RELPATH = "TX_MIC001_20260829_071201/TX01_MIC002_20260829_071204_orig.wav"
PARTKEY: PartKey = partkey_for("DJIMIC3", DevicePath(PurePosixPath(RELPATH)))
SECONDS = 2.0


@pytest.fixture(autouse=True)
def _roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`/data` と `/inbox` を `tmp_path` 配下へ寄せる。

    `paths.safe_unlink_staging()` は `/data` 配下しか消さない（§11.2）。**本番のコードに
    上書きの入口を作らない**方針なので、差し替えはテスト側で行う。
    """
    data = tmp_path / "data"
    (data / "staging").mkdir(parents=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(paths, "DATA_ROOT", data)
    monkeypatch.setattr(paths, "INBOX_ROOT", inbox)


@pytest.fixture
def source(tmp_path: Path) -> InboxPath:
    """inbox 上の 48 kHz / 24 bit WAV（実機の形式。`docs/POC.md` §6.2）。"""
    path = tmp_path / "inbox" / "DJIMIC3" / RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    write_wav(path, seconds=SECONDS, fmt=WavFormat.PCM24, content=Content.SPEECH)
    return InboxPath(path)


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fake_ffmpeg(
    tmp_path: Path,
    *,
    exit_code: int = 0,
    stderr: str = "",
    read_stdin: bool = True,
    payload: Path | None = None,
) -> Path:
    """`-i pipe:0` から読み、最後の引数へ WAV を書く偽 ffmpeg。

    **`tmp_path` 直下へ置かない。**`tests/helpers.complete_tree()` が `whisper-cli` を
    置くのと同じ事故（実行可能ビットだけ残った空スクリプトへの差し替え）を避ける。
    """
    folder = tmp_path / "fake"
    folder.mkdir(exist_ok=True)
    script = folder / "ffmpeg"
    body = [
        "#!/bin/sh",
        f"printf '%s\\n' \"$@\" > '{script}.argv'",
        "out=''",
        'for arg in "$@"; do out="$arg"; done',
    ]
    body.append("cat > /dev/null" if read_stdin else "")
    if stderr:
        body.append(f">&2 printf %s '{stderr}'")
    if payload is not None:
        body.append(f"cp '{payload}' \"$out\"")
    body.append(f"exit {exit_code}")
    script.write_text("\n".join(body) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def cfg_with(make_config: Callable[..., Config], tmp_path: Path, **patch: object) -> Config:
    audio_patch: dict[str, object] = {}
    for key in ("ffmpeg", "ffprobe", "duration_tolerance_seconds", "ffmpeg_min_timeout_seconds"):
        if key in patch:
            audio_patch[key] = patch.pop(key)
    document: dict[str, object] = {
        "import": {"staging_root": str(tmp_path / "data" / "staging"), **patch},
    }
    if audio_patch:
        document["audio"] = audio_patch
    return make_config(document)


@pytest.fixture
def cfg(make_config: Callable[..., Config], tmp_path: Path) -> Config:
    return cfg_with(make_config, tmp_path)


# --- SPEC 整合（§10.5(2)） ----------------------------------------------


def test_the_argv_matches_the_spec(cfg: Config) -> None:
    """§10.5(2) の `subprocess.Popen` の argv と一致すること。

    **SPEC のオプションを 1 つ変えたらここが落ちる。**`-ac 1` を落とすと 2 ch のまま
    出力され、whisper.cpp が読めない。
    """
    block = spec_section_code("10.5", "python")
    listed = block[block.index("[cfg.audio.ffmpeg") : block.index("stdin=")]
    expected = re.findall(r'"([^"]+)"', listed)

    argv = ffmpeg_argv(cfg, out_path=Path("/data/staging/x/audio16k.wav"))
    # 先頭は `cfg.audio.ffmpeg`、末尾は `str(out_path)` で SPEC では式なので落とす
    assert argv[1:-1] == expected


def test_no_nostdin_flag(cfg: Config) -> None:
    """**`-nostdin` を付けてはならない**（§10.5）。

    標準入力から WAV を受け取るため両立しない。付けると ffmpeg が `pipe:0` を
    `/dev/null` として開き、**常に「入力が空」で失敗する。**
    """
    assert "-nostdin" not in ffmpeg_argv(cfg, out_path=Path("out.wav"))


def test_the_target_format_comes_from_the_configuration(
    make_config: Callable[..., Config], tmp_path: Path
) -> None:
    argv = ffmpeg_argv(cfg_with(make_config, tmp_path), out_path=Path("out.wav"))
    assert argv[argv.index("-ar") + 1] == "16000"
    assert argv[argv.index("-ac") + 1] == "1"
    assert argv[argv.index("-c:a") + 1] == "pcm_s16le"


def test_the_staging_path_uses_the_key_slug(cfg: Config) -> None:
    """`/data/staging/<slug>/audio16k.wav`（§10.5 / §11.2）。

    **`partkey` をそのままパスにしない。**`/` と空白を含みうる（§5.4）。
    """
    assert Path(staging_dir_for(PARTKEY)).name == paths.key_slug(PARTKEY)
    assert Path(normalized_path_for(PARTKEY)).name == "audio16k.wav"


# --- タイムアウト（§10.5） ----------------------------------------------


def test_the_timeout_is_scaled_by_duration(cfg: Config) -> None:
    """`max(ffmpeg_min_timeout_seconds, duration × ffmpeg_timeout_factor)`（§10.5）。"""
    minimum = cfg.audio.ffmpeg_min_timeout_seconds
    assert convert_timeout(1.0, cfg) == minimum, "下限"
    long = minimum / cfg.audio.ffmpeg_timeout_factor * 10
    assert convert_timeout(long, cfg) == int(long * cfg.audio.ffmpeg_timeout_factor)


def test_an_unknown_duration_uses_the_minimum(cfg: Config) -> None:
    """**`duration` 不明なら下限。**`probe()` の `timeout_for()` とは向きが逆である。

    あちらは「失敗すると記録が失われる」ので上限へ倒すが、こちらは ffmpeg が
    ヘッダから長さを知るので、**長さ不明のまま 6 時間待つ理由が無い。**
    """
    assert convert_timeout(None, cfg) == cfg.audio.ffmpeg_min_timeout_seconds


# --- 空き容量（§10.5） --------------------------------------------------


def test_expected_bytes_uses_the_spec_rate() -> None:
    """16 kHz / 1 ch / s16 = 32,000 B/秒（§10.5）。"""
    assert BYTES_PER_SECOND == 32_000
    assert expected_bytes(1800.0) == 1800 * 32_000


def test_an_unknown_duration_assumes_thirty_minutes() -> None:
    assert DEFAULT_DURATION_SECONDS == 1800.0
    assert expected_bytes(None) == expected_bytes(DEFAULT_DURATION_SECONDS)


def test_enough_space_passes(cfg: Config) -> None:
    assert check_space(cfg, duration_seconds=1.0).ok


def test_the_margin_is_enforced(make_config: Callable[..., Config], tmp_path: Path) -> None:
    """`expected × multiplier + margin` を満たさなければ `DISK_SPACE_LOW`（§10.5）。"""
    cfg = cfg_with(make_config, tmp_path, free_space_multiplier=10.0**12)
    check = check_space(cfg, duration_seconds=1.0)
    assert not check.ok
    assert "必要量" in check.detail


def test_the_staging_cap_is_enforced(make_config: Callable[..., Config], tmp_path: Path) -> None:
    cfg = cfg_with(
        make_config,
        tmp_path,
        staging_max_bytes=1024,
        free_space_margin_bytes=512,
    )
    check = check_space(cfg, duration_seconds=1800.0)
    assert not check.ok
    assert "上限" in check.detail


def test_disk_space_low_leaves_the_source(
    cfg_low: Config, source: InboxPath, tmp_path: Path
) -> None:
    """**`DISK_SPACE_LOW` のときは処理を止めて待つ。元音声は削除しない**（§10.5）。"""
    result = normalize(
        source,
        partkey=PARTKEY,
        duration_seconds=SECONDS,
        sha256_helper=None,
        cfg=cfg_low,
    )
    assert result.error_code is ErrorCode.DISK_SPACE_LOW
    assert Path(source).is_file()
    assert not Path(normalized_path_for(PARTKEY)).exists()


@pytest.fixture
def cfg_low(make_config: Callable[..., Config], tmp_path: Path) -> Config:
    """空き容量が必ず足りない設定。

    **`free_space_margin_bytes` を巨大にしてはならない。**V-22 が
    `staging_max_bytes > free_space_margin_bytes` を要求しているので設定検証で落ちる。
    `free_space_multiplier` を上げるほうが規則に触れない。
    """
    return cfg_with(make_config, tmp_path, free_space_multiplier=10.0**12)


# --- 正常系（偽 ffmpeg） ------------------------------------------------


@pytest.fixture
def good_output(tmp_path: Path) -> Path:
    """偽 ffmpeg が書く「正しい」16 kHz / 1 ch / s16 の WAV。"""
    path = tmp_path / "fake" / "good16k.wav"
    path.parent.mkdir(exist_ok=True)
    write_wav(path, seconds=SECONDS, fmt=WavFormat.PCM16, content=Content.SPEECH, sample_rate=16000)
    return path


@pytest.mark.needs_ffmpeg
def test_a_real_conversion_produces_the_target_format(cfg: Config, source: InboxPath) -> None:
    """**実物の ffmpeg で 48 kHz / 24 bit → 16 kHz / 1 ch / s16 になること。**"""
    result = normalize(
        source,
        partkey=PARTKEY,
        duration_seconds=SECONDS,
        sha256_helper=sha_of(Path(source)),
        cfg=cfg,
    )
    assert result.ok, result.error_message
    assert result.path is not None
    verified, detail = verify_output(Path(result.path), duration_seconds=SECONDS, cfg=cfg)
    assert verified, detail
    assert result.sha256 == sha_of(Path(source))
    assert result.in_bytes == Path(source).stat().st_size
    assert result.out_bytes > 0


@pytest.mark.needs_ffmpeg
def test_the_source_is_read_exactly_once(cfg: Config, source: InboxPath) -> None:
    """**USB の読み込みは 1 回だけ**（§10.5）。

    読みながらハッシュを算出し、そのまま ffmpeg へ流す。ハッシュ用にもう 1 回読むと、
    **30 分の録音で 345 MB を 2 回読む。**
    """
    result = normalize(
        source,
        partkey=PARTKEY,
        duration_seconds=SECONDS,
        sha256_helper=None,
        cfg=cfg,
    )
    assert result.in_bytes == Path(source).stat().st_size, "入力を 1 回だけ読んでいない"


@pytest.mark.needs_ffmpeg
def test_the_original_is_never_touched(cfg: Config, source: InboxPath) -> None:
    """**inbox の原本にもデバイス上の原本にも一切触らない**（§10.5）。"""
    before = Path(source).read_bytes()
    normalize(source, partkey=PARTKEY, duration_seconds=SECONDS, sha256_helper=None, cfg=cfg)
    assert Path(source).read_bytes() == before


# --- コピー検証（§10.5。最重要） ----------------------------------------


@pytest.mark.needs_ffmpeg
def test_a_hash_mismatch_is_source_hash_mismatch(cfg: Config, source: InboxPath) -> None:
    """**Helper のコピーが壊れていないことを確認する唯一の経路**（§10.5）。

    ここを飛ばすと、**壊れたコピーを文字起こしした結果で §14.1 が真になり、
    デバイス上の原本が消える。**
    """
    result = normalize(
        source,
        partkey=PARTKEY,
        duration_seconds=SECONDS,
        sha256_helper="0" * 64,
        cfg=cfg,
    )
    assert result.error_code is ErrorCode.SOURCE_HASH_MISMATCH
    assert result.path is None


@pytest.mark.needs_ffmpeg
def test_a_hash_mismatch_removes_the_output_but_keeps_the_source(
    cfg: Config, source: InboxPath
) -> None:
    """§9.3: 出力削除。**inbox の原本も残す**（再コピーの判断材料）。"""
    normalize(
        source,
        partkey=PARTKEY,
        duration_seconds=SECONDS,
        sha256_helper="0" * 64,
        cfg=cfg,
    )
    assert not Path(normalized_path_for(PARTKEY)).exists()
    assert Path(source).is_file()


@pytest.mark.needs_ffmpeg
def test_no_helper_hash_skips_the_comparison(cfg: Config, source: InboxPath) -> None:
    """`sha256_helper` が `None`（`.meta.json` が古い）なら照合を飛ばす。

    **照合できないことを失敗にしない** — 記録を残すほうを選ぶ（§1.3）。
    """
    assert normalize(
        source, partkey=PARTKEY, duration_seconds=SECONDS, sha256_helper=None, cfg=cfg
    ).ok


# --- 出力検証（§10.5） --------------------------------------------------


@pytest.mark.needs_ffmpeg
def test_a_duration_mismatch_fails_verification(
    make_config: Callable[..., Config], tmp_path: Path, source: InboxPath
) -> None:
    """**出力の duration が入力と ±`duration_tolerance_seconds` 以内**（§10.5）。

    照合を省くと、**途中で切れた出力を「変換済み」として通す。**そのまま文字起こしすると
    **録音の後半が無いノートで §14.1 が真になる。**
    """
    cfg = cfg_with(make_config, tmp_path, duration_tolerance_seconds=0.01)
    result = normalize(
        source,
        partkey=PARTKEY,
        duration_seconds=SECONDS + 60.0,  # 実際より 60 秒長いと申告する
        sha256_helper=None,
        cfg=cfg,
    )
    assert result.error_code is ErrorCode.NORMALIZE_VERIFY_FAILED
    assert result.error_message is not None
    assert "長さ" in result.error_message
    assert not Path(normalized_path_for(PARTKEY)).exists(), "出力が残っている"


@pytest.mark.needs_ffmpeg
def test_an_unknown_input_duration_skips_the_length_check(cfg: Config, source: InboxPath) -> None:
    """**入力の長さが不明なら長さの照合を飛ばす。**

    ffprobe 失敗は §10.5 が支える恒久状態（duration = NULL）であり、ここで落とすと
    **変換が永久にできない。**
    """
    assert normalize(source, partkey=PARTKEY, duration_seconds=None, sha256_helper=None, cfg=cfg).ok


@pytest.mark.needs_ffmpeg
def test_a_wrong_sample_rate_fails_verification(
    cfg: Config, source: InboxPath, tmp_path: Path
) -> None:
    """48 kHz のまま出力されたら不合格（whisper.cpp が読めない）。"""
    wrong = tmp_path / "wrong.wav"
    write_wav(wrong, seconds=SECONDS, fmt=WavFormat.PCM16, content=Content.SPEECH)
    verified, detail = verify_output(wrong, duration_seconds=SECONDS, cfg=cfg)
    assert not verified
    assert "sample_rate" in detail


def test_a_missing_output_fails_verification(cfg: Config, tmp_path: Path) -> None:
    verified, detail = verify_output(tmp_path / "absent.wav", duration_seconds=1.0, cfg=cfg)
    assert not verified
    assert "ありません" in detail


def test_an_empty_output_fails_verification(cfg: Config, tmp_path: Path) -> None:
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    verified, detail = verify_output(empty, duration_seconds=1.0, cfg=cfg)
    assert not verified
    assert "0 バイト" in detail


# --- 失敗（偽 ffmpeg） --------------------------------------------------


def test_a_nonzero_exit_removes_the_output(
    make_config: Callable[..., Config], tmp_path: Path, source: InboxPath, good_output: Path
) -> None:
    cfg = cfg_with(
        make_config, tmp_path, ffmpeg=str(fake_ffmpeg(tmp_path, exit_code=1, stderr="boom"))
    )
    result = normalize(
        source, partkey=PARTKEY, duration_seconds=SECONDS, sha256_helper=None, cfg=cfg
    )
    assert result.error_code is ErrorCode.IMPORT_FAILED
    assert result.error_message is not None
    assert "boom" in result.error_message
    assert not Path(normalized_path_for(PARTKEY)).exists()
    assert Path(source).is_file()


def test_a_missing_source_is_import_failed(
    make_config: Callable[..., Config], tmp_path: Path, good_output: Path
) -> None:
    """**USB が途中で切断された場合**（§10.5）。`FileNotFoundError` を捕まえる。"""
    cfg = cfg_with(make_config, tmp_path, ffmpeg=str(fake_ffmpeg(tmp_path, payload=good_output)))
    absent = InboxPath(tmp_path / "inbox" / "DJIMIC3" / "absent.wav")
    result = normalize(
        absent, partkey=PARTKEY, duration_seconds=SECONDS, sha256_helper=None, cfg=cfg
    )
    assert result.error_code is ErrorCode.IMPORT_FAILED


def test_a_missing_ffmpeg_is_import_failed(
    make_config: Callable[..., Config], tmp_path: Path, source: InboxPath
) -> None:
    cfg = cfg_with(make_config, tmp_path, ffmpeg=str(tmp_path / "fake" / "absent"))
    result = normalize(
        source, partkey=PARTKEY, duration_seconds=SECONDS, sha256_helper=None, cfg=cfg
    )
    assert result.error_code is ErrorCode.IMPORT_FAILED


def test_the_partial_output_goes_through_safe_unlink_staging(
    make_config: Callable[..., Config],
    tmp_path: Path,
    source: InboxPath,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部分出力の削除が **`paths.safe_unlink_staging()` 経由**であること（§14.4 N-10）。

    直接 `unlink` を書くと `test_no_device_delete.py` が落ちる。ここでは
    「実際にその関数を通っている」ことを見る。
    """
    cfg = cfg_with(make_config, tmp_path, ffmpeg=str(fake_ffmpeg(tmp_path, exit_code=2)))
    seen: list[Path] = []
    real = paths.safe_unlink_staging

    def spy(path: Path, **kwargs: object) -> None:
        seen.append(Path(path))
        real(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(paths, "safe_unlink_staging", spy)
    normalize(source, partkey=PARTKEY, duration_seconds=SECONDS, sha256_helper=None, cfg=cfg)
    assert seen == [Path(normalized_path_for(PARTKEY))]


def test_the_argv_is_a_list(
    make_config: Callable[..., Config], tmp_path: Path, source: InboxPath, good_output: Path
) -> None:
    """**`shell=True` と文字列連結を使わない**（§14.4）。"""
    script = fake_ffmpeg(tmp_path, payload=good_output)
    cfg = cfg_with(make_config, tmp_path, ffmpeg=str(script))
    normalize(source, partkey=PARTKEY, duration_seconds=SECONDS, sha256_helper=None, cfg=cfg)
    argv = Path(f"{script}.argv").read_text(encoding="utf-8").splitlines()
    assert len(argv) > 10, "argv が 1 本の文字列として渡っている"


# --- slug 衝突（§10.5） -------------------------------------------------


def test_a_slug_collision_is_import_failed(cfg: Config, source: InboxPath) -> None:
    """**確率で安全を担保しない**（§10.5）。

    衝突したまま進むと、**別 Part の `audio16k.wav` を自分のものとして文字起こしし、
    その結果で §14.1 が真になって元音声を削除する。**
    """
    result = normalize(
        source,
        partkey=PARTKEY,
        duration_seconds=SECONDS,
        sha256_helper=None,
        cfg=cfg,
        claimed_by="DJIMIC3/other/other_orig.wav",
    )
    assert result.error_code is ErrorCode.IMPORT_FAILED
    assert result.error_message is not None
    assert "衝突" in result.error_message


@pytest.mark.needs_ffmpeg
def test_our_own_claim_is_not_a_collision(cfg: Config, source: InboxPath) -> None:
    assert normalize(
        source,
        partkey=PARTKEY,
        duration_seconds=SECONDS,
        sha256_helper=None,
        cfg=cfg,
        claimed_by=PARTKEY,
    ).ok


# --- 二重処理防止（§10.5） ----------------------------------------------


@pytest.mark.needs_ffmpeg
def test_duplicate_content_is_skipped(cfg: Config, source: InboxPath) -> None:
    """同じ SHA-256 の行が在れば `DUPLICATE_CONTENT`（§10.5）。出力を削除する。"""
    result = normalize(
        source,
        partkey=PARTKEY,
        duration_seconds=SECONDS,
        sha256_helper=None,
        cfg=cfg,
        duplicate_of=lambda _digest: "DJIMIC3/other/other_orig.wav",
    )
    assert result.error_code is ErrorCode.DUPLICATE_CONTENT
    assert not Path(normalized_path_for(PARTKEY)).exists()
    assert Path(source).is_file()


@pytest.mark.needs_ffmpeg
def test_our_own_sha_is_not_a_duplicate(cfg: Config, source: InboxPath) -> None:
    """**自分自身は重複ではない。**再実行で `SKIPPED` へ落ちると記録が失われる。"""
    assert normalize(
        source,
        partkey=PARTKEY,
        duration_seconds=SECONDS,
        sha256_helper=None,
        cfg=cfg,
        duplicate_of=lambda _digest: PARTKEY,
    ).ok


# --- 冪等性（§10.5） ----------------------------------------------------


@pytest.mark.needs_ffmpeg
def test_a_verified_output_is_reused(cfg: Config, source: InboxPath) -> None:
    """出力が存在し検証を通れば再実行しない（§10.5）。"""
    first = normalize(
        source, partkey=PARTKEY, duration_seconds=SECONDS, sha256_helper=None, cfg=cfg
    )
    assert first.ok
    again = normalize(
        source, partkey=PARTKEY, duration_seconds=SECONDS, sha256_helper=None, cfg=cfg
    )
    assert again.reused
    assert again.ok
    assert again.sha256 is None, "再利用時はハッシュを算出しない（入力を読まない）"


@pytest.mark.needs_ffmpeg
def test_a_broken_output_is_regenerated(
    make_config: Callable[..., Config], tmp_path: Path, source: InboxPath, good_output: Path
) -> None:
    """**壊れた出力は「無い」と同じ扱い。**残すと次回「変換済み」と誤認する（§10.5）。

    偽 ffmpeg で動かすが、**壊れた出力の判定には実物の `ffprobe` が要る**
    （`verify_output()` が呼ぶ）。CI には無いので `needs_ffmpeg` を付ける。
    """
    target = Path(normalized_path_for(PARTKEY))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"not a wav")
    cfg = cfg_with(make_config, tmp_path, ffmpeg=str(fake_ffmpeg(tmp_path, payload=good_output)))
    result = normalize(
        source, partkey=PARTKEY, duration_seconds=SECONDS, sha256_helper=None, cfg=cfg
    )
    assert not result.reused
    assert result.ok, result.error_message


# --- inbox の寿命（§10.5 の `inbox_retain`） ----------------------------


def test_release_inbox_removes_the_wav_and_keeps_the_tombstone(
    cfg: Config, source: InboxPath
) -> None:
    """**`<name>.wav.meta.json` は残す**（§10.2 の墓標）。

    消すと次の Helper 起動で**再コピーされる**（3 日分なら 5 分ごとに約 50 GB）。
    """
    meta = Path(f"{source}.meta.json")
    meta.write_text("{}", encoding="utf-8")
    assert release_inbox(source, cfg)
    assert not Path(source).exists()
    assert meta.is_file()


def test_release_inbox_respects_raw_saved(
    make_config: Callable[..., Config], tmp_path: Path, source: InboxPath
) -> None:
    """`inbox_retain: raw_saved` なら変換直後には消さない（§10.5）。"""
    cfg = cfg_with(make_config, tmp_path, inbox_retain="raw_saved")
    assert not release_inbox(source, cfg)
    assert Path(source).is_file()


def test_normalize_does_not_release_the_inbox(
    make_config: Callable[..., Config], tmp_path: Path, source: InboxPath, good_output: Path
) -> None:
    """**`normalize()` は inbox を消さない。**

    §9.3 はこれを `NORMALIZING → NORMALIZED` の副作用としており、**DB 更新が成功して
    初めて `NORMALIZED` である。**先に消すと、DB 更新に失敗した場合に
    「原本も 16 kHz も無い」状態になりうる。
    """
    cfg = cfg_with(make_config, tmp_path, ffmpeg=str(fake_ffmpeg(tmp_path, payload=good_output)))
    assert cfg.import_.inbox_retain == "normalized"
    normalize(source, partkey=PARTKEY, duration_seconds=SECONDS, sha256_helper=None, cfg=cfg)
    assert Path(source).is_file(), "normalize が inbox を消した"


# --- 補助 ----------------------------------------------------------------


def test_directory_bytes_tolerates_unreadable_entries(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ではディレクトリの読み取り拒否が成立しない")
    root = tmp_path / "du"
    (root / "sub").mkdir(parents=True)
    (root / "a").write_bytes(b"x" * 10)
    (root / "sub" / "b").write_bytes(b"y" * 5)
    assert audio.directory_bytes(root) == 15


def test_normalize_result_ok_requires_both(tmp_path: Path) -> None:
    assert not NormalizeResult(path=None, sha256="x").ok
    assert not NormalizeResult(path=None, sha256=None, error_code=ErrorCode.IMPORT_FAILED).ok
