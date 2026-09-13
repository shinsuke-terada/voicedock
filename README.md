# VoiceDock

DJI Mic 3 で録音した音声を Mac へ接続するだけで、**ローカル完結**で文字起こし・要約し、Obsidian へ保存する。

```text
DJI Mic 3 で録音 → 帰宅 → Mac へ USB 接続 → （以降すべて自動）
```

- 文字起こし: whisper.cpp（Docker コンテナ内 / CPU）
- 要約・タスク抽出: Docker Model Runner 上のローカル LLM（Apple Silicon Metal）
- 出力: Obsidian Vault への Markdown（1 日 1 枚の生データ + 1 日 1 枚の整理済みノート）
- **クラウド AI API は使用しない。音声は外部へ送信しない。**
- Mac へインストールするのは Docker Desktop と Obsidian、そして **VoiceDock Helper**（bash スクリプト 2 本 + LaunchAgent）だけ

## ドキュメント

実装時の規範は **[docs/SPEC.md](docs/SPEC.md)（詳細仕様書 v5.2）** に集約されている。

前身の方針書は [docs/archive/](docs/archive/) に保存してある。両者が矛盾する場合は `docs/SPEC.md` を優先する。

## セットアップ

```bash
cp .env.example .env
cp config/config.example.yaml config/config.yaml
# .env の OBSIDIAN_VAULT と VOICEDOCK_HOME を自分の環境に合わせる

./helper/install.sh          # ホスト側 Helper の導入（§3.4(6)）
./scripts/doctor.sh          # ホスト設定の検査（§19.2 DH-1〜DH-15）
./scripts/fetch-models.sh
make up                      # = docker compose up -d --build
```

**`helper/install.sh` を飛ばすと録音は 1 本も取り込まれない。**コンテナは正常に見え続けるため
気づきにくい。`doctor` の DH-12 と healthcheck の H-8 がこれを検出する。

`./scripts/doctor.sh`（= `make doctor`）は **初回セットアップ検査と環境診断を兼ねる。検査のみを
行い、ホスト設定は変更しない。**`make up` の前はホスト側の 5 件だけを実行し、起動後に実行すると
続けてコンテナ内の 12 件も走る（合計 17）。

日常操作は `make` にまとめてある（`up` / `down` / `logs` / `status` / `test` / `doctor` / `models`
/ `helper-install` / `helper-status`）。
`make test` はコンテナ内で pytest を実行するため、**ホストに Python を入れる必要はない**。
依存 lock の再生成は `make lock`（これも使い捨てコンテナで動く）。

前提条件とホスト設定の詳細は [docs/SPEC.md §3](docs/SPEC.md) を参照。

> **なぜ Helper がホスト側に要るのか**: Docker Desktop のファイル共有層（VirtioFS）は
> **物理 USB マスストレージを読めない**。読もうとすると Docker Desktop 全体が固まり、
> 再起動でしか回復しないことを実機で確認した。そのためデバイスの読み取りだけはホスト側で行う。
> 測定の全経過は [docs/POC.md](docs/POC.md) §2、設計判断は [docs/SPEC.md §5.6](docs/SPEC.md) にある。
>
> Helper は macOS の base system だけで動く。**Python も Homebrew も要求しない。**

## 安全設計

元の録音は、**文字起こし本文と整理済みノートの双方が Obsidian に保存され、実ファイルを読み直した検証に成功した場合にのみ**削除される。

削除は**三重ロック**で守られており、既定ではすべて無効になっている。

| ロック | 内容 |
|---|---|
| 1. 設定 | `config.yaml` と `helper.conf` の**両方**で有効化する必要がある |
| 2-A. コードの不在 | 削除を実行できるプログラム（`voicedock-reaper`）は**既定でインストールされない** |
| 2-B. OS レベル | デバイスは読み取り専用で再マウントされる（`mount(8)`: *even the super-user may not write it*） |

さらに、**削除の判断（コンテナ）と実行（Helper）を分け、実行側が判断を信用せず 12 項目を独立に再検証する。**

詳細は [docs/SPEC.md §14](docs/SPEC.md)。

## 運用

無人で動くものなので、**異常に気づける・復旧できる・更新できる**ことを前提に組んである。

### 日常

```bash
make up                      # 起動（= docker compose up -d --build）
make down                    # 停止
make logs                    # ログを追う
make status                  # 処理状況サマリ（失敗・滞留を見る唯一の窓口）
make doctor                  # ホスト 5 件 → コンテナ 12 件の診断（合計 17）
```

**異常は `docker ps` に出る。**healthcheck（§19.1）が落ちるとコンテナが `unhealthy` に
なるので、`docker ps` の `STATUS` 列で気づける。Helper が止まった場合もここに出る
（H-8。**これが最も気づきにくい故障**である —— コンテナは正常に見えたまま、録音だけが
1 本も取り込まれなくなる）。

**失敗した録音は自動で復帰する。**人が打つコマンドは無い（`retry` は v5.0 で削除した）。
`FAILED` はデバイスを繋ぎ直したときとサービス起動時に無条件で再評価される（§15.2）。

### DB のバックアップ

**単純なファイルコピーをしてはならない。**SQLite を WAL モードで開いているため
（§8.1）、`voicedock.db` だけをコピーすると **`-wal` にしか無い最新のトランザクションが
落ちる。**壊れたバックアップは、壊れていることが分かりにくい。

```bash
# オンラインのまま整合の取れた 1 ファイルを作る
docker compose exec voicedock python -c "
import sqlite3
with sqlite3.connect('/data/voicedock.db') as src, sqlite3.connect('/data/backup.db') as dst:
    src.backup(dst)
"
docker compose cp voicedock:/data/backup.db ./voicedock-$(date +%Y%m%d).db
```

`sqlite3` CLI が使えるなら `VACUUM INTO '/data/backup.db'` でもよい。どちらも
**読み取りロックを取って一貫したスナップショットを作る。**

復元は逆向きにコピーして `make doctor` を通す（D-2 がスキーマ版と件数を表示する）。

### 更新

**base image は digest で固定してある**（§18.5）。`latest` / `master` / `main` への
無条件依存は 1 つも無く、`tests/unit/test_version_pinning.py` がそれを見張っている。

更新は**手動の PR としてのみ**行う。

```bash
docker buildx imagetools inspect debian:bookworm-slim --format '{{.Manifest.Digest}}'
# Dockerfile の @sha256:... を書き換えて PR を出す
make down && make up && make doctor
```

Python 依存は `uv.lock` と `requirements*.lock` で固定してある（`make lock` で再生成）。

### 復旧

| 症状 | 見るところ |
|---|---|
| 録音が取り込まれない | `make doctor` の DH-12（LaunchAgent）と D-18（Helper の heartbeat） |
| コンテナが `unhealthy` | `make logs`。設定エラーなら終了コード 2 で落ちるので `docker compose run --rm voicedock voicedock doctor` |
| ノートが出ない | `make status` の Failed parts。LLM は D-12 が実際にリクエストを投げて確かめる |
| 途中で電源が落ちた | **起動時に自動で巻き戻す**（§9.4）。完了済みの工程は飛ばすので二重処理しない |

**Docker Desktop を再起動しても自動復帰する**（`restart: unless-stopped`。§18.4）。
ログは `10m × 3` でローテーションする。

## 状態

開発中。**取り込みから Daily ノートの保存までが動く。削除は既定で無効のままである。**

| Phase | 状態 |
|---|---|
| 0 PoC | 一部完了（[docs/POC.md](docs/POC.md)）。**残りは実機が要る** |
| 1〜6 取り込み・変換・文字起こし・ノート生成 | 実装済み |
| 7 削除 | **コードは揃っている**（`cleaner.py` と `voicedock-reaper`、削除禁止テスト ND-01〜31）が、**有効化していない。**§21.1 は E2E-01〜E2E-12 の全件 PASS を前提にしており、**実機での確認が残っている** |
| 8 運用 | 本章のとおり |

Phase 0 の実機検証の結果を受けて v4.0 で**アーキテクチャを変更した**（コンテナが直接
デバイスを読む構成から、ホスト側 Helper 経由へ）。テストの対応表は
[docs/TEST_COVERAGE.md](docs/TEST_COVERAGE.md) にある。
