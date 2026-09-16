#!/bin/bash
# 削除の有効化 / 無効化（SPEC §14.2 / §21.2）。
#
# **三重ロックは 2 つの系統に分かれている**（§7.4）。
#
#   ホスト側   : helper.conf の DELETE_SOURCE_AUDIO / MOUNT_MODE、voicedock-reaper の有無
#   コンテナ側 : config.yaml の cleanup.delete_source_audio
#
# **1 つずつ手で直すと、どれか 1 つ忘れた状態に落ちやすい。**ロック 1 だけを解除すると
# 削除は 1 件も行われないままセッションも進まなくなる（#145）。V-30 / V-33 が起動時に
# 止めるが、**そもそも半端な状態を作らせない**のがこのスクリプトの役目である。
#
# **このスクリプトはデバイスに触れない。**設定を変えるだけである。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$REPO_ROOT/config/config.yaml"
KEY="  delete_source_audio:"

info() { printf '  %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

set_config() {  # $1=true|false
    grep -q "^$KEY" "$CONFIG" || die "config.yaml に cleanup.delete_source_audio がありません"
    tmp="$(mktemp)"
    awk -v value="$1" '
        index($0, "  delete_source_audio:") == 1 { printf "  delete_source_audio: %s\n", value; next }
        { print }
    ' "$CONFIG" > "$tmp"
    cat "$tmp" > "$CONFIG"
    rm -f "$tmp"
    info "config.yaml: cleanup.delete_source_audio=$1"
}

[ -f "$CONFIG" ] || die "config/config.yaml がありません。cp config/config.example.yaml config/config.yaml してください"

if [ "${1:-}" = "--disable" ]; then
    printf '\n削除を無効に戻します（安全ロックを 3 つとも掛け直す）。\n\n'
    "$REPO_ROOT/helper/install.sh" --disable-deletion
    set_config false
    printf '\n'
    info "コンテナを入れ替えます"
    (cd "$REPO_ROOT" && docker compose up -d)
    exit 0
fi

cat <<'WARN'

============================================================
  **デバイス上の元音声を自動削除する状態にします。**

  これ以降、保存検証に成功した録音は DJI Mic から削除されます。
  削除されたファイルは戻せません。

  変更するもの（§14.2 の三重ロック）:
    ロック 1   : helper.conf   DELETE_SOURCE_AUDIO=false → true
                 config.yaml   cleanup.delete_source_audio=false → true
    ロック 2-A : voicedock-reaper を配置する
    ロック 2-B : helper.conf   MOUNT_MODE=ro → rw

  **先に確かめること**（§21.2）:
    - docs/E2E.md の E2E-01〜E2E-09 / E2E-12 が PASS していること
    - **1 日を通しで運用して問題が出ていないこと**（#125）
    - make doctor が 1 件も落ちていないこと

  戻すときは make disable-deletion
============================================================

WARN
printf '続けるには ENABLE と入力してください: '
read -r reply || reply=""
[ "$reply" = "ENABLE" ] || die "中止しました。何も変更していません"

printf '\n'
"$REPO_ROOT/helper/install.sh" --enable-deletion --yes
set_config true
printf '\n'
info "コンテナを入れ替えます"
(cd "$REPO_ROOT" && docker compose up -d)
printf '\n'
info "確認します（三重ロックの状態は D-17 に出ます）"
(cd "$REPO_ROOT" && docker compose exec -T voicedock voicedock doctor) || true
