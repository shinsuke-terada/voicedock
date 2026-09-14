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
# **8 時間判定は「測った音声の長さ」を基準に外挿する。**
#
# 平均 `rtf` からの換算は Part 長のばらつきを潰す。かといって **Part 数で外挿するのも誤る** —
# 26 秒の録音 2 本で測ると「1 Part = 13 秒」として 32 倍し、**0.25 時間で PASS と答える**
# （2026-09-14 に実機で踏んだ。#95）。実際の 1 Part は 30 分である。
#
# 音声長で外挿すれば、少なくとも**保守側**へ倒れる（1 回あたりのモデル読み込みという
# 固定費が短い録音では相対的に大きく出るため）。**そして測った音声が 1 日分に遠く
# 及ばないときは判定しない** — 偽の PASS を掴むくらいなら「足りない」と言う。
#
# 音声長はログに無いが `duration = elapsed_s / rtf` で復元できる。
#
# 終了コード: 0=達成 / 6=未達 / 7=対象のログが 1 件も無い / 8=判定に足るデータが無い
#
# bash 3.2 で書く（macOS 同梱。§3.3）。**`jq` も `bc` も使わない** — 計算は `awk` で行う。

set -euo pipefail

# --- SPEC の数値 -----------------------------------------------------------
# **`# SPEC ` で始まる註釈が目印である。**`tests/unit/test_perf_report.py` が
# この印の付いた定数だけを拾って §21.2 / §20.3 と突き合わせる。
# 印で拾うのは、**この道具の方針にすぎない定数（下の MIN_COVERAGE_PERCENT など）を
# 巻き込まないため**である。
DAY_HOURS=16          # SPEC §21.2 Phase 2「1 日分（16 時間）」
LIMIT_HOURS=8         # SPEC §21.2 Phase 2「8 時間以内に処理しきる」
DAY_PARTS=32          # SPEC §20.3 E2E-06「16 時間・32 Part 相当」
DAY_CHUNKS=18         # SPEC §21.2 Phase 3「約 18 チャンク」
LIMIT_MINUTES=30      # SPEC §21.2 Phase 3「30 分以内」

EXIT_BELOW_TARGET=6
EXIT_NO_DATA=7
EXIT_INSUFFICIENT=8

# 1 日分の音声の何 % を測れば判定してよいか。**SPEC の数値ではなく、この道具の方針である。**
# 下回ったら PASS とも FAIL とも言わない（`⬜ 判定保留`）
MIN_COVERAGE_PERCENT=10

WANT_ASR=1
WANT_LLM=1

usage() {
    cat <<'USAGE'
perf-report.sh — Phase 2 / Phase 3 の受け入れ条件を判定する（SPEC §21.2）

  docker compose logs voicedock | ./scripts/perf-report.sh [--asr|--llm]

  --asr   文字起こしだけ（§21.2 Phase 2。8 時間）
  --llm   LLM だけ（§21.2 Phase 3。30 分）

終了コード: 0=達成 / 6=未達 / 7=対象のログが無い / 8=判定に足るデータが無い
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
    -v below="$EXIT_BELOW_TARGET" -v nodata="$EXIT_NO_DATA" \
    -v insufficient="$EXIT_INSUFFICIENT" -v min_coverage="$MIN_COVERAGE_PERCENT" '
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
    # **音声長はログに無いので `elapsed_s / rtf` で復元する**（`transcribe.metrics`）
    part_d[parts] = (numeric(r) && r + 0 > 0) ? e / r : ""
    if (part_d[parts] != "") sum_d += part_d[parts]
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
            printf "| # | Part | 音声 s | elapsed_s | chars | rtf | speech_ratio |\n"
            printf "|---|---|---|---|---|---|---|\n"
            for (i = 1; i <= parts; i++)
                printf "| %d | `%s` | %s | %s | %s | %s | %s |\n", i, tail_of(part_key[i]),
                    (part_d[i] == "" ? "—" : sprintf("%.1f", part_d[i])),
                    part_e[i], part_c[i], (part_r[i] == "" ? "—" : part_r[i]),
                    (part_s[i] == "" ? "—" : part_s[i])
            printf "\n"
            printf "- Part 数 : **%d**\n", parts
            printf "- 実 `elapsed_s` 合計 : **%.2f 時間**（%.1f 秒）\n", sum_e / 3600, sum_e
            printf "- 文字数合計 : %d\n", sum_c
            if (n_r > 0) printf "- 平均 `rtf` : %.3f\n", sum_r / n_r
            if (n_s > 0) printf "- 平均 `speech_ratio` : %.3f\n", sum_s / n_s

            day_seconds = day_hours * 3600
            if (sum_d <= 0) {
                printf "- 判定: ⬜ **保留** — `rtf` が無く音声長を復元できない\n\n"
                status = (status == 0 ? insufficient : status)
            } else {
                coverage = sum_d / day_seconds * 100
                printf "- 測った音声の長さ : **%.1f 秒**（1 日分 %d 時間の **%.2f%%**）\n",
                    sum_d, day_hours, coverage
                day = sum_e / sum_d * day_seconds / 3600
                printf "- **1 日分（%d 時間）への外挿 : %.2f 時間**（音声長あたりの所要で換算）\n",
                    day_hours, day
                if (coverage < min_coverage) {
                    printf "- 判定: ⬜ **保留** — 測った音声が 1 日分の %d%% に満たない（%.2f%%）。\n",
                        min_coverage, coverage
                    printf "  **短い録音では 1 回あたりのモデル読み込み（固定費）が相対的に大きく出る**ので、\n"
                    printf "  この外挿は悲観へ倒れる。**Part 数で外挿すると逆に楽観へ倒れる** — どちらも当てにならない。\n"
                    printf "  30 分程度の Part を数本流してから判定すること（§21.2 Phase 2）\n\n"
                    status = (status == 0 ? insufficient : status)
                } else if (day <= limit_hours) {
                    printf "- 判定: ✅ **PASS** — %d 時間以内\n\n", limit_hours
                } else {
                    printf "- 判定: ✗ **FAIL** — %d 時間を超える。`large-v3-turbo-q5_0` → `medium-q5_0` → `small-q5_1` と落として再測定する（§21.2）\n\n", limit_hours
                    status = below
                }
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
