# 実機 E2E 検証（SPEC §20.3）

**この文書が実機手順の正本である。**GitHub issue の本文に手順を置かない。

> **なぜ正本をここに置くか。**issue は git 管理外なので、v5.0 で `scan` / `retry` /
> `pending` / `cleanup` を削除しても **issue 本文の手順は何も落ちずに残った**（#93）。
> ここに置けば `tests/unit/test_runbook.py` が
> **手順に現れる `voicedock <サブコマンド>` の実在を検査する。**

**サブコマンドは 5 本だけである**（§17.1）。`service` / `status` / `doctor` / `health` / `version`。
**引数を取るものは無い。**

**v5.0 で削除した 6 本**（§17.1）。**手順にこの名前を書かない** —
`tests/unit/test_runbook.py` が検査する。

| 削除されたもの | いま何をするか |
|---|---|
| `scan` | **走査は Helper が行う**（§10.1）。`state/inventory.json` に現れるのを待つ |
| `retry` | **デバイス再接続とサービス起動での無条件再評価**（§15.2）。`docker compose restart voicedock` |
| `pending` | `voicedock status` が `SOURCE_DELETE_PENDING` の件数を出す |
| `history` / `show` | `events` テーブルを SQL で読む（§17.1 の例） |
| `cleanup` | Phase 7 で必要になった時点で足す（#38） |

## 0. 記録の規約

`docs/POC.md` §0 と同じ。**要約した数値だけを書かない。生の出力を貼る。**
判定は `✅ PASS` / `✗ FAIL` / `⬜ 未実施` / `— 対象外` のいずれか。**空欄にしない。**

**1 件でも FAIL なら修正チケットを起票し、Phase 7 へ進まない**（§21.1）。

## 1. 前提

```bash
./scripts/doctor.sh                              # ホスト側（§19.2 の DH 表）
docker compose exec voicedock voicedock doctor   # コンテナ側（§19.2 の D 表）
./helper/install.sh --status                     # Helper と LaunchAgent
```

**削除は OFF のまま行う**（E2E-10 / E2E-11 を除く）。三重ロック（§14.2）が
掛かっていることを `voicedock doctor` で確認してから始める。

## 2. 判定表

| # | シナリオ | 判定 | 記録 |
|---|---|---|---|
| E2E-01 | 1 分程度の録音を 1 本。削除 OFF | ⬜ 未実施 | §3.1 |
| E2E-02 | **コピー中に** USB を抜く | ✅ **PASS** | §3.2。**変換中ではない** — 変換は inbox から読むので USB と無関係 |
| E2E-03 | Whisper 実行中に USB を抜く | ⬜ 未実施 | §3.3 |
| E2E-04 | Obsidian Vault を一時的に利用不可にする | ⬜ 未実施 | §3.4 |
| E2E-05 | 同じ Mic を再接続 | ⬜ 未実施 | §3.5 |
| E2E-06 | **1 日分（16 時間・32 Part 相当）を投入** | ⬜ 未実施 | §3.6。**P0-15b を兼ねる**（取り込み時間の実測） |
| E2E-07 | **無音だけの Part を混ぜる** | ⬜ 未実施 | §3.7 |
| E2E-08 | **1 本だけ Whisper を失敗させる** | ⬜ 未実施 | §3.8 |
| E2E-09 | Daily ノート保存後に同じ日の Part を追加投入 | ⬜ 未実施 | §3.9 |
| E2E-10 | 十分な試験後に削除を ON | ⬜ Phase 7 | §3.10 |
| E2E-11 | Phase 7 移行時に後追いの一括削除 | ⬜ Phase 7 | §3.11 |
| E2E-12 | Docker Desktop を再起動 | ⬜ 未実施 | §3.12 |

**E2E-10 / E2E-11 は Phase 7 のものである**（#39 / #38）。
§21.1 は「ND-01〜ND-31 と E2E-01〜E2E-12 の全件 PASS」を Phase 7 の前提としているが、
**E2E-10 / E2E-11 自体が Phase 7 の作業**なので、先に E2E-01〜09 / 12 を通す（#35）。

## 3. シナリオ

各節に **手順 / 期待 / 判定 / 生の出力** を書く。

### 3.1 E2E-01 — 1 本を通しで

削除 OFF。検出 → 変換 → transcript → Raw → LLM → Daily 保存。**元音声が残る。**
**単一チャンクでも Timeline が生成されること。**

```bash
# 1 分程度を 1 本録音し、DJI を USB 接続する（launchd の StartOnMount で ingest が走る）
cat "$VOICEDOCK_HOME/state/inventory.json"          # devices に現れる
tail -f "$VOICEDOCK_HOME/log/ingest.log"            # import_completed

docker compose exec voicedock voicedock status
docker compose logs voicedock | grep -E 'part_discovered|normalize_completed|transcription_completed|raw_note_saved|session_merged|llm_completed|obsidian_saved'

ls -1 "$OBSIDIAN_VAULT/Daily/Voice/Raw/"*/ "$OBSIDIAN_VAULT/Daily/Voice/Wiki/"*/
ls -la "/Volumes/<VOL>/TX_.../"                     # ★元音声が残っていること
```

判定: ⬜ 未実施

### 3.2 E2E-02 — コピー中に USB を抜く

クラッシュしない。**部分出力が削除される。DJI 側の音声が残る。**次回接続で再試行される。

> **危険な窓は「Helper のコピー（デバイス → inbox）」である。**コンテナの変換は
> inbox（内蔵ディスク）から読むので、**そのあいだに USB を抜いても何も起こらない。**
> v5.24 までこの節は `normalize_completed` の前に抜けと書いていたが、
> **それでは何も試験していない。**

#### 手順

**コピーに時間のかかる状態を作る。**inbox の**墓標（`.meta.json`）を外す**と、
その録音は再コピーされる（§10.2）。259 MB のファイル 3 件なら約 62 秒になる。

```bash
# 1. 墓標を外す（**退避しておくこと**）
cp ~/VoiceDock/inbox/<VOL>/<FOLDER>/*.meta.json /tmp/tombstones/
rm ~/VoiceDock/inbox/<VOL>/<FOLDER>/TX00_MIC00{3,5,6}_*.meta.json

# 2. デバイスを挿す → コピーが始まる
# 3. **30 秒ほどで物理的に抜く**

# 4. 確認
grep -E 'copy_failed|copy_size_mismatch' ~/VoiceDock/log/ingest.log
ls -la ~/VoiceDock/inbox/<VOL>/<FOLDER>/   # ★ .partial が残っていないこと
docker compose exec voicedock ls -la /data/staging/

# 5. 再接続して再試行されることを確認（§15.2 の「デバイスの再接続」）
```

> **再コピーの対象は処理済みの録音なので、二重処理は起きない**（partkey が登録済みで
> `part_skipped reason=already_known`。§9.3）。**ただし inbox に取り残しが生まれる** —
> 変換しない Part の原本は `inbox_retain` の経路では消えない（#120）。
> 試験後は手で消すこと。

#### 実測（2026-09-14 23:36）

```text
23:36:13  scanned name=DJIMIC3 files=12 candidates=3
23:36:34  copied  MIC003 bytes=259254376          ← 1 件目は完了
          （MIC005 の .partial が 64 MB まで育ったところで USB を抜いた）
cat: .../TX00_MIC005_...wav: Device not configured
23:36:49  WARN  copy_failed relpath=.../MIC005 pipestatus=1 0 0
cat: .../TX00_MIC006_...wav: No such file or directory
23:36:49  WARN  copy_failed relpath=.../MIC006 pipestatus=1 0 0
23:36:49  INFO  ingest_finished devices=1          ← クラッシュせず正常終了
```

| 確認項目 | 結果 |
|---|---|
| クラッシュしない | ✅ `ingest_finished` まで到達 |
| **部分出力が削除される** | ✅ **64 MB の `.partial` と `.sha` が両方消えている** |
| 失敗の理由が残る | ✅ `pipestatus=1 0 0` — **`cat` だけが失敗**したことが分かる |
| 中途半端なファイルをコンテナが読まない | ✅ 墓標が無いので**候補にすらならない**（§10.2） |
| **DJI 側の音声が残る** | ✅ **12 件すべてサイズ一致**（下記） |
| **次回接続で再試行される** | ✅ `candidates=2` → MIC005 / MIC006 とも完了 |

**抜いた瞬間に読んでいた MIC005 を含め、デバイス上の 12 件は 1 バイトも変わっていない:**

```text
856456     TX00_MIC001_20260912_163444_orig.wav
2963176    TX00_MIC002_20260913_233420_orig.wav
259254376  TX00_MIC003_20260914_115939_orig.wav
224865736  TX00_MIC004_20260914_122940_orig.wav
259254376  TX00_MIC005_20260914_150918_orig.wav   ← コピー中に抜いた
259254376  TX00_MIC006_20260914_153918_orig.wav
259254376  TX00_MIC007_20260914_160918_orig.wav
259254376  TX00_MIC008_20260914_163918_orig.wav
259254376  TX00_MIC009_20260914_170919_orig.wav
183665896  TX00_MIC010_20260914_173919_orig.wav
1236616    TX00_MIC011_20260914_202800_orig.wav
1648456    TX00_MIC012_20260914_230003_orig.wav
```

再接続後（23:39）:

```text
23:39:13  scanned name=DJIMIC3 files=12 candidates=2
23:39:34  copied  MIC005 bytes=259254376
23:39:54  copied  MIC006 bytes=259254376
23:39:54  ingest_finished devices=1
```

判定: ✅ **PASS**

> **ここで #120 が見つかった。**再コピーで inbox に残った `.wav` は partkey が
> `COMPLETED` なので二度と処理されないが、`voicedock status` は
> **`Inbox : 1 parts pending`** と報告し続けた。`Backlog : 未処理なし` と同時に出るため、
> **どちらを信じればよいか分からない。**

### 3.3 E2E-03 — Whisper 実行中に USB を抜く

**Whisper 以降が継続する**（staging に変換済みが在るため）。Raw / Daily が保存される。
`SOURCE_DELETE_PENDING` になる。

```bash
# `transcription_completed` の前に抜く
docker compose logs voicedock | grep -E 'transcription_completed|obsidian_saved|source_delete'
docker compose exec voicedock voicedock status        # ★SOURCE_DELETE_PENDING の件数
```

判定: ⬜ 未実施

### 3.4 E2E-04 — Vault を利用不可にする

**source 削除されない。**復帰できる。

> **`retry` サブコマンドは無い**（v5.0 で削除）。**復帰の契機は「デバイスの再接続」と
> 「サービスの起動」の 2 つだけである**（§15.2）。ここではサービスを再起動する。

```bash
mv "$OBSIDIAN_VAULT" "$OBSIDIAN_VAULT.bak"
docker compose logs voicedock | grep -E 'obsidian_not_found|OBSIDIAN_NOT_FOUND'
docker compose exec voicedock voicedock status        # FAILED として残る

mv "$OBSIDIAN_VAULT.bak" "$OBSIDIAN_VAULT"
docker compose restart voicedock                      # ★サービス起動 = 再評価の契機
docker compose logs voicedock | grep -E 'recovery_completed|obsidian_saved'
ls -la "/Volumes/<VOL>/TX_.../"                       # ★元音声が残っていること
```

判定: ⬜ 未実施

### 3.5 E2E-05 — 同じ Mic を再接続

**既処理ファイルを再度ノート化しない。**delete pending のみ処理される。

```bash
docker compose exec voicedock voicedock status        # 接続前の件数を控える
# DJI を抜いて挿し直す
grep -E 'already_known|import_skipped' "$VOICEDOCK_HOME/log/ingest.log"
docker compose exec voicedock voicedock status        # ★件数が増えないこと
```

判定: ⬜ 未実施

### 3.6 E2E-06 — 1 日分（32 Part 相当）

**Daily 1 枚 + Raw 1 枚に集約される。Timeline から時間帯を辿れる。8 時間以内に終わる。**

```bash
date; docker compose exec voicedock voicedock status  # 開始時刻と開始前の状態
# DJI を接続し、Helper の取り込みを待つ（走査は Helper が行う。§10.1）
docker compose logs voicedock | grep -E 'session_merged|obsidian_saved'
date

docker compose logs voicedock | ./scripts/perf-report.sh   # ★8 時間判定
ls -1 "$OBSIDIAN_VAULT/Daily/Voice/Raw/"*/ "$OBSIDIAN_VAULT/Daily/Voice/Wiki/"*/
#   ★Raw 1 枚 + Daily 1 枚であること。Obsidian で Timeline を目視確認する
```

判定: ⬜ 未実施

### 3.7 E2E-07 — 無音だけの Part

`SKIPPED` になり、**セッション全体は停止しない。**Daily ノートが生成される。

```bash
docker compose logs voicedock | grep -E 'part_skipped|NO_SPEECH_DETECTED'
docker compose exec voicedock voicedock status        # ★SKIPPED があっても止まらない
```

判定: ⬜ 未実施

### 3.8 E2E-08 — 1 本だけ Whisper を失敗させる

他の Part で Daily が生成され、警告行と `voicedock_failed_parts` に記録される。
**失敗 Part の音声は削除されない。**

```bash
docker compose logs voicedock | grep -E 'transcription_failed|session_merged'
grep -A3 voicedock_failed_parts "$OBSIDIAN_VAULT/Daily/Voice/Wiki/<日付>/"*.md
ls -la "/Volumes/<VOL>/TX_.../"                       # ★失敗 Part の音声が残っていること
```

判定: ⬜ 未実施

### 3.9 E2E-09 — 保存後に同じ日の Part を追加投入

**セッションが再オープンされ、同じファイルが更新される（増殖しない）。**

```bash
ls -1 "$OBSIDIAN_VAULT/Daily/Voice/Wiki/<日付>/"      # 追加前のファイル数
# 同じ日の録音をもう 1 本入れて接続する
docker compose logs voicedock | grep -E 'session_reopened|obsidian_saved'
ls -1 "$OBSIDIAN_VAULT/Daily/Voice/Wiki/<日付>/"      # ★ (2) が増えていないこと
```

判定: ⬜ 未実施

### 3.10 E2E-10 — 削除を ON（**Phase 7**）

保存確認後にのみ DJI 側 WAV が削除される。`_orig` と denoised の両方が消える。

**§21.1 のハードゲートを満たすまで実施しない**（#39）。三重ロックを 3 つとも外す作業である。

判定: ⬜ Phase 7

### 3.11 E2E-11 — 後追いの一括削除（**Phase 7**）

既に `COMPLETED` の Part も §14.1 が真なら削除される。

**入口そのものが未実装である**（#38）。`cleanup` サブコマンドは v5.0 で削除されており、
**Phase 7 に入るまで存在しないほうが安全である**（削除の入口を 1 本に保てる。§17.1）。

判定: ⬜ Phase 7

### 3.12 E2E-12 — Docker Desktop を再起動

**途中状態から再開する。二重処理しない。**

```bash
docker compose exec voicedock voicedock status        # 再起動前の件数を控える
osascript -e 'quit app "Docker"'                      # 再起動する
docker compose logs voicedock | grep -E 'recovery_completed|service_started'
docker compose exec voicedock voicedock status        # ★二重処理されていないこと
```

判定: ⬜ 未実施

## 4. §21.3 MVP 完成条件

⬜ 未実施。削除に関わらない項目を表で確認する（#35）。
