"""偽 `whisper-cli`（SPEC §10.6 の生 JSON を出力する実行可能スクリプトを組み立てる）。

**本物の whisper.cpp はテストに使えない。**CI にはバイナリもモデルも無く、1 本の文字起こしに
数分かかる（§21.2 Phase 2）。ここで組み立てるのは「§10.6 の argv を受け取り、`-of` が
指すパスへ生 JSON を書く」だけのシェルスクリプトである。

**生 JSON の形は whisper.cpp `v1.9.4` の `-oj` 出力に合わせている。**とくに
`offsets` は**ミリ秒**であり（`timestamps` は同じ情報の文字列表現）、`transcribe.py` は
これを秒へ割る。形を間違えると、テストは通るのに実機で時刻がすべて 1000 倍になる。
"""

from __future__ import annotations

import json
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

HELP_WITH_VAD = """usage: whisper-cli [options] file0 file1 ...
  -m FNAME,  --model FNAME
  -f FNAME,  --file FNAME
  -oj,       --output-json
             --vad
             --vad-model FNAME
             --vad-threshold N
             --vad-min-speech-duration-ms N
             --vad-min-silence-duration-ms N
             --vad-speech-pad-ms N
"""
"""`--help` の出力（VAD あり）。D-7 / `is_available()` がこれを見る。"""

HELP_WITHOUT_VAD = """usage: whisper-cli [options] file0 file1 ...
  -m FNAME,  --model FNAME
  -f FNAME,  --file FNAME
  -oj,       --output-json
"""


@dataclass(frozen=True)
class Utterance:
    """生 JSON の 1 区間。`start` / `end` は**秒**で書き、出力時にミリ秒へ直す。"""

    start: float
    end: float
    text: str


DEFAULT_UTTERANCES: tuple[Utterance, ...] = (
    Utterance(0.0, 3.2, " おはようございます。"),
    Utterance(5.5, 9.0, " 今日の予定を確認します。"),
)


def raw_document(
    utterances: tuple[Utterance, ...] = DEFAULT_UTTERANCES, *, language: str = "ja"
) -> dict[str, object]:
    """whisper.cpp `-oj` の生 JSON（§10.6 が「残さない」と定めている形）。"""
    return {
        "systeminfo": "AVX = 0 | NEON = 1 |",
        "model": {"type": "large", "multilingual": True},
        "params": {"model": "ggml-large-v3-turbo-q5_0.bin", "language": language},
        "result": {"language": language},
        "transcription": [
            {
                "timestamps": {"from": _stamp(u.start), "to": _stamp(u.end)},
                "offsets": {"from": round(u.start * 1000), "to": round(u.end * 1000)},
                "text": u.text,
            }
            for u in utterances
        ],
    }


def _stamp(seconds: float) -> str:
    whole = int(seconds)
    millis = round((seconds - whole) * 1000)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d},{millis:03d}"


def write_fake_whisper(
    path: Path,
    *,
    utterances: tuple[Utterance, ...] = DEFAULT_UTTERANCES,
    language: str = "ja",
    exit_code: int = 0,
    stderr: str = "",
    sleep_seconds: float = 0.0,
    grandchild_marker: Path | None = None,
    help_text: str = HELP_WITH_VAD,
    write_output: bool = True,
) -> Path:
    """`-of` の次の引数を見て `<base>.json` を書く偽 whisper を作る。

    `--help` を渡されたら `help_text` を出して終わる（`is_available()` 用）。
    **呼ばれた argv を `<path>.argv` へ 1 行ずつ残す** — テストが「`shell=True` で
    組み立てた 1 本の文字列ではなく、リストで渡っている」ことを確かめられる。

    `grandchild_marker` を与えると、**孫プロセス**（`( sleep; touch marker ) &`）で待つ。
    同じプロセスグループに属するが `sh` の直接の子ではないので、**`process.kill()` では
    生き残り、`os.killpg()` でだけ死ぬ。**§10.6 の「プロセスグループごと kill」を
    marker の有無で直接確かめられる。
    """
    document = json.dumps(raw_document(utterances, language=language), ensure_ascii=False)
    script = f"""#!/bin/sh
printf '%s\\n' "$@" > '{path}.argv'
for arg in "$@"; do
  if [ "$arg" = "--help" ] || [ "$arg" = "-h" ]; then
    cat <<'HELP_EOF'
{help_text.rstrip()}
HELP_EOF
    exit 0
  fi
done
base=''
take=0
for arg in "$@"; do
  if [ "$take" = "1" ]; then base="$arg"; take=0; continue; fi
  if [ "$arg" = "-of" ]; then take=1; fi
done
{_wait_clause(sleep_seconds, grandchild_marker)}
{f">&2 printf %s {shlex.quote(stderr)}" if stderr else ""}
if [ -n "$base" ] && [ "{int(write_output)}" = "1" ]; then
  cat > "$base.json" <<'JSON_EOF'
{document}
JSON_EOF
fi
exit {exit_code}
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _wait_clause(sleep_seconds: float, marker: Path | None) -> str:
    """待機部分。`marker` があれば孫プロセスで待つ（プロセスグループ kill の検証用）。"""
    if not sleep_seconds:
        return ""
    if marker is None:
        return f"sleep {sleep_seconds}"
    return f"( sleep {sleep_seconds}; touch '{marker}' ) &\nwait"


def recorded_argv(path: Path) -> list[str]:
    """偽 whisper が受け取った argv（`write_fake_whisper` が残したもの）。"""
    log = Path(f"{path}.argv")
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []
