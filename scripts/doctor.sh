#!/bin/bash
#
# doctor.sh — ホスト側の診断（SPEC §19.2 の DH 表 5 件）
#
# **コンテナ内からは確認できない前提だけを見る。**とくに DH-1（Apple Virtualization
# Framework）は致命的で、Docker VMM では `/Volumes` 配下の外付けボリュームの bind mount が
# 失敗する（docker/for-mac#7480）。**ローカルディスクの bind mount は動くため、Obsidian
# Vault は読み書きできるのに DJI Mic だけ見えない**という切り分けの難しい症状になる。
#
# **検査のみを行い、ホスト設定は変更しない**（§19.2）。v3.1 は `setup.sh` と `doctor.sh` に
# 同じ検査を二重に書いており、片方だけ更新して食い違う事故があった（§21.1 に一本化した）。
#
# 実行順は DH-1 → DH-10 → DH-13 → DH-12 → DH-15（前提が積み上がる順）。
#
# bash 3.2 で書く（macOS 同梱。§3.3）。`voicedock-ingest` と同じ制約に従う。

set -euo pipefail

LABEL_WIDTH=21
DETAIL_INDENT=27
SEPARATOR="────────────────────────────────────────────────────────"
EXIT_DOCTOR_FATAL=4

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# **テストからの差し替え口。**既定はいずれも実機の位置である
DOCKER_SETTINGS_DIR="${VOICEDOCK_DOCKER_SETTINGS_DIR:-$HOME/Library/Group Containers/group.com.docker}"
LAUNCH_LABEL="com.voicedock.ingest"

passed=0
failed=0
notices=0

# --- 表示（§19.2 の行の書式） -------------------------------------------
# `[<記号>] <ラベル（21 桁左詰め）><詳細>`。続き行は 27 桁インデントする。
# **`doctor.py` と同じ桁で出す** — 片方だけ変えると 17 行が揃わない

row() {
    printf '[%s] %-*s%s\n' "$1" "$LABEL_WIDTH" "$2" "$3"
    case "$1" in
        '✓') passed=$((passed + 1)) ;;
        '✗') failed=$((failed + 1)) ;;
        '!') notices=$((notices + 1)) ;;
    esac
}

cont() { printf '%*s%s\n' "$DETAIL_INDENT" "" "$1"; }
ok()     { row '✓' "$1" "$2"; }
notice() { row '!' "$1" "$2"; }
bad()    { row '✗' "$1" "$2"; }
skip()   { printf '[-] %-*s%s\n' "$LABEL_WIDTH" "$1" "$2"; }

die() {
    bad "$1" "$2"
    printf '%s\n' "$SEPARATOR"
    printf '%s\n' "$(summary)"
    exit "$EXIT_DOCTOR_FATAL"
}

summary() {
    printf '%d checks passed, %d failed, %d notices' "$passed" "$failed" "$notices"
}

# --- VOICEDOCK_HOME の決定（install.sh と同じ順序） ----------------------
# .env の VOICEDOCK_HOME → 環境変数 → $HOME/VoiceDock。
# **ingest は .env を読まない**（§7.4 の系統境界）ので、ここで解決した値と
# helper.conf の値が一致することを DH-13 が確かめる。

resolve_home() {
    local from_env=""
    if [ -f "$REPO_ROOT/.env" ]; then
        from_env="$(sed -n 's/^VOICEDOCK_HOME=//p' "$REPO_ROOT/.env" | tail -1)"
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

# --- DH-1: Apple Virtualization Framework（§3.2） -----------------------
# **致命的（中止）。**外れていると DJI Mic の bind mount だけが静かに失敗する。
# `jq` は使わない（macOS に無い。§3.3）ので grep で拾う。

check_virtualization() {
    local file found=""
    for file in "$DOCKER_SETTINGS_DIR/settings-store.json" "$DOCKER_SETTINGS_DIR/settings.json"; do
        [ -f "$file" ] && { found="$file"; break; }
    done
    [ -n "$found" ] || die "Virtualization" "Docker Desktop の設定が見つかりません（$DOCKER_SETTINGS_DIR）"

    local value
    value="$(sed -n 's/.*"UseVirtualizationFramework"[[:space:]]*:[[:space:]]*\([a-z]*\).*/\1/p' "$found" | tail -1)"
    case "$value" in
        true) ok "Virtualization" "UseVirtualizationFramework=true" ;;
        false)
            die "Virtualization" \
                "UseVirtualizationFramework=false — Docker Desktop の Settings > General で有効にしてください（§3.2）"
            ;;
        *) die "Virtualization" "UseVirtualizationFramework を $found から読めません" ;;
    esac
}

# --- DH-10: compose の変数解決（§19.2） ---------------------------------
# `${OBSIDIAN_VAULT:?}` が未設定ならここで落ちる。**解決できた値が実在する
# ディレクトリであることも見る** — 存在しないパスを bind mount すると空ディレクトリが
# 作られ、**Vault が空だと誤認する**（v5.0 で DH-5 を統合）。

COMPOSE_CONFIG=""

check_compose_config() {
    if ! COMPOSE_CONFIG="$(cd "$REPO_ROOT" && docker compose config 2>&1)"; then
        bad "Compose config" "docker compose config が失敗しました"
        printf '%s\n' "$COMPOSE_CONFIG" | sed "s/^/$(printf '%*s' "$DETAIL_INDENT" '')/" | head -5
        COMPOSE_CONFIG=""
        return 1
    fi

    # **`target: /obsidian` に対応する `source:` を取る。**`$2` で切ると
    # **空白を含む Vault パス**（`~/Library/Mobile Documents/...`）で壊れる
    local vault
    vault="$(printf '%s\n' "$COMPOSE_CONFIG" | awk '
        /^[[:space:]]*source:[[:space:]]/ { s = $0; sub(/^[[:space:]]*source:[[:space:]]*/, "", s) }
        /^[[:space:]]*target:[[:space:]]*\/obsidian[[:space:]]*$/ { print s; exit }
    ')"
    [ -n "$vault" ] || vault="$(env_value OBSIDIAN_VAULT)"

    if [ -z "$vault" ]; then
        bad "Compose config" "OBSIDIAN_VAULT を解決できません"
        return 1
    fi
    if [ ! -d "$vault" ]; then
        bad "Compose config" "OBSIDIAN_VAULT=$vault は存在しないディレクトリです"
        cont "存在しないパスを bind mount すると空のディレクトリが作られ、Vault が空だと誤認します"
        return 1
    fi
    ok "Compose config" "valid, OBSIDIAN_VAULT=$vault (exists)"
}

env_value() {
    [ -f "$REPO_ROOT/.env" ] || return 0
    sed -n "s/^$1=//p" "$REPO_ROOT/.env" | tail -1 | sed 's/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//'
}

# --- DH-13: helper.conf（§7.4 の起動時検証 1〜7） -----------------------
# **`voicedock-ingest --check-config` を呼ぶ。**検査を 2 箇所に書くと片方だけ古くなる。

check_helper_conf() {
    local home ingest conf
    home="$(resolve_home)"
    if [ ! -d "$home" ]; then
        bad "Helper config" "VOICEDOCK_HOME=$home がありません（helper/install.sh を実行してください）"
        return 1
    fi
    # **Darwin でだけ課す。**この条件は Docker Desktop for Mac の VirtioFS が理由であり、
    # Linux には /Users も VirtioFS も無い（`voicedock-ingest` の起動時検証 2 と同じ）
    if [ "$(uname -s)" = "Darwin" ]; then
        case "$home" in
            /Users/*) ;;
            *) bad "Helper config" "VOICEDOCK_HOME=$home が /Users 配下ではありません（§7.4）"; return 1 ;;
        esac
    fi

    ingest="$home/bin/voicedock-ingest"
    conf="$home/helper.conf"
    [ -x "$ingest" ] || { bad "Helper config" "$ingest がありません"; return 1; }
    [ -f "$conf" ] || { bad "Helper config" "$conf がありません"; return 1; }

    local output
    if ! output="$(VOICEDOCK_HELPER_CONF="$conf" "$ingest" --check-config 2>&1)"; then
        bad "Helper config" "§7.4 の起動時検証に失敗しました"
        printf '%s\n' "$output" | sed "s/^/$(printf '%*s' "$DETAIL_INDENT" '')/" | head -5
        return 1
    fi

    # **conf の VOICEDOCK_HOME が ingest を置いた場所の親と一致すること**（§19.2 DH-13）。
    # 食い違うと heartbeat がコンテナの見ない場所へ書かれ、**H-8 が「Helper が止まっている」と
    # 報告し続ける**（実際は動いている）
    local conf_home
    conf_home="$(conf_value "$conf" VOICEDOCK_HOME)"
    if [ "$conf_home" != "$home" ]; then
        bad "Helper config" "helper.conf の VOICEDOCK_HOME=$conf_home が $home と食い違います"
        cont "heartbeat がコンテナの見ない場所へ書かれ、H-8 が止まっていると報告し続けます"
        return 1
    fi

    local mount_mode delete_source include exclude
    mount_mode="$(conf_value "$conf" MOUNT_MODE)"
    delete_source="$(conf_value "$conf" DELETE_SOURCE_AUDIO)"
    include="$(conf_array "$conf" INCLUDE_VOLUMES)"
    exclude="$(conf_array "$conf" EXCLUDE_VOLUMES)"

    if [ "$mount_mode" = "rw" ]; then
        # **安全ロック 2-B が外れている**（§14.2）。Phase 7 以降だけが取る値である
        notice "Helper config" "MOUNT_MODE=rw — デバイスが読み書きで再マウントされます（安全ロック 2-B が解除）"
    else
        ok "Helper config" "MOUNT_MODE=$mount_mode, DELETE_SOURCE_AUDIO=$delete_source"
    fi
    cont "VOICEDOCK_HOME=$home"
    cont "INCLUDE_VOLUMES=${include:-(none)}"
    cont "EXCLUDE_VOLUMES=${exclude:-(none)}"
}

conf_value() {
    # **source しない。**helper.conf は ingest が読むものであり、doctor が変数を汚さない
    sed -n "s/^[[:space:]]*$2=//p" "$1" | tail -1 | sed 's/^"//; s/"$//; s/#.*$//; s/[[:space:]]*$//'
}

conf_array() {
    # `NAME=("a" "b")` の中身を 1 行で。**bash 3.2 に連想配列は無い**ので文字列で扱う。
    #
    # **1 行形式と複数行形式の両方を読む。**`helper.example.conf`（§7.4 の規範ブロック）は
    #
    #     EXCLUDE_VOLUMES=(
    #       "Macintosh HD"
    #       ...
    #     )
    #
    # と**複数行で書かれており、`install.sh` はそれをそのまま配る。**1 行形式だけを
    # 読んでいた頃は **DH-13 が実機で常に `(none)` と表示していた** — ingest は
    # ちゃんと効かせているのに、**デバイスが検出されないときに真っ先に見る欄が嘘をつく。**
    #
    # 値の中に `)` を含む場合はそこで切れる。ボリューム名に `)` は使わない前提である。
    awk -v name="$2" '
        BEGIN { inside = 0; out = "" }
        {
            line = $0
            sub(/^[ \t]+/, "", line)
            if (substr(line, 1, 1) == "#") next
            if (inside == 0) {
                if (substr(line, 1, length(name) + 2) != name "=(") next
                line = substr(line, length(name) + 3)
                inside = 1
                buf = ""
            }
            p = index(line, ")")
            if (p > 0) {
                buf = buf " " substr(line, 1, p - 1)
                out = buf
                inside = 0
            } else {
                buf = buf " " line
            }
        }
        END {
            gsub(/[ \t]+/, " ", out)
            sub(/^ /, "", out); sub(/ $/, "", out)
            print out
        }
    ' "$1"
}

# --- DH-12: LaunchAgent（§19.2） ---------------------------------------
# **致命的。**Helper が動かなければ録音は 1 本も取り込まれない。

check_launch_agent() {
    if ! command -v launchctl >/dev/null 2>&1; then
        skip "LaunchAgent" "launchctl がありません（macOS 以外では確認できません）"
        return 0
    fi
    if launchctl print "gui/$(id -u)/$LAUNCH_LABEL" >/dev/null 2>&1; then
        ok "LaunchAgent" "$LAUNCH_LABEL loaded"
    else
        bad "LaunchAgent" "$LAUNCH_LABEL が load されていません（helper/install.sh を実行してください）"
        cont "Helper が動かなければ録音は 1 本も取り込まれません"
        return 1
    fi
}

# --- DH-15: compose に /Volumes と ports: が無い（§14.4 N-3 / N-13） ----

check_compose_safety() {
    if [ -z "$COMPOSE_CONFIG" ]; then
        skip "Compose safety" "docker compose config が読めていません"
        return 0
    fi
    local problems=""
    printf '%s\n' "$COMPOSE_CONFIG" | grep -q '/Volumes' && problems="/Volumes (N-3)"
    if printf '%s\n' "$COMPOSE_CONFIG" | grep -qE '^[[:space:]]*ports:'; then
        problems="${problems:+$problems, }ports: (N-13)"
    fi
    if [ -n "$problems" ]; then
        bad "Compose safety" "compose に含めてはならないものがあります: $problems"
        cont "N-3: コンテナは /Volumes を mount しない（取り込みは Helper が行う）"
        cont "N-13: ポートを公開しない（§14.5）"
        return 1
    fi
    ok "Compose safety" "no /Volumes, no ports:"
}

# --- コンテナ内の 12 検査（§19.2） --------------------------------------

run_container_doctor() {
    command -v docker >/dev/null 2>&1 || return 0
    (cd "$REPO_ROOT" && docker compose ps --status running --quiet voicedock 2>/dev/null) \
        | grep -q . || {
        printf '\n'
        skip "Container checks" "コンテナが起動していません（make up のあとで再実行してください）"
        return 0
    }
    printf '\n'
    (cd "$REPO_ROOT" && docker compose exec -T voicedock voicedock doctor) || return 0
}

main() {
    printf 'VoiceDock doctor (host)\n'
    printf '%s\n' "$SEPARATOR"

    local status=0
    check_virtualization
    check_compose_config || status=1
    check_helper_conf || status=1
    check_launch_agent || status=1
    check_compose_safety || status=1

    printf '%s\n' "$SEPARATOR"
    printf '%s\n' "$(summary)"
    run_container_doctor
    [ "$status" -eq 0 ] || exit "$EXIT_DOCTOR_FATAL"
}

main "$@"
