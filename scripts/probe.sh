#!/bin/bash
#
# probe.sh — 実機で 1 回叩けば `docs/POC.md` §10 に貼れる Markdown が出る（#93）
#
# **採るだけで、何も変えない。**読むのは `mount` / `df -Pk` / `stat` と
# `<VOICEDOCK_HOME>/state/*.json` だけである。**デバイスへ書かない。`diskutil` を呼ばない** —
# マウント操作は `voicedock-ingest` の仕事であり（N-19）、probe が呼ぶと
# **測ろうとしている状態そのものを変えてしまう。**
#
# **P0-8 は機械判定する。**`mount` の read-only と `heartbeat.json` の `mount_readonly` が
# 食い違えば `✗` を出し、終了コード 5 で終わる。issue #3 が
# 「一致しなければ実装のバグである」と規定しているので、人の目視に委ねない。
#
# **`ro` で繋いだ瞬間は採り直しが効かない。**`heartbeat.json` は次の ingest で上書きされる。
#
# 使い方:
#   ./scripts/probe.sh                  # inventory.json のデバイスを自動で拾う
#   ./scripts/probe.sh DJIMIC3          # ボリュームを明示する
#   ./scripts/probe.sh > /tmp/probe.md  # §10 へ貼る
#
# bash 3.2 で書く（macOS 同梱。§3.3）。`voicedock-ingest` と同じ制約に従う。

set -euo pipefail

EXIT_PROBE_MISMATCH=5

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# **テストからの差し替え口。**既定はいずれも実機の位置である
VOLUMES_ROOT="${VOICEDOCK_VOLUMES_ROOT:-/Volumes}"
MOUNT_CMD="${VOICEDOCK_MOUNT_CMD:-mount}"

# P0-11 の連続 stat。既定は helper.conf の STABILITY_* に合わせる
PROBE_SAMPLES="${PROBE_SAMPLES:-2}"

mismatch=0

usage() {
    cat <<'USAGE'
probe.sh — 実機の採取（SPEC §20.3 / docs/POC.md §10）

  ./scripts/probe.sh [VOLUME ...]

VOLUME を省略すると <VOICEDOCK_HOME>/state/inventory.json の devices を使う。
終了コード: 0=一致 / 5=P0-8 が食い違った（実装のバグ）
USAGE
}

# --- VOICEDOCK_HOME の決定（install.sh / doctor.sh と同じ順序） ----------
# .env の VOICEDOCK_HOME → 環境変数 → $HOME/VoiceDock。**3 か所で同じ順序を使う。**

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

# --- JSON の読み取り（`jq` は使わない。macOS に無い。§3.3） --------------
# **壊れた JSON・欠落したキーは「不明」として扱い、落ちない**（§7.5）。
# Helper の書き込み途中を読む可能性があるためである。

json_scalar() {            # $1=file, $2=key -> 値 or ""（文字列の `"` は外す）
    [ -f "$1" ] || return 0
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\\([^,}]*\\).*/\\1/p" "$1" \
        | tail -1 | sed 's/[[:space:]]*$//; s/^"//; s/"$//'
}

json_bool() {              # $1=file, $2=key -> true|false|unknown
    local value
    value="$(json_scalar "$1" "$2")"
    case "$value" in
        true|false) printf '%s' "$value" ;;
        *) printf 'unknown' ;;
    esac
}

# `inventory.json` の devices のキー。ingest が 4 桁インデントで `"<名前>": [` と書く（§7.5）
inventory_devices() {      # $1=file
    [ -f "$1" ] || return 0
    sed -n 's/^    "\(.*\)": \[$/\1/p' "$1"
}

# `inventory.json` の relpath（全デバイス分）。6 桁インデントの文字列
inventory_relpaths() {     # $1=file
    [ -f "$1" ] || return 0
    sed -n 's/^      "\(.*\)",\{0,1\}$/\1/p' "$1"
}

# **1 デバイス分の relpath。**全件を数えると 2 台繋いだときに P0-10 が壊れる
inventory_relpaths_for() { # $1=file, $2=device
    [ -f "$1" ] || return 0
    awk -v want="$2" '
        /^    ".*": \[$/ {
            key = $0; sub(/^    "/, "", key); sub(/": \[$/, "", key)
            cur = (key == want); next
        }
        /^    \]/ { cur = 0; next }
        cur && /^      "/ {
            line = $0; sub(/^      "/, "", line); sub(/",?$/, "", line)
            print line
        }
    ' "$1"
}

# **`device_free_bytes` は数値の行だけを見る。**`devices` のキーが同名で後から来るため、
# キー名だけで拾うと `[` を読んでしまう
inventory_free_bytes() {   # $1=file, $2=device
    [ -f "$1" ] || return 0
    sed -n "s/^    \"$2\": \\([0-9][0-9]*\\),\\{0,1\\}$/\\1/p" "$1" | tail -1
}

# --- マウント状態（**`diskutil` を呼ばない**） ---------------------------
# `mount` の 1 行は `/dev/disk4s1 on /Volumes/DJIMIC3 (msdos, local, nodev, read-only, ...)`。
# **`grep -F " on <path> "` で厳密に照合する** — ボリューム名の部分一致を避けるため。

mount_line() {             # $1=volume path -> 行 or ""
    "$MOUNT_CMD" 2>/dev/null | grep -F " on $1 " || true
}

mount_is_readonly() {      # $1=volume path -> true|false|absent
    local line
    line="$(mount_line "$1")"
    if [ -z "$line" ]; then
        printf 'absent'
    elif printf '%s' "$line" | grep -q "read-only"; then
        printf 'true'
    else
        printf 'false'
    fi
}

# --- stat（BSD / GNU 両対応。テストは Linux コンテナで走る） -------------

stat_size_mtime() {        # $1=path -> "<size> <mtime>" or ""
    stat -f '%z %m' "$1" 2>/dev/null || stat -c '%s %Y' "$1" 2>/dev/null || true
}

# --- helper.conf の値（**`.env` ではない**。§7.4 の系統境界） ------------

conf_value() {             # $1=conf, $2=key -> 値 or ""
    [ -f "$1" ] || return 0
    sed -n "s/^$2=//p" "$1" | tail -1 | sed 's/^"//; s/"$//'
}

# =========================================================================

main() {
    case "${1:-}" in
        -h|--help) usage; return 0 ;;
    esac

    local home state heartbeat inventory conf
    home="$(resolve_home)"
    state="$home/state"
    heartbeat="$state/heartbeat.json"
    inventory="$state/inventory.json"
    conf="$home/helper.conf"

    printf '# 実機採取 — %s\n\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
    printf '`probe.sh` の出力。**要約せずこのまま貼る**（docs/POC.md §0）。\n\n'
    printf -- '- VOICEDOCK_HOME : `%s`\n' "$home"
    printf -- '- VOLUMES_ROOT   : `%s`\n\n' "$VOLUMES_ROOT"

    # 対象デバイス。引数が無ければ inventory から拾う
    local devices=()
    if [ "$#" -gt 0 ]; then
        devices=("$@")
    else
        local name
        while IFS= read -r name; do
            [ -n "$name" ] && devices+=("$name")
        done <<EOF
$(inventory_devices "$inventory")
EOF
    fi

    report_p0_8 "$heartbeat" "$conf" ${#devices[@]} ${devices[@]+"${devices[@]}"}
    report_p0_10 "$inventory" ${#devices[@]} ${devices[@]+"${devices[@]}"}
    report_p0_11 "$conf" "$inventory" ${#devices[@]} ${devices[@]+"${devices[@]}"}
    report_p0_13 "$inventory"
    report_p0_15 "$home"
    report_raw "$home" "$heartbeat" "$inventory" "$conf" ${#devices[@]} ${devices[@]+"${devices[@]}"}

    if [ "$mismatch" -ne 0 ]; then
        printf '\n**P0-8 が食い違った。実装のバグである**（issue #3）。採り直しても直らない。\n'
        return "$EXIT_PROBE_MISMATCH"
    fi
    return 0
}

# --- P0-8: `mount` と `heartbeat.json` の一致（最重要） ------------------
# ingest は「**検出したデバイスが 1 つ以上あり、その全部が read-only**」のときだけ
# `mount_readonly: true` を書く。probe は同じ式を `mount` から独立に組み立てて突き合わせる。

report_p0_8() {            # $1=heartbeat, $2=conf, $3=件数, $4...=devices
    local heartbeat="$1" conf="$2" count="$3"; shift 3
    local reported observed="true" state name absent=0 mode

    printf '## P0-8 マウントモードと heartbeat の一致\n\n'

    mode="$(conf_value "$conf" MOUNT_MODE)"
    [ -n "$mode" ] || mode="unknown"
    reported="$(json_bool "$heartbeat" mount_readonly)"
    printf -- '- `helper.conf` の `MOUNT_MODE` : `%s`\n' "$mode"
    printf -- '- `heartbeat.json` の `mount_readonly` : `%s`\n' "$reported"
    printf -- '- `heartbeat.json` の `updated_at` : `%s`\n\n' "$(json_scalar "$heartbeat" updated_at)"

    if [ "$count" -eq 0 ]; then
        printf '| デバイス | `mount` | 判定 |\n|---|---|---|\n'
        printf '| （なし） | — | ⬜ 判定不能 |\n\n'
        printf '**デバイスが `inventory.json` に無い。**接続してから `bin/voicedock-ingest` を'
        printf '実行し、採り直す。\n\n'
        return 0
    fi

    printf '| デバイス | `mount` | 読み取り専用 |\n|---|---|---|\n'
    for name in "$@"; do
        state="$(mount_is_readonly "$VOLUMES_ROOT/$name")"
        case "$state" in
            absent) absent=1; observed="unknown" ;;
            false)  observed="false" ;;
        esac
        printf '| `%s` | `%s` | %s |\n' \
            "$name" "$(mount_line "$VOLUMES_ROOT/$name")" "$state"
    done
    printf '\n'

    if [ "$absent" -eq 1 ]; then
        printf -- '- 判定: ⬜ **判定不能** — `inventory.json` に在るデバイスが `mount` に無い。\n'
        printf -- '  **2 つのスナップショットがずれている。**接続し直して `bin/voicedock-ingest`'
        printf -- ' を実行してから採り直す\n\n'
        return 0
    fi

    printf -- '- `mount` からの観測 : `%s`（デバイス %s 個の論理積）\n' "$observed" "$count"

# ロック 2-B が実際にどうなっているかを 1 行で述べる（§14.2）。
#
# **一致しているかどうかとは別の問いである。**`MOUNT_MODE=ro` を意図したのに
# 書き込み可能なら、一致していても**保護はされていない。**
lock_2b_line() {           # $1=観測（true/false）, $2=helper.conf の MOUNT_MODE
    if [ "$1" = "true" ]; then
        printf -- '- ロック 2-B: **効いている**（デバイスは読み取り専用）\n\n'
    elif [ "$2" = "rw" ]; then
        printf -- '- ロック 2-B: **外れている**（`MOUNT_MODE=rw` の意図どおり。Phase 7 の状態）\n\n'
    else
        # **`%s` と引数を同じ printf に置く。**分けると `%s` が空で出る（実機で踏んだ）
        printf -- '- ロック 2-B: ⚠ **かかっていない** — `MOUNT_MODE=%s` を意図したが' "$2"
        printf -- 'デバイスは書き込み可能である。**再マウントが成功していない**\n\n'
    fi
}

    # **`heartbeat.json` が読めないときは「不明」として扱い、FAIL にしない**（§7.5）。
    # Helper が一度も走っていないだけかもしれず、それは実装のバグではない
    if [ "$reported" = "unknown" ]; then
        printf -- '- 判定: ⬜ **判定不能** — `heartbeat.json` の `mount_readonly` が読めない。'
        printf -- '`bin/voicedock-ingest` を実行してから採り直す\n\n'
        return 0
    fi

    if [ "$reported" = "$observed" ]; then
        printf -- '- 判定: ✅ **PASS** — `mount` と `heartbeat.json` が一致した\n'
        # **一致と「ロックが効いている」は別の問いである。**v5.19 までは一致しさえすれば
        # 「ロック 2-B が実際に効いている」と書いていた。2026-09-14 の実機で、
        # `MOUNT_MODE=ro` なのにデバイスが書き込み可能なまま **✅ PASS と表示した**
        # （`docs/POC.md` §10.1.2）。**保護されていないことを保護されていると読ませていた**
        lock_2b_line "$observed" "$mode"
    else
        printf -- '- 判定: ✗ **FAIL** — `mount`=`%s` に対し `heartbeat.json`=`%s`。**一致しないなら実装のバグである**\n\n' \
            "$observed" "$reported"
        mismatch=1
    fi
}

# --- P0-10: デバイスの見え方（送信機 2 台なら 2 エントリ） ---------------

report_p0_10() {           # $1=inventory, $2=件数, $3...=devices
    local inventory="$1" count="$2"; shift 2
    local name

    printf '## P0-10 デバイスの見え方\n\n'
    printf -- '- `inventory.json` の `devices` : **%s 個**\n\n' "$count"
    [ "$count" -eq 0 ] && { printf '⬜ 判定不能。\n\n'; return 0; }

    printf '| デバイス | 録音 | 空き容量 | `df -Pk` |\n|---|---|---|---|\n'
    for name in "$@"; do
        printf '| `%s` | %s | %s | `%s` |\n' \
            "$name" \
            "$(inventory_relpaths_for "$inventory" "$name" | grep -c . || true)" \
            "$(inventory_free_bytes "$inventory" "$name")" \
            "$(df -Pk "$VOLUMES_ROOT/$name" 2>/dev/null | tail -1 || true)"
    done
    printf '\n**2 個なら Daily ノートも 2 枚になること**を Vault で確認する（#3）。\n\n'
}

# --- P0-11: 安定性判定（録音中ファイルの mtime の進み方） ---------------

report_p0_11() {           # $1=conf, $2=inventory, $3=件数, $4...=devices
    local conf="$1" inventory="$2" count="$3"; shift 3
    local interval target name rel i

    interval="$(conf_value "$conf" STABILITY_INTERVAL_SECONDS)"
    # **数値でなければ既定へ倒す。**`helper.conf` が壊れていても採取は続ける（§7.5 と同じ方針）
    case "$interval" in
        ''|*[!0-9]*) interval=3 ;;
    esac

    printf '## P0-11 安定性判定\n\n'
    printf -- '- `STABILITY_FAST_PATH_SECONDS` : `%s`\n' "$(conf_value "$conf" STABILITY_FAST_PATH_SECONDS)"
    printf -- '- `STABILITY_INTERVAL_SECONDS`  : `%s`\n' "$interval"
    printf -- '- `STABILITY_CHECKS`            : `%s`\n\n' "$(conf_value "$conf" STABILITY_CHECKS)"

    [ "$count" -eq 0 ] && { printf '⬜ 判定不能。\n\n'; return 0; }

    # そのデバイスの最後の relpath を対象にする（**録音中のものが最新であることが多い**）
    name="$1"
    rel="$(inventory_relpaths_for "$inventory" "$name" | tail -1)"
    [ -n "$rel" ] || { printf '⬜ 録音が `inventory.json` に無い。\n\n'; return 0; }
    target="$VOLUMES_ROOT/$name/$rel"

    printf -- '対象: `%s`\n\n' "$target"
    printf '| # | size mtime |\n|---|---|\n'
    i=1
    while [ "$i" -le "$PROBE_SAMPLES" ]; do
        printf '| %s | `%s` |\n' "$i" "$(stat_size_mtime "$target")"
        i=$((i + 1))
        [ "$i" -le "$PROBE_SAMPLES" ] && sleep "$interval"
    done
    printf '\n**録音中なら size と mtime が進む。**`file_not_stable` がログに出て'
    printf '次回に回ることを確認する（§10.3）。\n\n'
}

# --- P0-13: `_orig` 以外がデバイスに溜まっているか（§5.3 の代償） -------

report_p0_13() {           # $1=inventory
    local inventory="$1" total others

    total="$(inventory_relpaths "$inventory" | grep -c . || true)"
    others="$(inventory_relpaths "$inventory" | grep -v '_orig\.wav$' | grep -c . || true)"

    printf '## P0-13 `_orig` 以外の有無\n\n'
    printf -- '- `inventory.json` の録音 : **%s 件**\n' "$total"
    printf -- '- うち `_orig` 以外 : **%s 件**\n\n' "$others"
    if [ "$others" -eq 0 ]; then
        printf '**denoised は生成されていない。**§5.3 の代償（読まなかったファイルが'
        printf '溜まり続ける）は現実になっていない。\n\n'
    else
        printf '**denoised が溜まっている。**§22 R-28 の隣に記録し、'
        printf 'DJI 側で原音のみに設定できるかを確認する（#3）。\n\n'
        inventory_relpaths "$inventory" | grep -v '_orig\.wav$' | sed 's/^/    /'
        printf '\n'
    fi
}

# --- P0-15: ingest 1 回の所要時間（**probe は ingest を実行しない**） ---

report_p0_15() {           # $1=home
    printf '## P0-15 ingest 1 回の所要時間\n\n'
    printf '**probe は ingest を実行しない**（状態が変わり、他の項目が採り直しになる）。\n'
    printf '別途これを叩き、結果をここへ貼る:\n\n'
    printf '```bash\ntime "%s/bin/voicedock-ingest"\n```\n\n' "$1"
    printf '`StartInterval`（既定 300 秒）で間に合うことを裏づける（#3）。\n\n'
}

# --- 生の出力（§0 の記録の規約） ----------------------------------------

report_raw() {             # $1=home $2=heartbeat $3=inventory $4=conf $5=件数 $6...=devices
    local home="$1" heartbeat="$2" inventory="$3" conf="$4" count="$5"; shift 5
    local name

    printf '## 生の出力\n\n'

    printf '### `mount`\n\n```text\n'
    if [ "$count" -gt 0 ]; then
        for name in "$@"; do mount_line "$VOLUMES_ROOT/$name"; done
    else
        "$MOUNT_CMD" 2>/dev/null || true
    fi
    printf '```\n\n'

    printf '### `heartbeat.json`\n\n```json\n'
    cat "$heartbeat" 2>/dev/null || printf '（無い）\n'
    printf '```\n\n'

    printf '### `inventory.json`\n\n```json\n'
    cat "$inventory" 2>/dev/null || printf '（無い）\n'
    printf '```\n\n'

    printf '### `helper.conf`（コメントを除く）\n\n```sh\n'
    grep -v '^[[:space:]]*#' "$conf" 2>/dev/null | grep -v '^[[:space:]]*$' || printf '（無い）\n'
    printf '```\n\n'

    printf '### `log/ingest.log`（末尾 40 行）\n\n```text\n'
    tail -40 "$home/log/ingest.log" 2>/dev/null || printf '（無い）\n'
    printf '```\n'
}

main "$@"
