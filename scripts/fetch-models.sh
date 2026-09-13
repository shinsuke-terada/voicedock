#!/usr/bin/env bash
# Whisper モデルと Silero VAD モデルを named volume へ取得する（SPEC §18.6）。
#
# **モデルを Docker image へ埋め込まない。**18.6 GB あり、image の再構築ごとに
# 焼き直すことになる。named volume `voicedock-models` に置き、`/models/whisper` へ
# マウントする（§18.2）。
#
# **新規 named volume は root 所有で作成される。**だからダウンロードは root で行い、
# 取得後に uid 1000 へ所有権を移す（コンテナは非 root で動く。§18.4）。
#
# 使い方:
#   ./scripts/fetch-models.sh                      # 既定のモデル
#   ./scripts/fetch-models.sh large-v3-turbo-q8_0  # Whisper モデルを指定
#   VAD_MODEL=silero-v6.2.0 ./scripts/fetch-models.sh
#   FORCE=1 ./scripts/fetch-models.sh              # 既に在っても取り直す
set -euo pipefail

MODEL="${1:-large-v3-turbo-q5_0}"
VAD_MODEL="${VAD_MODEL:-silero-v5.1.2}"

WHISPER_BASE="${WHISPER_BASE:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main}"
# whisper.cpp v1.9.4 の models/download-vad-model.sh が参照する先。
# 2026-09-12 に HTTP 200 を確認済み（§22 R-5 クローズ）
VAD_URL="${VAD_URL:-https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-${VAD_MODEL}.bin}"

VOLUME="${VOLUME:-voicedock-models}"
MOUNT="/models/whisper"
IMAGE="${IMAGE:-debian:bookworm-slim}"
FORCE="${FORCE:-}"

die() {
    printf 'fetch-models: %s\n' "$1" >&2
    exit 1
}

# **モデル名を検証する。**そのまま URL とファイル名へ入るので、`/` や `..` を
# 許すと volume の外へ書きうる。`-` と `.` と英数字だけに限る。
# **docker の存在確認より前に行う。**入力の検証が外部コマンドの有無に依存すると、
# docker が無い環境で「何が悪いのか」が分からなくなる
for name in "$MODEL" "$VAD_MODEL"; do
    case "$name" in
        '' | *[!A-Za-z0-9._-]* | .* | *..*)
            die "モデル名に使えない文字があります: '$name'"
            ;;
    esac
done

command -v docker >/dev/null 2>&1 || die "docker が見つかりません（§3.2）"

printf 'fetch-models: volume=%s whisper=%s vad=%s\n' "$VOLUME" "$MODEL" "$VAD_MODEL"
printf 'fetch-models: 約 1 GB を取得します。既に在るものは飛ばします（FORCE=1 で取り直し）\n'

# **`--user 0:0` で root として入る。**新規 volume の所有権が root なので、
# 非 root では書き込めない（§18.6）
docker run --rm --user 0:0 \
    -v "${VOLUME}:${MOUNT}" \
    -e MODEL="$MODEL" \
    -e VAD_MODEL="$VAD_MODEL" \
    -e VAD_URL="$VAD_URL" \
    -e WHISPER_BASE="$WHISPER_BASE" \
    -e MOUNT="$MOUNT" \
    -e FORCE="$FORCE" \
    "$IMAGE" bash -c '
      set -euo pipefail

      fetch() {
          # $1 = 宛先, $2 = URL
          if [ -s "$1" ] && [ -z "${FORCE:-}" ]; then
              printf "skip (exists): %s\n" "$1"
              return 0
          fi
          # **`.part` へ落としてから rename する。**中断したダウンロードが
          # 「在る」と判定されると、0 バイトでないだけの壊れたモデルで起動してしまう
          # （doctor D-8 はサイズ 0 しか見ない）
          curl -fL --retry 3 --retry-delay 2 -o "$1.part" "$2"
          mv "$1.part" "$1"
          printf "fetched: %s\n" "$1"
      }

      apt-get update -qq
      apt-get install -y -qq --no-install-recommends curl ca-certificates >/dev/null

      fetch "${MOUNT}/ggml-${MODEL}.bin"     "${WHISPER_BASE}/ggml-${MODEL}.bin"
      fetch "${MOUNT}/ggml-${VAD_MODEL}.bin" "${VAD_URL}"

      # 途中で落ちた残骸を片付ける（次回の fetch が「在る」と誤認しないように）
      rm -f "${MOUNT}"/*.part

      # **取得後に uid 1000 へ移す。**コンテナは非 root で動く（§18.4）
      chown -R 1000:1000 "${MOUNT}"
      ls -la "${MOUNT}"
      sha256sum "${MOUNT}"/*.bin
    '

printf 'fetch-models: 完了しました。`make doctor` の D-8 / D-9 で確認できます\n'
