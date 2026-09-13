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

さらに、**削除の判断（コンテナ）と実行（Helper）を分け、実行側が判断を信用せず 11 項目を独立に再検証する。**

詳細は [docs/SPEC.md §14](docs/SPEC.md)。

## 状態

開発中。**実装はまだ存在しない**（仕様とホスト検証のみ）。

Phase 0 の実機検証は一部完了している（[docs/POC.md](docs/POC.md)）。
その結果を受けて v4.0 で**アーキテクチャを変更した**（コンテナが直接デバイスを読む構成から、
ホスト側 Helper 経由へ）。
