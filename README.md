# VoiceDock

**DJI Mic 3 で録音した音声を Mac へ挿すだけで、文字起こし・要約して Obsidian に残す。**

```text
DJI Mic 3 で録音 → 帰宅 → Mac へ USB 接続 → （以降すべて自動）
```

挿したあとに人がすることはありません。取り込み・変換・文字起こし・要約・ノート生成まで
自動で進み、**1 日ぶんが Obsidian の 2 枚のノート**（文字起こし全文と整理済みノート）に
まとまります。

## 何が動いているか

| 工程 | 使うもの | 場所 |
|---|---|---|
| デバイスの読み取り | VoiceDock Helper（bash + LaunchAgent） | **Mac 本体** |
| 16 kHz への変換 | ffmpeg | Docker コンテナ |
| 文字起こし | whisper.cpp（CPU） | Docker コンテナ |
| 要約・タスク抽出 | ローカル LLM（Docker Model Runner / Apple Silicon Metal） | Mac 本体 |
| ノート生成 | Python | Docker コンテナ |

**クラウド AI API は使いません。音声もテキストも外部へ出ません。**運用にかかる費用は
電気代だけです。外部送信をしないことは方針としてだけでなく、テストで強制しています
（全テストでネットワークを遮断し、`tests/unit/test_no_delete.py` が送信経路の不在を検査する）。

> **なぜ Helper が Mac 側に要るのか。**Docker Desktop のファイル共有層（VirtioFS）は
> **物理 USB マスストレージを読めません。**読もうとすると Docker Desktop 全体が固まり、
> 再起動でしか回復しないことを実機で確認しました。そのため**デバイスの読み取りだけは
> Mac 側で行い**、コンテナは共有フォルダ経由で受け取ります。
> 測定の全経過は [docs/POC.md](docs/POC.md) §2 にあります。
>
> Helper は macOS の標準機能だけで動きます。**Python も Homebrew も要りません。**

## 必要なもの

| | |
|---|---|
| Mac | Apple Silicon（macOS） |
| Docker Desktop | Model Runner を有効にしておく |
| Obsidian | Vault を 1 つ作り、**一度 Obsidian で開いておく**（`.obsidian/` が作られる） |
| ディスク | モデルに約 19 GB、作業領域に数 GB |

---

## インストール

### 1. 設定ファイルを用意する

```bash
cp .env.example .env
cp config/config.example.yaml config/config.yaml
```

`.env` の 2 行を自分の環境に合わせます。**引用符は付けません。**

```bash
OBSIDIAN_VAULT=/Users/USERNAME/Documents/Obsidian Vault
VOICEDOCK_HOME=/Users/USERNAME/VoiceDock
```

> `VOICEDOCK_HOME` は **`/Users` 配下**にしてください。VirtioFS が正常に扱えるのは
> ローカルディスク上のパスだけです（起動時検査 V-28 が確かめます）。

### 2. Mac 側の Helper を入れる

```bash
./helper/install.sh
```

`<VOICEDOCK_HOME>` に作業用のフォルダと `helper.conf` を作り、LaunchAgent を登録します。
以後、**5 分ごとにデバイスの有無を見に行きます。**

**これを飛ばすと録音は 1 本も取り込まれません。**コンテナは正常に見え続けるので
気づきにくい故障です。`doctor` の DH-12 と healthcheck の H-8 がこれを検出します。

### 3. 設定を検査する

```bash
./scripts/doctor.sh
```

**検査のみを行い、設定は変更しません。**この時点ではホスト側の 7 件だけが走ります。

### 4. モデルを取得する

```bash
./scripts/fetch-models.sh
```

whisper のモデル（約 19 GB）を Docker の named volume へ置きます。**イメージには
埋め込みません。**

### 5. 起動する

```bash
make up
```

---

## 使い方

**録って、挿す。以上です。**

```text
1. DJI Mic 3 で録音する
2. Mac へ USB で挿す
3. 待つ（Helper が 5 分以内に見つけて取り込む）
4. Obsidian にノートが増える
```

> **接続すると録音は自動的に停止します**（DJI Mic 3 の仕様）。録音中に挿すと
> その 1 本は途中で切れます。

### できあがるもの

| ノート | 場所 | 中身 |
|---|---|---|
| Raw ノート | `Daily/Voice/Raw/<日付>/` | **文字起こしの全文。**時刻の見出し付き |
| Daily ノート | `Daily/Voice/Wiki/<日付>/` | 要約・Timeline・タスク・決定事項・アイデア・タグ |

**1 日 1 枚ずつ**にまとまります。同じ日の録音を後から追加しても、同じファイルが
更新されるだけで増えません。

処理できなかった録音があれば、**ノート自身がそう言います。**

```text
> ⚠ この日の録音のうち 1 本が処理できませんでした。次にデバイスを接続したときに自動で再試行されます。
> この日の録音のうち 1 本を除外しました（無音）。自動では再試行されません。
```

### 状況を見る

```bash
make status
```

処理中・失敗・滞留を見る唯一の窓口です。**失敗した録音は自動で復帰する**ので、
人が打つ復旧コマンドはありません（デバイスを挿し直すか、サービスを起動し直せば
無条件に再評価されます）。

---

## 起動と終了

```bash
make up       # 起動（= docker compose up -d --build）
make down     # 終了
make logs     # ログを追う（Ctrl-C で抜ける）
```

**Mac を再起動しても自動で復帰します。**コンテナは `restart: unless-stopped`、
Helper は LaunchAgent なので、どちらも勝手に戻ります。

**Docker Desktop を止めているあいだもデバイスの取り込みは続きます。**Helper は
Docker に依存しません（共有フォルダに置くだけ）。コンテナが起きたときに続きから
処理されます。

### Helper だけを操作する

```bash
make helper-install   # 入れ直す
make helper-status    # 稼働確認
```

---

## 設定

**3 つのファイルがあります。役割が違います。**

| ファイル | 誰が読むか | 何を決めるか |
|---|---|---|
| `.env` | Docker Compose | Vault と作業領域の**場所** |
| `config/config.yaml` | コンテナ | **処理の中身**（文字起こし・要約・ノートの書式） |
| `<VOICEDOCK_HOME>/helper.conf` | Mac 側の Helper | **デバイスの扱い**（対象ボリューム・マウントモード） |

`config/config.yaml` と `helper.conf` は **Git 管理外**です。`*.example.*` からコピーして
使います。

### よく触るもの（`config/config.yaml`）

| キー | 既定 | 意味 |
|---|---|---|
| `session.idle_close_seconds` | `1800` | 最後の録音からこの秒数が経つと、その日のノートを作る |
| `transcription.model` | large-v3-turbo-q5_0 | whisper のモデル |
| `transcription.threads` | `0` | `0` で自動（CPU 数と 8 の小さい方） |
| `transcription.vad.enabled` | `true` | 無音区間を飛ばす。終日録音では実質必須 |
| `llm.analysis.sections` | 全部有効 | **抽出する項目。**切った項目はプロンプトにもスキーマにもノートにも出ない |
| `llm.analysis.custom_instructions` | 空 | 要約への追加指示（「この話題は要約しない」など） |
| `obsidian.raw` / `obsidian.wiki` | — | ノートの置き場所とファイル名 |
| `obsidian.vault_marker` | `.obsidian` | **Vault の目印。**これが無い場所へは書かない |
| `import.inbox_retain` | `normalized` | 取り込んだ原本をいつ消すか |

**書き換えたら `make up` で反映します。**

> 設定は起動時に全件検査されます。綴り間違いや値の矛盾があると**起動を中止**し、
> 何が悪いかを表示します。黙って既定値で動くことはありません。

### 対象ボリューム（`helper.conf`）

| キー | 既定 | 意味 |
|---|---|---|
| `INCLUDE_VOLUMES` | 空（すべて） | 取り込み対象のボリューム名 |
| `EXCLUDE_VOLUMES` | `Macintosh HD` など | 除外するボリューム名 |
| `MOUNT_MODE` | `ro` | **安全ロック 2-B。**読み取り専用で再マウントする |
| `DELETE_SOURCE_AUDIO` | `false` | **安全ロック 1。**下の「元音声の削除」を参照 |

---

## 元音声の削除

**既定では削除しません。**デバイス上の録音はそのまま残ります。

有効にすると、**文字起こしが Obsidian に保存・検証された録音から順に**デバイスから
削除されます。要約（Daily ノート）は待ちません —— 要約は元音声を使わないからです。

```bash
make enable-deletion    # 有効にする（確認入力を求める）
make disable-deletion   # 元に戻す
```

削除の根拠は**テキストが 2 か所に独立して存在すること**です。

| 場所 | 確かめ方 |
|---|---|
| Obsidian の Raw ノート | 実ファイルを読み直して SHA-256 を照合し、その録音の鍵が載っていることを確認 |
| `/data/transcripts/parts/` | 文字起こし JSON。**無期限に保持**され、Raw ノートの再生成元になる |

**どちらか一方でも欠けていれば削除しません。**

### 三重ロック

| ロック | 内容 |
|---|---|
| 1. 設定 | `config.yaml` と `helper.conf` の**両方**で有効化する必要がある |
| 2-A. コードの不在 | 削除を実行できるプログラム（`voicedock-reaper`）は**既定で配置されない** |
| 2-B. OS レベル | デバイスは読み取り専用で再マウントされる（`mount(8)`: *even the super-user may not write it*） |

さらに、**削除の判断（コンテナ）と実行（Helper）を分け、実行側が判断を信用せず
12 項目を独立に再検証します。**片方だけを解除した状態は起動時に検出して止めます。

---

## 困ったとき

**異常は `docker ps` に出ます。**healthcheck が落ちるとコンテナが `unhealthy` になります。

```bash
make doctor   # ホスト 7 件 → コンテナ 13 件の診断（合計 20）
make logs     # 何が起きているか
make status   # どこで止まっているか
```

| 症状 | 見るところ |
|---|---|
| 録音が取り込まれない | `make doctor` の DH-12（LaunchAgent）と D-18（Helper の heartbeat） |
| コンテナが `unhealthy` | `make logs`。設定エラーなら `docker compose run --rm voicedock voicedock doctor` |
| ノートが出ない | `make status` の Failed parts。LLM は D-12 が実際にリクエストを投げて確かめる |
| 途中で電源が落ちた | **起動時に自動で巻き戻します。**完了済みの工程は飛ばすので二重処理しません |

`./scripts/doctor.sh`（= `make doctor`）は**初回セットアップ検査と環境診断を兼ねます。
検査のみを行い、設定は変更しません。**`make up` の前はホスト側の 7 件だけを実行し、
起動後に実行すると続けてコンテナ内の 13 件も走ります（合計 20）。

---

## 保守

### DB のバックアップ

**単純なファイルコピーをしてはいけません。**SQLite を WAL モードで開いているため、
`voicedock.db` だけをコピーすると **`-wal` にしか無い最新のトランザクションが落ちます。**
壊れたバックアップは、壊れていることが分かりにくいものです。

```bash
docker compose exec voicedock python -c "
import sqlite3
with sqlite3.connect('/data/voicedock.db') as src, sqlite3.connect('/data/backup.db') as dst:
    src.backup(dst)
"
docker compose cp voicedock:/data/backup.db ./voicedock-$(date +%Y%m%d).db
```

復元は逆向きにコピーして `make doctor` を通します（D-2 がスキーマ版と件数を表示します）。

### 更新

**base image は digest で固定してあります。**`latest` への無条件依存は 1 つも無く、
テストがそれを見張っています。更新は**手動の PR としてのみ**行います。

```bash
docker buildx imagetools inspect debian:bookworm-slim --format '{{.Manifest.Digest}}'
# Dockerfile の @sha256:... を書き換えて PR を出す
make down && make up && make doctor
```

Python 依存は `uv.lock` と `requirements*.lock` で固定してあります（`make lock` で再生成）。

---

## 開発

```bash
make test    # コンテナ内で pytest。**ホストに Python を入れる必要はない**
make lint    # ruff + mypy（使い捨てコンテナ）
make fmt     # 自動整形
make lock    # 依存 lock の再生成
```

実装時の規範は **[docs/SPEC.md](docs/SPEC.md)（詳細仕様書）** に集約されています。
実機での検証手順と結果は [docs/E2E.md](docs/E2E.md)、Phase 0 の実測は
[docs/POC.md](docs/POC.md)、テストの対応表は [docs/TEST_COVERAGE.md](docs/TEST_COVERAGE.md)
にあります。前身の方針書は [docs/archive/](docs/archive/) に保存してあり、
矛盾する場合は `docs/SPEC.md` を優先します。

## 状態

開発中。**取り込みから Daily ノートの保存までが動きます。**

| Phase | 状態 |
|---|---|
| 0 PoC | **完了**（[docs/POC.md](docs/POC.md)）。送信機 2 台（機材なし）と電池切れ（運用の中で確認）を除く |
| 1〜6 取り込み・変換・文字起こし・ノート生成 | 実装済み。**E2E-01〜E2E-05 / E2E-07〜E2E-10 / E2E-12 を実機で PASS**（[docs/E2E.md](docs/E2E.md)） |
| 7 削除 | **実機で成立した**（2026-09-16。E2E-10 PASS）。削除禁止テスト ND-01〜32（**ND-10〜17 は廃止し欠番**）。残るのは E2E-06（運用の中で確認）と E2E-11（後追いの一括削除。#38） |
| 8 運用 | 本書のとおり |

Phase 0 の実機検証を受けて v4.0 で**アーキテクチャを変更しました**（コンテナが直接
デバイスを読む構成から、Mac 側 Helper 経由へ）。
