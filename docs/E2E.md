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
| E2E-01 | 1 分程度の録音を 1 本。削除 OFF | ✅ **PASS** | §3.1 |
| E2E-02 | **コピー中に** USB を抜く | ✅ **PASS** | §3.2。**変換中ではない** — 変換は inbox から読むので USB と無関係 |
| E2E-03 | Whisper 実行中に USB を抜く | ✅ **PASS** | §3.3。**判定条件の `SOURCE_DELETE_PENDING` は v3.x の名残**（削除が無効なら `COMPLETED`） |
| E2E-04 | Obsidian Vault を一時的に利用不可にする | ✅ **PASS** | §3.4。**v5.33 まで手順が空振りしていた**（#134） |
| E2E-05 | 同じ Mic を再接続 | ✅ **PASS** | §3.5 |
| E2E-06 | **1 日分（16 時間・32 Part 相当）を投入** | ⬜ **運用の中で確認する** | **#125**。P0-14 / P0-15b と Docker Desktop の再起動を兼ねる。**リリースはこれを待たない** |
| E2E-07 | **無音だけの Part を混ぜる** | ✅ **PASS** | §3.7。**この試験で #140 が見つかった** |
| E2E-08 | **1 本だけ Whisper を失敗させる** | ✅ **PASS** | §3.8。**この試験で #131 / #133 / #135 が見つかった** |
| E2E-09 | Daily ノート保存後に同じ日の Part を追加投入 | ✅ **PASS** | §3.9 |
| E2E-10 | 十分な試験後に削除を ON | ✅ **PASS** | §3.10。**この試験で #151 / #152 / #154 / #156 が見つかった** |
| E2E-11 | Phase 7 移行時に後追いの一括削除 | ✅ **PASS** | §3.11。**`--backlog` は 0 件が正しい**（古い Part は §14.1 が偽） |
| E2E-12 | Docker Desktop を再起動 | ✅ **PASS**（コンテナ再起動で実施） | §3.12 |

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

#### 実測（2026-09-14 23:00。11 秒の録音 1 本）

全段階のログが出た:

```text
23:00:20  stability_pending count=1
23:00:26  copied relpath=.../TX00_MIC012_20260914_230003_orig.wav bytes=1648456
23:00:29  part_discovered ... duration=11.22
23:00:29  normalize_completed in_bytes=1648456 out_bytes=... elapsed_s=0.0
23:00:53  transcription_completed chars=24 rtf=2.13 speech_ratio=0.826
23:00:53  raw_note_saved session_key=DJIMIC3:20260914 parts=10
23:00:53  session_merged parts=10 chars=18737
23:03:15  analysis_trimmed fields="reduce: key_points: 22 -> 20"
23:03:15  llm_completed chunks=6 elapsed_s=142.6
23:03:15  obsidian_saved path="Daily/Voice/Wiki/20260914/2026-09-14 Voice.md"
```

**元音声は残っている**（デバイス上の 12 件はサイズ不変。§3.2 の一覧を参照）。

#### 単一チャンクでも Timeline が生成されること

**これは別の日のセッションで確認できる。**`DJIMIC3:20260912` は **1 Part・26 秒**で、
`llm.analyze_session()` の**単一パス経路**（チャンク 1 個）を通る。

```text
parts: 1
blocks: 1
## Summary
## Timeline
### 16:34–16:34     ← **Map 中間結果が無くても見出しが出ている**
## Key Points
```

§13.4 の代替経路（Block ごとに `summary` の各文を箇条書きにする）が働いている。
**Timeline のためだけに LLM を再度呼んでいない。**

判定: ✅ **PASS**

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

> **削除が無効なあいだは `COMPLETED` になる。**v3.x の判定条件は
> 「`SOURCE_DELETE_PENDING` になる」だったが、**あれは削除を有効にしたあとの姿**である。
> 安全ロック 1（`cleanup.delete_source_audio: false`）が掛かっていると
> §9.3 の `RAW_SAVED → COMPLETED` を通るので、**要求そのものが書かれない。**
> Phase 7 で有効化したら E2E-10 と併せて再確認する。

> **10 秒の録音では取れない。**whisper の RTF は 0.131（§12.3）なので、
> **10 秒の音声は 1.3 秒で終わる。**抜く窓を作るには**数分の録音が要る。**
> 無音だと VAD が全区間を飛ばすのでさらに一瞬で終わる —— **E2E-07 と同じ録音では
> 両立しない。**

```bash
# `transcription_completed` の前に抜く
docker compose logs voicedock | grep -E 'transcription_completed|obsidian_saved'
docker compose exec voicedock voicedock status        # ★COMPLETED になること
ls ~/VoiceDock/queue/delete/                          # ★空であること（削除は無効）
```

#### 実測（2026-09-15。203.6 秒の録音）

```text
14:36:09  normalize_completed recording_key=…MIC020… in_bytes=29352616 out_bytes=6515656 elapsed_s=0.1
          ← ここから whisper。**14:36:29 に USB を抜いた**（開始 20 秒後）
14:37:08  transcription_completed recording_key=…MIC020… elapsed_s=58.5 chars=116 rtf=0.287 speech_ratio=0.959
14:37:08  raw_note_saved session_key=DJIMIC3:20260915 parts=4 bytes=1986
14:37:42  llm_completed session_key=DJIMIC3:20260915 chunks=2 elapsed_s=33.6
14:37:42  obsidian_saved session_key=DJIMIC3:20260915 bytes=5763
```

| 判定条件 | 結果 |
|---|---|
| Whisper が完走する | ✅ 抜去後も 39 秒走り切った（全 58.5 秒） |
| Raw が保存される | ✅ `raw_note_saved parts=4` |
| Daily が保存される | ✅ `obsidian_saved bytes=5763`（LLM も抜去後に走った） |
| Part が `COMPLETED` | ✅ |
| 削除要求が書かれない | ✅ `queue/delete/` は空 |

**変換も文字起こしも LLM もデバイスを見ない。**§10.5 以降は `/data/staging` と
`/data/transcripts` だけで完結するので、**USB の有無と無関係である。**
E2E-02 の注記（変換は inbox から読むので USB と無関係）が文字起こし以降にも当てはまる
ことを実機で確かめた。

判定: ✅ **PASS**

### 3.4 E2E-04 — Vault を利用不可にする

**source 削除されない。**復帰できる。

> **`retry` サブコマンドは無い**（v5.0 で削除）。**復帰の契機は「デバイスの再接続」と
> 「サービスの起動」の 2 つだけである**（§15.2）。ここではサービスを再起動する。

```bash
mv "$OBSIDIAN_VAULT" "$OBSIDIAN_VAULT.bak"
docker compose restart voicedock                      # ★Docker が空ディレクトリを作る
docker compose logs voicedock | grep -E 'obsidian_not_found|OBSIDIAN_NOT_FOUND'
docker compose exec voicedock voicedock status        # FAILED として残る
docker compose exec voicedock voicedock doctor        # ★D-13 が「.obsidian/ がありません」

mv "$OBSIDIAN_VAULT.bak" "$OBSIDIAN_VAULT"            # ★空ディレクトリを先に消すこと
docker compose restart voicedock                      # ★サービス起動 = 再評価の契機
docker compose logs voicedock | grep -E 'recovery_completed|obsidian_saved'
ls -la "/Volumes/<VOL>/TX_.../"                       # ★元音声が残っていること
```

#### この手順は 2026-09-15 まで空振りしていた（#134）

**`mv` で退避しても `obsidian_saved` が成功し続けた。**

```text
12:15:39  service_started version=0.1.0 schema_version=1
12:15:52  obsidian_saved session_key=DJIMIC3:20260915 path="…/2026-09-15 Voice.md" bytes=2320
12:16:10  obsidian_saved session_key=DJIMIC3:20260915 path="…/2026-09-15 Voice.md" bytes=2320
```

**Docker は bind mount の source が無ければ空ディレクトリとして作る。**再起動で
マウントが貼り直され、VoiceDock は**中身の無い「幻の Vault」**へ書いていた。

```text
$ find "$OBSIDIAN_VAULT"
…/Obsidian Vault/Daily/Voice/Wiki/20260915/2026-09-15 Voice.md   ← これ 1 枚だけ
```

H-4 も D-13 も「読み書きできるか」しか見ておらず、空ディレクトリは両方とも通っていた。
**§14.1 の削除条件（テキストが Vault に残っていること）が、幻の Vault でも成立していた。**

v5.34 で `obsidian.vault_marker`（既定 `.obsidian`）を追加し、**目印の無い場所へは
書かない**ようにした（§13.6 手順 0）。**上の手順はそのままで検査として成立する。**

#### 実測（2026-09-15。v5.34 で再実施）

**前半 — Vault を退避して再起動**

```text
13:32:07  service_started version=0.1.0 schema_version=1
13:32:20  ERROR obsidian_failed session_key=DJIMIC3:20260915 error_code=OBSIDIAN_NOT_FOUND
          reason=vault detail="/obsidian に .obsidian/ がありません（Vault が未マウントか、別の場所を指しています）"
```

| 判定条件 | 結果 |
|---|---|
| `OBSIDIAN_NOT_FOUND` が出る | ✅ 何が足りないかを文言で言う |
| **幻の Vault へ書かない** | ✅ Docker が作った空ディレクトリは**0 バイトのまま**（v5.33 まではノート 1 枚が書かれていた） |
| Session が `FAILED` として残る | ✅ `WRITING → FAILED` を 3 回。`retry_count=2` を経て `FAILED` |
| **元音声が残る** | ✅ デバイス側 5022376 bytes 健在 |
| 削除要求が書かれない | ✅ `queue/delete/` は空 |
| `doctor` が原因を言う | ✅ D-13 が `✗`。**権限エラーとは別の文言**（`.obsidian/ がありません。…新しい Vault なら Obsidian で 1 度開いてください`） |
| `healthcheck` が落ちる | ✅ `unhealthy: H-4` |

**後半 — Vault を戻して再起動**

```text
13:44:06  service_started version=0.1.0 schema_version=1
13:44:06  recovery_completed requeued=2
13:44:19  obsidian_saved session_key=DJIMIC3:20260915 path="…/2026-09-15 Voice.md" bytes=2320
13:44:37  session_merged session_key=DJIMIC3:20260915 parts=3 excluded=1 chars=300
13:44:37  obsidian_saved session_key=DJIMIC3:20260915 path="…/2026-09-15 Voice.md" bytes=2320
```

Session は `COMPLETED` / `error_code=None` / `retry_count=0` へ戻り、D-13 も `[✓]` に戻った。
**人が打ったコマンドは `mv` と `docker compose restart` だけである**（`retry` は存在しない）。

> **`docker ps` の `STATUS` 列には最大 6 分遅れて出る**（`interval: 120s` × `retries: 3`）。
> healthcheck のログ（`docker inspect` / `make logs`）には即座に出るので、
> **気づく手段としてはログのほうが速い。**

> **空ディレクトリは手で消す必要がある。**Docker が作ったものなので、
> `mv` で戻す前に `rmdir` する（**`rm -rf` にしない** — 空でなければ失敗してほしい）。

判定: ✅ **PASS**

### 3.5 E2E-05 — 同じ Mic を再接続

**既処理ファイルを再度ノート化しない。**delete pending のみ処理される。

```bash
docker compose exec voicedock voicedock status        # 接続前の件数を控える
# DJI を抜いて挿し直す
grep -E 'already_known|import_skipped' "$VOICEDOCK_HOME/log/ingest.log"
docker compose exec voicedock voicedock status        # ★件数が増えないこと
```

#### 実測（2026-09-14。抜き挿しを 6 回以上）

```text
23:25:29  scanned name=DJIMIC3 files=12 candidates=0
23:30:12  scanned name=DJIMIC3 files=12 candidates=0
23:36:13  scanned name=DJIMIC3 files=12 candidates=3   ← 墓標を外した分だけ（E2E-02）
23:39:13  scanned name=DJIMIC3 files=12 candidates=2   ← 抜いて失敗した分だけ
23:44:55  scanned name=DJIMIC3 files=12 candidates=0
```

**`files=12` に対し `candidates=0`。**墓標（`.meta.json`）があるものは `stat` すら呼ばれない（§10.2）。
Part の件数は **`COMPLETED: 12` のまま増えていない。**

判定: ✅ **PASS**

### 3.6 E2E-06 — 1 日分（32 Part 相当）

**Daily 1 枚 + Raw 1 枚に集約される。Timeline から時間帯を辿れる。8 時間以内に終わる。**

> **「8 時間以内」は密な発話では満たせない**（2026-09-15 実測。#125）。
>
> | | 音声 | 所要 | RTF | 文字数 |
> |---|---|---|---|---|
> | POC part 1 | 30.1 分 | 153.3 秒 | 0.085 | **688** |
> | POC part 2 | 26.0 分 | 204.5 秒 | 0.131 | **667** |
> | 実運用 1 | 30.0 分 | **31.2 分** | **1.04** | **7,768** |
> | 実運用 2 | 30.0 分 | **46.6 分** | **1.55** | **10,759** |
> | 実運用 3 | 30.0 分 | **39.5 分** | **1.32** | **10,839** |
>
> **RTF は音声の長さではなく文字数で決まる。**文字あたりの処理速度は
> **3.9〜4.6 字/秒で安定**しており、POC（3.26 字/秒）とほぼ同じである。
> **POC の RTF 0.131 は「30 分で 688 文字」というほぼ無音の録音で測ったもの**であり、
> 実音声を代表していなかった。
>
> **16 時間の密な発話なら 15 時間前後かかる。**この機械（コンテナに 7 CPU / ホストは
> 14）で `threads: 0`（= `min(nproc, 8)` → 7）を使った場合である。
>
> **判定条件をどうするかは #125 で決める。**速くする手立ては
> (1) Docker Desktop の CPU 割り当てを増やす（7 → 10〜12。POC の実測で 4→7 が
> 1.72 倍とほぼ線形）、(2) `-nf`（温度フォールバック無効）、(3) `-bs 1 -bo 1`
> （ビーム探索をやめる）、(4) ホスト側で Metal を使う、がある。**いずれも未着手。**

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
grep '^>' "$OBSIDIAN_VAULT/Daily/Voice/Wiki/<日付>/"*.md   # ★警告行の文言
```

#### 実測（2026-09-15。15.62 秒の無音を 1 本）

```text
14:36:09  normalize_completed recording_key=…MIC019… in_bytes=2282056 out_bytes=499976 elapsed_s=0.1
14:36:09  part_skipped recording_key=…MIC019… reason=no_speech
14:37:08  session_merged session_key=DJIMIC3:20260915 parts=4 excluded=2 chars=416
14:37:42  obsidian_saved session_key=DJIMIC3:20260915 bytes=5763
```

| 判定条件 | 結果 |
|---|---|
| `SKIPPED` になる | ✅ `NO_SPEECH_DETECTED` |
| セッション全体が停止しない | ✅ `parts=4 excluded=2` で統合が進んだ |
| Daily ノートが生成される | ✅ |

**ノートの報告（v5.36 の #140 の修正を実機で確認）**

```yaml
voicedock_failed_parts: []
voicedock_skipped_parts:
  - "DJIMIC3/…/TX00_MIC018_20260915_110509_orig.wav"   # SOURCE_MISSING
  - "DJIMIC3/…/TX00_MIC019_20260915_143115_orig.wav"   # NO_SPEECH_DETECTED
```

```text
> ⚠ この日の録音のうち 2 本を除外しました（元ファイルが見つかりません・無音）。自動では再試行されません。デバイスから採り直してください。
```

- **`voicedock_failed_parts` が空**である（`SKIPPED` を混ぜていない）
- 理由の並びが **§15.1 の順**（`SOURCE_MISSING` → `NO_SPEECH_DETECTED`）であり、Part の処理順に依存しない
- `SOURCE_MISSING` が混ざっているので **`⚠` が付き、次の操作が書かれている**

> **v5.35 まではこのノートが「2 本が処理できませんでした。次にデバイスを接続したときに
> 自動で再試行されます」と書いていた。**`SKIPPED` は終端であり、**起きないことを
> 約束していた**（#140）。**無音を失敗として報告する試験結果になるところだった。**

判定: ✅ **PASS**

### 3.8 E2E-08 — 1 本だけ Whisper を失敗させる

他の Part で Daily が生成され、警告行と `voicedock_failed_parts` に記録される。
**失敗 Part の音声は削除されない。**

```bash
docker compose logs voicedock | grep -E 'transcription_failed|session_merged'
grep -A3 voicedock_failed_parts "$OBSIDIAN_VAULT/Daily/Voice/Wiki/<日付>/"*.md
ls -la "/Volumes/<VOL>/TX_.../"                       # ★失敗 Part の音声が残っていること
```

#### 実測（2026-09-15）

**失敗のさせ方**: `transcription.model` を **VAD モデルのパス**へ向ける。
**whisper のモデルファイルを消したり改名したりしてはならない** —— V-23（起動時のファイル
実在検査）が起動を中止し、`restart: unless-stopped` と合わさって**再起動ループ**になる。
実在する別のファイルを指せば V-23 は通り、**whisper だけが失敗する。**

```text
12:14:06  session_merged session_key=DJIMIC3:20260915 parts=3 excluded=1 chars=300
12:14:06  obsidian_saved bytes=2320
```

ノートの frontmatter:

```yaml
voicedock_failed_parts:
  - "DJIMIC3/TX_MIC001_20260912_163444/TX00_MIC018_20260915_110509_orig.wav"
parts: 3
```

§13.4 の警告行も本文に出た。

```text
> ⚠ この日の録音のうち 1 本が処理できませんでした。次にデバイスを接続したときに自動で再試行されます。
```

| 判定条件 | 結果 |
|---|---|
| 他の Part で Daily が生成される | ✅ `parts=3 excluded=1` |
| `voicedock_failed_parts` に載る | ✅ |
| §13.4 の警告行が出る | ✅ |
| **失敗 Part の音声が残る** | ✅ デバイス側 5022376 bytes（取り込み時と同一） |

> **この試験の途中で #131 / #133 / #135 の 3 件が見つかった。**
> 完成済みセッションに入った失敗 Part がノートに現れない（#131）、
> 失敗 Part の 16 kHz 音声まで消していた（#133）、
> `WHISPER_FAILED` の `error_message` が whisper のヘルプ全文だった（#135）。
> **1 本失敗させるだけの試験で 3 件出たことが、この試験の価値である。**

判定: ✅ **PASS**

### 3.9 E2E-09 — 保存後に同じ日の Part を追加投入

**セッションが再オープンされ、同じファイルが更新される（増殖しない）。**

```bash
ls -1 "$OBSIDIAN_VAULT/Daily/Voice/Wiki/<日付>/"      # 追加前のファイル数
# 同じ日の録音をもう 1 本入れて接続する
docker compose logs voicedock | grep -E 'session_reopened|obsidian_saved'
ls -1 "$OBSIDIAN_VAULT/Daily/Voice/Wiki/<日付>/"      # ★ (2) が増えていないこと
```

#### 実測（2026-09-14。**4 回再オープンした**）

`sessions` の行:

```text
('DJIMIC3:20260914', 'COMPLETED', part_count=10, regenerated_count=4, failed_part_count=0,
 'Daily/Voice/Wiki/20260914/2026-09-14 Voice.md')
```

Wiki フォルダ:

```text
$ ls -1 "$OBSIDIAN_VAULT/Daily/Voice/Wiki/20260914/"
2026-09-14 Voice.md
```

**4 回作り直して 1 ファイルのまま。**` (2)` は生まれていない（§13.5 の同名衝突規則が
`voicedock_session_key` の一致を見て上書きする）。

> **この 4 回のうち 1 回で #108 が見つかった。**再オープンしても LLM 解析が
> やり直されず、**`parts: 8` と書かれたノートの本文が 2 Part 分のまま**だった。
> 修正後の再オープン（#112 の修正と合わせて）で、**9 Part 全部が載ったノートに直った。**

判定: ✅ **PASS**

### 3.10 E2E-10 — 削除を ON（**Phase 7**）

**保存確認後にのみ DJI 側 WAV が削除される。**

> **判定条件の「`_orig` と denoised の両方が消える」は v3.x の名残である。**
> §5.3 は v5.0 で **`_orig` 固定**にし、**denoised は登録も削除もしない**と定めた
> （§2 の用語集にも「1 パート = 1 ファイル」と書いてある）。取り込まないものを
> 消すことはない。E2E-03 の `SOURCE_DELETE_PENDING` と同じ種類の古さである。

```bash
make enable-deletion                                  # 三重ロックを 3 つとも外す
# DJI を接続する
grep -E 'source_deleted|source_delete_rejected' "$VOICEDOCK_HOME/log/ingest.log"
docker compose exec voicedock voicedock status        # ★COMPLETED になること
ls -la "/Volumes/<VOL>/TX_.../"                       # ★元音声が消えていること
```

#### 実測（2026-09-16。3.3 時間ぶん 7 本）

```text
01:13:14  source_deleted  …TX00_MIC004_20260915_182731_orig.wav
01:13:15  source_deleted  …TX00_MIC007_20260915_195731_orig.wav
01:13:15  source_deleted  …TX00_MIC002_20260915_172730_orig.wav
01:13:15  source_deleted  …TX00_MIC005_20260915_185731_orig.wav
01:13:16  source_deleted  …TX00_MIC003_20260915_175730_orig.wav
01:13:16  source_deleted  …TX00_MIC001_20260915_165730_orig.wav
01:13:16  reaper_completed requests=6

01:18:16  scanned name=DJIMIC3 files=1      ← 7 → 1
```

| 判定条件 | 結果 |
|---|---|
| 保存確認後にのみ削除される | ✅ Raw ノートの検証を通った 6 本だけ |
| **無音（`SKIPPED`）は消えない** | ✅ 残る 1 本は MIC006（`NO_SPEECH_DETECTED`）。**本文が保存されていないので §14.1 が拒否した** |
| テキストが残る | ✅ Raw ノート 133,746 バイト / transcript 26 件 |
| 容量が解放される | ✅ 26.4 GiB → 27.7 GiB（1.3 GiB） |

**TCC を越えた。**手で叩いた reaper は `Operation not permitted` で `unlink` に失敗する。
**LaunchAgent の子プロセスとして走ると通る**（§3.4(7) のラッパ経由。#152 の設計判断）。

> **この試験に到達するまでに削除の鎖が 4 箇所で切れていた**（#151 / #152 / #154）。
> **どれも単体テストは全部緑のままだった。**さらに成功の記録が「保留」になる
> 不具合（#156）もここで見つかった。**三重ロックを全部外して実機で 1 本流す以外に
> 見つける方法が無かった。**

判定: ✅ **PASS**

### 3.11 E2E-11 — 後追いの一括削除（**Phase 7**）

**既に `COMPLETED` の Part も §14.1 が真なら削除される。**

```bash
docker compose exec voicedock voicedock cleanup --backlog --dry-run   # ★何も書かない
docker compose exec voicedock voicedock cleanup --backlog
docker compose exec voicedock voicedock cleanup --resolve-absent --dry-run
docker compose exec voicedock voicedock cleanup --resolve-absent
```

#### 実測（2026-09-16）

**`--resolve-absent`** —— #151 / #156 を直す前に「実体は消えたのに保留」になっていた
6 件を解消した。

```text
10:45:41  source_delete_skipped recording_key=…TX00_MIC007_20260915_195731_orig.wav reason=already_absent
処理しました: 6 件

SOURCE_DELETE_PENDING: 0     ← 6 → 0
COMPLETED            : 23    ← 17 → 23
```

| 判定条件 | 結果 |
|---|---|
| 保留が解消される | ✅ 6 件すべて `COMPLETED` |
| **`source_deleted_at` を入れない** | ✅ `(なし)` のまま。**VoiceDock が消したのではない** |
| 削除要求を書かない | ✅ `Delete queue: 0 requested` |

**`--backlog`** —— 対象 **0 件**、対象外 13 件（すべて `not_deletable`）。

```text
対象（--dry-run。何も書きません）: 0 件
対象外: 13 件
  DJIMIC3/TX_MIC001_20260912_163444/TX00_MIC001_20260912_163444_orig.wav  (not_deletable)
```

**0 件が正しい。**削除 OFF の期間に処理した古い Part は Raw ノートの保存検証を
経ておらず、**§14.1 が偽**である。**「`COMPLETED` だから消してよい」ではない**ことが
実機で確認できた。

> **対象外の理由を出すことに意味がある。**出さないと「なぜ消えないのか」が分からず、
> 利用者は判断できない。

判定: ✅ **PASS**

### 3.12 E2E-12 — Docker Desktop を再起動

**途中状態から再開する。二重処理しない。**

```bash
docker compose exec voicedock voicedock status        # 再起動前の件数を控える
osascript -e 'quit app "Docker"'                      # 再起動する
docker compose logs voicedock | grep -E 'recovery_completed|service_started'
docker compose exec voicedock voicedock status        # ★二重処理されていないこと
```

#### 実測（2026-09-14）

**途中状態からの再開**（22:21。`FAILED` のセッションが残っていた状態で再起動）:

```text
22:21:04  INFO  service_started version=0.1.0 schema_version=1
22:21:04  INFO  recovery_completed requeued=1          ← FAILED を再投入した
22:23:25  INFO  llm_completed session_key=DJIMIC3:20260914 chunks=5 elapsed_s=141.0
22:23:25  INFO  obsidian_saved ... bytes=13402         ← 完走した
```

**二重処理しないこと**（23:48。すべて `COMPLETED` の状態で再起動）:

```text
23:48:30  INFO  service_stopping version=0.1.0
23:48:30  INFO  service_started version=0.1.0 schema_version=1
```

| | 再起動前 | 再起動後 |
|---|---|---|
| Part `COMPLETED` | 12 | **12** |
| Part `FAILED` | 0 | **0** |

> **実施したのは `docker compose restart` である。**Docker Desktop 自体の終了・再起動は
> **他のコンテナも巻き込む**ため行っていない。**コンテナから見れば同じ**
> （プロセスが落ちて上がり、`/data` の SQLite から状態を読み直す）が、
> **Docker Desktop の再起動そのものは E2E-06 の当日に併せて行うこと。**

判定: ✅ **PASS**（上記の留保つき）

## 4. §21.3 MVP 完成条件

⬜ 未実施。削除に関わらない項目を表で確認する（#35）。
