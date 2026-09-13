"""`scripts/fetch-models.sh` が SPEC §18.6 の規定どおりであることを固定する。

**モデルの取得は 1 GB のダウンロードを伴うので、テストでは実行しない。**代わりに
(1) SPEC の規定（取得元 URL / 既定のモデル名 / `chown 1000:1000`）との一致、
(2) `bash -n` による構文検査、
(3) **モデル名の検証**（`/` や `..` を渡したときに拒否すること）を見る。

**(3) が実害に直結する。**モデル名はそのまま URL とファイル名へ入るので、`../` を
通すと named volume の外へ書きうる。`docker run` の中で動くので被害は volume の
マウント先に限られるが、**`/models/whisper` の外は書かせない。**
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

import yaml

from tests.helpers import REPO_ROOT
from tests.spec_sync import spec_section_code, spec_text

SCRIPT = REPO_ROOT / "scripts" / "fetch-models.sh"

WHISPER_DEFAULT = "large-v3-turbo-q5_0"
VAD_DEFAULT = "silero-v5.1.2"


def body() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def code() -> str:
    """**コメントを除いた本文。**

    散文にも `--user 0:0` と書いてあるので、ファイル全体を見るテストは
    **コメントだけで通ってしまう**（意図的に壊して確認したときに発覚した）。
    行頭 `#` の行と行内の `#` 以降を落として、実際に実行される部分だけを残す。
    ヒアドキュメント中の `#` も落ちるが、見たいのはフラグなので差し支えない。
    """
    lines = []
    for line in body().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line.split(" #", 1)[0])
    return "\n".join(lines)


def section_18_6() -> str:
    text = spec_text()
    start = text.index("### 18.6")
    return text[start : text.index("### 18.7", start)] if "### 18.7" in text else text[start:]


def bash() -> str:
    found = shutil.which("bash")
    assert found is not None, "bash が無い"
    return found


# --- 存在と実行権 -------------------------------------------------------


def test_the_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111, "実行権が無い（§17.4 の 2 本のうち 1 本）"


def test_the_script_parses() -> None:
    """`bash -n` で構文を確かめる。**壊れたスクリプトを配らない。**"""
    result = subprocess.run(  # noqa: S603
        [bash(), "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_the_comment_stripper_works() -> None:
    """`code()` がコメントを落とし、実行部分を残していること。

    **このヘルパが壊れると、以下の検査が全部「コメントだけで通る」状態に戻る。**
    """
    prose = "新規 named volume は root 所有で作成される"
    assert prose in body(), "前提にしているコメントが消えている"
    assert prose not in code()
    assert "docker run --rm --user 0:0" in code()


def test_the_script_uses_strict_mode() -> None:
    """`set -euo pipefail`（§18.6 の写し）。

    **`-u` が無いと未設定の変数が空文字になり、`curl -o /ggml-.bin` のような
    壊れたパスへ書きうる。**
    """
    assert "set -euo pipefail" in code()


# --- SPEC との一致（§18.6） ---------------------------------------------


def test_the_default_models_match_the_spec() -> None:
    """既定のモデル名が §18.6 の表と一致すること。"""
    spec = section_18_6()
    assert f"`{WHISPER_DEFAULT}`" in spec
    assert f"`{VAD_DEFAULT}`" in spec
    assert f'MODEL="${{1:-{WHISPER_DEFAULT}}}"' in code()
    assert f'VAD_MODEL="${{VAD_MODEL:-{VAD_DEFAULT}}}"' in code()


def test_the_urls_match_the_spec_table() -> None:
    """取得元が §18.6 の表と一致すること。

    **URL を勝手に変えない。**別のリポジトリから取ると、量子化の種類が違う
    同名ファイルを引きうる（サイズだけ見る doctor D-8 では気づけない）。
    """
    spec = section_18_6()
    urls = set(re.findall(r"https://huggingface\.co/[^\s`|]+", spec))
    assert any("ggerganov/whisper.cpp/resolve/main" in url for url in urls)
    assert any("ggml-org/whisper-vad/resolve/main" in url for url in urls)

    script = code()
    assert "https://huggingface.co/ggerganov/whisper.cpp/resolve/main" in script
    assert "https://huggingface.co/ggml-org/whisper-vad/resolve/main" in script


def test_the_spec_snippet_and_the_script_agree_on_the_essentials() -> None:
    """§18.6 のコードブロックと実物が要点で一致すること。

    **逐語一致は求めない** — 実物には SPEC に無い堅牢化（`.part` への退避・冪等な
    skip・モデル名の検証）が入っている。**SPEC の方が簡略である**ことを前提に、
    落とせない 4 点を見る。
    """
    snippet = spec_section_code("18.6", "bash")
    script = body()
    executed = code()
    for essential in ("--user 0:0", "voicedock-models", "/models/whisper", "chown -R 1000:1000"):
        assert essential in snippet, f"§18.6 から {essential} が消えている"
        assert essential in executed, f"スクリプトの実行部分に {essential} が無い"
    assert "sha256sum" in snippet
    assert "sha256sum" in script
    # SPEC は値を直書きし、実物は上書き可能にしている。**既定値が一致すること**を見る
    assert 'VOLUME="${VOLUME:-voicedock-models}"' in executed
    assert 'MOUNT="/models/whisper"' in executed


def test_the_download_runs_as_root() -> None:
    """**新規 named volume は root 所有で作成される**（§18.6）。

    非 root で走らせると書き込めない。`--user 0:0` を落とすと、**初回だけ失敗して
    2 回目以降は通る**という分かりにくい壊れ方になる。
    """
    assert "docker run --rm --user 0:0" in code(), "実行部分に --user 0:0 が無い"


def test_the_ownership_moves_to_uid_1000() -> None:
    """**取得後に uid 1000 へ移す**（§18.6 / §18.4）。コンテナは非 root で動く。"""
    assert "chown -R 1000:1000" in code()


def test_the_models_are_not_baked_into_the_image() -> None:
    """**モデルを Docker image へ埋め込まない**（§18.6）。18.6 GB ある。

    `ggml-org/whisper.cpp.git`（whisper.cpp 本体の取得元）は Dockerfile に在ってよい。
    見たいのは**モデルファイルの取得**なので、配布元と `.bin` で判定する。
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "huggingface" not in dockerfile
    assert ".bin" not in dockerfile or "ggml-large" not in dockerfile
    assert "ggml-silero" not in dockerfile


def test_the_volume_is_declared_in_compose() -> None:
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "voicedock-models:/models/whisper" in compose
    assert re.search(r"^  voicedock-models:", compose, re.M)


def test_every_volume_pins_its_name() -> None:
    """**`name:` を明示していること**（§18.2。v5.12→v5.13 の変更 AA-1）。

    書かないと Compose が**プロジェクト名を前置**して `voicedock_voicedock-models` になり、
    `fetch-models.sh` が書き込む `voicedock-models` とは**別の volume** をマウントする。
    症状は「574 MB を取得したのに D-8 がモデルを見つけない」であり、
    **取得の失敗と区別がつかない**（2026-09-14 に実機で踏んだ。#95）。

    宣言の有無だけを見ていた `test_the_volume_is_declared_in_compose` は、
    **名前が食い違っていても緑のままだった。**
    """
    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    declared = compose.get("volumes") or {}
    assert declared, "compose.yaml に volumes が無い"
    for key, options in declared.items():
        assert isinstance(options, dict) and options.get("name") == key, (
            f"volume {key!r} に name: が無い（Compose がプロジェクト名を前置する）"
        )


def test_the_makefile_has_a_models_target() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^models:", makefile, re.M)
    assert "./scripts/fetch-models.sh" in makefile
    assert re.search(r"^\.PHONY:.*\bmodels\b", makefile, re.M)


# --- モデル名の検証（実害に直結する） -----------------------------------


def run_script(name: str, *, vad: str | None = None) -> subprocess.CompletedProcess[str]:
    """到達できない `DOCKER_HOST` で走らせ、**検証だけを通す。**

    検証を抜けると `docker run` へ進む。**到達できない daemon を指しておけば、
    1 GB のダウンロードは始まらない**（テストがネットワークを使わない。§20.2）。

    **「docker が無い」ことに依存してはならない。**`make test` のコンテナには docker が
    無いが **CI には在る**ので、エラー文面で判定すると CI だけが落ちる（実際に落ちた）。
    検証を通ったかどうかは**標準出力のバナー**で見る。
    """
    env = {"PATH": "/usr/bin:/bin", "DOCKER_HOST": "unix:///nonexistent"}
    if vad is not None:
        env["VAD_MODEL"] = vad
    return subprocess.run(  # noqa: S603
        [bash(), str(SCRIPT), name],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=30,
    )


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "a/b",
        "..",
        ".hidden",
        "name with space",
        "name;rm -rf /",
        "name$(whoami)",
        "name`id`",
        "name&&id",
    ],
)
def test_a_dangerous_model_name_is_rejected(name: str) -> None:
    """**モデル名はそのまま URL とファイル名へ入る。**

    `../` を通すと named volume の外へ書きうる。シェルの展開文字を通すと
    `docker run -e` の中で評価されうる。**英数字と `-` `.` `_` だけに限る。**
    """
    result = run_script(name)
    assert result.returncode != 0
    assert "モデル名に使えない文字があります" in result.stderr, result.stderr


@pytest.mark.parametrize("vad", ["../escape", "a/b", ".hidden", "v;id"])
def test_a_dangerous_vad_model_name_is_rejected(vad: str) -> None:
    result = run_script(WHISPER_DEFAULT, vad=vad)
    assert result.returncode != 0
    assert "モデル名に使えない文字があります" in result.stderr, result.stderr


@pytest.mark.parametrize("empty", ["vad", "arg"])
def test_an_empty_model_name_falls_back_to_the_default(empty: str) -> None:
    """**空文字は「未設定」と同じ扱いにする。**

    `${VAD_MODEL:-silero-v5.1.2}` と `${1:-large-v3-turbo-q5_0}` はどちらも空文字で
    既定へ倒れる。空を拒否すると、**環境変数を空で export している環境や
    `./scripts/fetch-models.sh ""` で理由の分からない失敗になる。**
    """
    result = run_script(WHISPER_DEFAULT, vad="") if empty == "vad" else run_script("")
    assert "モデル名に使えない文字があります" not in result.stderr
    assert f"whisper={WHISPER_DEFAULT} vad={VAD_DEFAULT}" in result.stdout


@pytest.mark.parametrize("name", [WHISPER_DEFAULT, "large-v3-turbo-q8_0", "base.en", "tiny"])
def test_a_normal_model_name_passes_validation(name: str) -> None:
    """**正常な名前は検証を通る。**

    「検証を抜けたか」は**標準出力のバナー**で見る。`docker` の有無やエラー文面で
    判定すると、**`make test`（docker 無し）と CI（docker 在り）で結果が変わる。**
    """
    result = run_script(name)
    assert "モデル名に使えない文字があります" not in result.stderr
    assert f"whisper={name}" in result.stdout, "検証を抜けていない"


# --- 堅牢化（SPEC より厳しくしている点） --------------------------------


def test_the_download_goes_through_a_part_file() -> None:
    """**`.part` へ落としてから rename する。**

    中断したダウンロードが「在る」と判定されると、**0 バイトでないだけの壊れた
    モデルで起動してしまう**（doctor D-8 はサイズ 0 しか見ない）。
    """
    script = code()
    assert '-o "$1.part"' in script
    assert 'mv "$1.part" "$1"' in script
    assert 'rm -f "${MOUNT}"/*.part' in script


def test_an_existing_model_is_skipped_unless_forced() -> None:
    """**既に在るものは飛ばす。**毎回 1 GB を落とし直さない。`FORCE=1` で取り直す。"""
    script = code()
    assert 'if [ -s "$1" ] && [ -z "${FORCE:-}" ]' in script
    assert "FORCE=1" in script, "使い方に FORCE の説明が無い"


def test_curl_retries_and_fails_on_http_errors() -> None:
    """`-f`（HTTP エラーで失敗）と `--retry`（§18.6）。

    **`-f` が無いと 404 の HTML 本文を `.bin` として保存する。**サイズが 0 でないので
    doctor D-8 は通り、**whisper-cli が起動してから初めて壊れていることが分かる。**
    """
    assert "curl -fL --retry 3" in code()
