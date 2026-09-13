#!/bin/bash
#
# install.sh — ホスト側 Helper のインストール（SPEC §3.4(6), §4.1）
#
# 行うのは次の 3 つだけで、いずれも**ユーザーのホームディレクトリ配下に閉じている。**
#   1. <VOICEDOCK_HOME> に inbox/ queue/ state/ log/ bin/ と helper.conf を作る
#   2. helper/voicedock-ingest を <VOICEDOCK_HOME>/bin/ へコピーする
#   3. ~/Library/LaunchAgents/com.voicedock.ingest.plist を置き launchctl bootstrap する
#
# **`voicedock-reaper` はインストールしない。**これが安全ロック 2-A である（§14.2）。
# Phase 7 で初めて `--with-reaper` を実行する。
#
# bash 3.2 で書く（macOS 同梱。§3.3）。`voicedock-ingest` と同じ制約に従う。

set -euo pipefail

LABEL="com.voicedock.ingest"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELPER_DIR="$REPO_ROOT/helper"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"

info()  { printf '[install] %s\n' "$1"; }
warn()  { printf '[install] WARN  %s\n' "$1" >&2; }
die()   { printf '[install] ERROR %s\n' "$1" >&2; exit 1; }

# --- VOICEDOCK_HOME の決定 ----------------------------------------------
# .env の VOICEDOCK_HOME → 環境変数 → $HOME/VoiceDock。
# **決めた値を helper.conf に書き込む。**ingest は .env を読まない（§7.4 の系統境界）。

resolve_home() {
    local from_env=""
    if [ -f "$REPO_ROOT/.env" ]; then
        from_env="$(sed -n 's/^VOICEDOCK_HOME=//p' "$REPO_ROOT/.env" | tail -1)"
        # .env では引用符を付けない規約だが、付いていても動くようにする（§3.4(4)）
        from_env="${from_env%\"}"; from_env="${from_env#\"}"
        from_env="${from_env%\'}"; from_env="${from_env#\'}"
    fi
    if [ -n "$from_env" ]; then
        printf '%s' "$from_env"
    elif [ -n "${VOICEDOCK_HOME:-}" ]; then
        printf '%s' "$VOICEDOCK_HOME"
    else
        printf '%s' "$HOME/VoiceDock"
    fi
}

VD_HOME="$(resolve_home)"

check_home() {
    case "$VD_HOME" in
        /Users/*) ;;
        *) die "VOICEDOCK_HOME は /Users 配下でなければなりません（VirtioFS の制約。§4.1）: $VD_HOME" ;;
    esac
}

# --- 1. ディレクトリと helper.conf ---------------------------------------

make_tree() {
    local dir
    for dir in bin inbox queue/delete queue/result state log; do
        mkdir -p "$VD_HOME/$dir"
    done
    info "tree ready: $VD_HOME"
}

write_conf() {
    local conf="$VD_HOME/helper.conf"
    local example="$HELPER_DIR/helper.example.conf"
    [ -f "$example" ] || die "helper.example.conf がありません: $example"

    if [ -f "$conf" ]; then
        # **既存の設定を上書きしない。**ユーザーが編集した値を壊さない
        info "helper.conf は既にあります（上書きしません）: $conf"
        local missing
        missing="$(_missing_keys "$example" "$conf")"
        if [ -n "$missing" ]; then
            warn "helper.example.conf にあって helper.conf に無いキーがあります。手で反映してください:"
            printf '%s\n' "$missing" >&2
        fi
        return 0
    fi

    # VOICEDOCK_HOME の行だけ、決めた値に差し替える
    awk -v home="$VD_HOME" '
        /^VOICEDOCK_HOME=/ { printf "VOICEDOCK_HOME=\"%s\"\n", home; next }
        { print }
    ' "$example" > "$conf"
    info "helper.conf を作りました: $conf"
}

_missing_keys() {   # $1=example $2=conf
    local key
    sed -n 's/^\([A-Z_][A-Z_0-9]*\)=.*/\1/p' "$1" | while read -r key; do
        grep -q "^$key=" "$2" || printf '  %s\n' "$key"
    done
}

# --- 2. ingest（と reaper）の配置 ----------------------------------------

install_ingest() {
    [ -f "$HELPER_DIR/voicedock-ingest" ] || die "voicedock-ingest がありません: $HELPER_DIR"
    cp "$HELPER_DIR/voicedock-ingest" "$VD_HOME/bin/voicedock-ingest"
    chmod 755 "$VD_HOME/bin/voicedock-ingest"
    info "voicedock-ingest を配置しました: $VD_HOME/bin/"
}

install_reaper() {
    # **黙って何もしてはならない。**「ロック 2-A を解除したつもり」の誤解を生む
    [ -f "$HELPER_DIR/voicedock-reaper" ] \
        || die "voicedock-reaper が $HELPER_DIR にありません。--with-reaper は使えません。
       安全ロック 2-A は解除されていません（§14.2）。"
    cp "$HELPER_DIR/voicedock-reaper" "$VD_HOME/bin/voicedock-reaper"
    chmod 755 "$VD_HOME/bin/voicedock-reaper"
    warn "voicedock-reaper を配置しました。**安全ロック 2-A を解除しました**（§14.2）"
}

# --- 3. LaunchAgent ------------------------------------------------------

install_plist() {
    local template="$HELPER_DIR/$LABEL.plist"
    [ -f "$template" ] || die "$LABEL.plist がありません: $HELPER_DIR"
    mkdir -p "$PLIST_DIR"
    sed "s|@VOICEDOCK_HOME@|$VD_HOME|g" "$template" > "$PLIST_PATH"
    info "plist を配置しました: $PLIST_PATH"

    # load / unload は非推奨。bootstrap / bootout を使う
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$PLIST_PATH"
    info "LaunchAgent を bootstrap しました: $LABEL"
}

uninstall() {
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    info "LaunchAgent を撤去しました: $LABEL"
    # **<VOICEDOCK_HOME> は消さない。**inbox / queue / state はデータである
    info "$VD_HOME は残しています（データのため）。不要なら手で削除してください"
}

# --- --status ------------------------------------------------------------

status() {
    printf 'VoiceDock Helper status\n'
    printf '────────────────────────────────────────────────────────\n'
    printf 'VOICEDOCK_HOME    : %s\n' "$VD_HOME"

    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
        printf 'LaunchAgent       : loaded (%s)\n' "$LABEL"
    else
        printf 'LaunchAgent       : NOT loaded — ./helper/install.sh を実行してください\n'
    fi

    if [ -x "$VD_HOME/bin/voicedock-ingest" ]; then
        if VOICEDOCK_HELPER_CONF="$VD_HOME/helper.conf" \
            "$VD_HOME/bin/voicedock-ingest" --check-config >/dev/null 2>&1; then
            printf 'helper.conf       : OK（起動時検証 1〜7 を通過。§7.4）\n'
        else
            printf 'helper.conf       : INVALID —\n'
            VOICEDOCK_HELPER_CONF="$VD_HOME/helper.conf" \
                "$VD_HOME/bin/voicedock-ingest" --check-config 2>&1 | sed 's/^/                    /' || true
        fi
    else
        printf 'helper.conf       : - (ingest が未配置)\n'
    fi

    # 安全ロック 2-A（§14.2）
    if [ -x "$VD_HOME/bin/voicedock-reaper" ]; then
        printf 'lock 2-A          : voicedock-reaper is INSTALLED  <- 削除が可能な状態です\n'
    else
        printf 'lock 2-A          : voicedock-reaper is NOT installed（削除は不可能。既定）\n'
    fi

    local hb="$VD_HOME/state/heartbeat.json"
    if [ -f "$hb" ]; then
        printf 'heartbeat         : %s\n' "$(sed -n 's/.*"updated_at": *"\([^"]*\)".*/\1/p' "$hb")"
        printf 'mount_readonly    : %s\n' "$(sed -n 's/.*"mount_readonly": *\([a-z]*\).*/\1/p' "$hb")"
        printf 'config_error      : %s\n' "$(sed -n 's/.*"config_error": *\(.*\)$/\1/p' "$hb")"
    else
        printf 'heartbeat         : まだありません（%s）\n' "$hb"
    fi

    local log="$VD_HOME/log/ingest.log"
    if [ -f "$log" ]; then
        printf '────────────────────────────────────────────────────────\n'
        printf '%s の末尾 20 行:\n' "$log"
        tail -20 "$log"
    fi
}

usage() {
    cat <<'USAGE'
usage: ./helper/install.sh [--with-reaper | --status | --uninstall]

  (no args)      Helper をインストールする（SPEC §3.4(6)）
  --with-reaper  voicedock-reaper も配置する。**安全ロック 2-A を解除する**（§14.2。Phase 7）
  --status       稼働確認
  --uninstall    LaunchAgent を撤去する（<VOICEDOCK_HOME> は消さない）
  --help         この表示
USAGE
}

main() {
    case "${1:-}" in
        --status)      status ;;
        --uninstall)   uninstall ;;
        --help | -h)   usage ;;
        --with-reaper)
            check_home; make_tree; write_conf; install_ingest; install_reaper; install_plist ;;
        "")
            check_home; make_tree; write_conf; install_ingest; install_plist
            printf '\n'
            info "完了しました。./helper/install.sh --status で確認してください"
            info "voicedock-reaper は配置していません（安全ロック 2-A。§14.2）" ;;
        *) usage >&2; exit 2 ;;
    esac
}

main "$@"
