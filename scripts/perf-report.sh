#!/bin/bash
#
# perf-report.sh — ログから Phase 2 / Phase 3 の受け入れ条件を判定する（#93）
#
# **`voicedock` のログを stdin から読むだけで、何も変えない。**
#
#   docker compose logs voicedock | ./scripts/perf-report.sh
#
# `logging.format` は `text` / `json` のどちらでもよい（既定は `text`）。
# **`elapsed_s` / `rtf` / `speech_ratio` / `chars` / `chunks` は両形式とも引用されない数値**
# なので、`"key":` を `key=` へ正規化すれば同じ抽出で足りる。
#
# **8 時間判定は平均 rtf からの換算ではなく、実 `elapsed_s` の合計と Part 数からの外挿で行う。**
# 平均 rtf からの換算は Part 長のばらつきを潰し、短い Part ばかり測ったときに楽観へ倒れる。
#
# 終了コード: 0=達成 / 6=未達 / 7=対象のログが 1 件も無い
#
# bash 3.2 で書く（macOS 同梱。§3.3）。**`jq` も `bc` も使わない** — 計算は `awk` で行う。

set -euo pipefail

# --- SPEC の数値（`tests/unit/test_perf_report.py` が §21.2 / §20.3 と突き合わせる） ---
DAY_HOURS=16          # §21.2 Phase 2「1 日分（16 時間）」
LIMIT_HOURS=8         # §21.2 Phase 2「8 時間以内に処理しきる」
DAY_PARTS=32          # §20.3 E2E-06「16 時間・32 Part 相当」
DAY_CHUNKS=18         # §21.2 Phase 3「約 18 チャンク」
LIMIT_MINUTES=30      # §21.2 Phase 3「30 分以内」

EXIT_BELOW_TARGET=6
EXIT_NO_DATA=7

WANT_ASR=1
WANT_LLM=1

usage() {
    cat <<'USAGE'
perf-report.sh — Phase 2 / Phase 3 の受け入れ条件を判定する（SPEC §21.2）

  docker compose logs voicedock | ./scripts/perf-report.sh [--asr|--llm]

  --asr   文字起こしだけ（§21.2 Phase 2。8 時間）
  --llm   LLM だけ（§21.2 Phase 3。30 分）

終了コード: 0=達成 / 6=未達 / 7=対象のログが無い
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --asr) WANT_LLM=0 ;;
        --llm) WANT_ASR=0 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'perf-report.sh: 不明な引数: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# **`"key":` を `key=` へ正規化し、区切りを空白に均す。**これで text と json が同じ形になる。
# **コロンの後の空白も一緒に食う** — `json.dumps` の既定は `"key": value` と空ける。
# 食わないと `key=` と `value` が別のトークンになり、**1 件も拾えないまま「0 件」と言う。**
# 数値だけを読むので、引用符の中のカンマで壊れる心配は無い
normalize() {
    sed 's/"\([a-z_][a-z_0-9]*\)"[[:space:]]*:[[:space:]]*/\1=/g' | tr ',{}' '   '
}

normalize | awk \
    -v want_asr="$WANT_ASR" -v want_llm="$WANT_LLM" \
    -v day_hours="$DAY_HOURS" -v limit_hours="$LIMIT_HOURS" \
    -v day_parts="$DAY_PARTS" -v day_chunks="$DAY_CHUNKS" -v limit_minutes="$LIMIT_MINUTES" \
    -v below="$EXIT_BELOW_TARGET" -v nodata="$EXIT_NO_DATA" '
function value(token,    a) {
    if (split(token, a, "=") < 2) return ""
    gsub(/^"|"$/, "", a[2])
    return a[2]
}
function numeric(v) { return (v != "" && v != "null" && v + 0 == v) }
function tail_of(key,   n, a) { n = split(key, a, "/"); return a[n] }

/transcription_completed/ {
    e = ""; c = ""; r = ""; s = ""; k = ""
    for (i = 1; i <= NF; i++) {
        if ($i ~ /^elapsed_s=/)        e = value($i)
        else if ($i ~ /^chars=/)       c = value($i)
        else if ($i ~ /^rtf=/)         r = value($i)
        else if ($i ~ /^speech_ratio=/) s = value($i)
        else if ($i ~ /^recording_key=/) k = value($i)
    }
    if (!numeric(e)) next
    parts++
    part_key[parts] = k; part_e[parts] = e; part_c[parts] = c
    part_r[parts] = r;   part_s[parts] = s
    sum_e += e
    if (numeric(c)) sum_c += c
    if (numeric(r)) { sum_r += r; n_r++ }
    if (numeric(s)) { sum_s += s; n_s++ }
}

/llm_completed/ {
    e = ""; n = ""; k = ""
    for (i = 1; i <= NF; i++) {
        if ($i ~ /^elapsed_s=/)      e = value($i)
        else if ($i ~ /^chunks=/)    n = value($i)
        else if ($i ~ /^session_key=/) k = value($i)
    }
    if (!numeric(e) || !numeric(n) || n + 0 == 0) next
    sessions++
    sess_key[sessions] = k; sess_e[sessions] = e; sess_n[sessions] = n
    llm_sum_e += e; llm_sum_n += n
}

END {
    status = 0
    seen = 0

    if (want_asr) {
        printf "## 文字起こし（SPEC §21.2 Phase 2）\n\n"
        if (parts == 0) {
            printf "⬜ `transcription_completed` が 1 件も無い。\n\n"
        } else {
            seen = 1
            printf "| # | Part | elapsed_s | chars | rtf | speech_ratio |\n"
            printf "|---|---|---|---|---|---|\n"
            for (i = 1; i <= parts; i++)
                printf "| %d | `%s` | %s | %s | %s | %s |\n", i, tail_of(part_key[i]),
                    part_e[i], part_c[i], (part_r[i] == "" ? "—" : part_r[i]),
                    (part_s[i] == "" ? "—" : part_s[i])
            printf "\n"
            printf "- Part 数 : **%d**\n", parts
            printf "- 実 `elapsed_s` 合計 : **%.2f 時間**（%.1f 秒）\n", sum_e / 3600, sum_e
            printf "- 文字数合計 : %d\n", sum_c
            if (n_r > 0) printf "- 平均 `rtf` : %.3f\n", sum_r / n_r
            if (n_s > 0) printf "- 平均 `speech_ratio` : %.3f\n", sum_s / n_s

            day = sum_e / parts * day_parts / 3600
            printf "- **1 日分（%d 時間 / %d Part 相当）への外挿 : %.2f 時間**", day_hours, day_parts, day
            if (parts >= day_parts) printf "（%d Part 以上を実測しているので外挿ではなく平均値である）", day_parts
            printf "\n"
            if (day <= limit_hours) {
                printf "- 判定: ✅ **PASS** — %d 時間以内\n\n", limit_hours
            } else {
                printf "- 判定: ✗ **FAIL** — %d 時間を超える。`large-v3-turbo-q5_0` → `medium-q5_0` → `small-q5_1` と落として再測定する（§21.2）\n\n", limit_hours
                status = below
            }
        }
    }

    if (want_llm) {
        printf "## LLM（SPEC §21.2 Phase 3）\n\n"
        if (sessions == 0) {
            printf "⬜ `llm_completed` が 1 件も無い。\n\n"
        } else {
            seen = 1
            printf "| # | Session | chunks | elapsed_s | 1 チャンクあたり |\n"
            printf "|---|---|---|---|---|\n"
            for (i = 1; i <= sessions; i++)
                printf "| %d | `%s` | %s | %s | %.1f 秒 |\n", i, sess_key[i],
                    sess_n[i], sess_e[i], sess_e[i] / sess_n[i]
            printf "\n"
            per = llm_sum_e / llm_sum_n
            day = per * day_chunks / 60
            printf "- チャンク合計 : %d / 所要合計 : %.1f 秒\n", llm_sum_n, llm_sum_e
            printf "- 1 チャンクあたり : **%.1f 秒**\n", per
            printf "- **1 日分（約 %d チャンク）への換算 : %.1f 分**\n", day_chunks, day
            if (day <= limit_minutes) {
                printf "- 判定: ✅ **PASS** — %d 分以内\n\n", limit_minutes
            } else {
                printf "- 判定: ✗ **FAIL** — %d 分を超える。モデル段を下げる（MoE 30B → 密 14B → 4B。§21.2）\n\n", limit_minutes
                status = below
            }
        }
    }

    if (!seen) exit nodata
    exit status
}
'
