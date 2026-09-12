# VoiceDock

DJI Mic 3 で録音した音声を Mac へ接続するだけで、**ローカル完結**で文字起こし・要約し、Obsidian へ保存する。

```text
DJI Mic 3 で録音 → 帰宅 → Mac へ USB 接続 → （以降すべて自動）
```

- 文字起こし: whisper.cpp（Docker コンテナ内 / CPU）
- 要約・タスク抽出: Docker Model Runner 上のローカル LLM（Apple Silicon Metal）
- 出力: Obsidian Vault への Markdown（1 日 1 枚の生データ + 1 日 1 枚の整理済みノート）
- **クラウド AI API は使用しない。音声は外部へ送信しない。**
- Mac へインストールするのは Docker Desktop と Obsidian だけ

## ドキュメント

実装時の規範は **[docs/SPEC.md](docs/SPEC.md)（詳細仕様書 v3.3）** に集約されている。

前身の方針書は [docs/archive/](docs/archive/) に保存してある。両者が矛盾する場合は `docs/SPEC.md` を優先する。

## セットアップ

```bash
cp .env.example .env
cp config/config.example.yaml config/config.yaml
# .env の OBSIDIAN_VAULT を自分の環境に合わせる

./scripts/doctor.sh          # ホスト設定の検査（§19.2 DH-1〜DH-11）
./scripts/fetch-models.sh
make up                      # = docker compose up -d --build
```

日常操作は `make` にまとめてある（`up` / `down` / `logs` / `status` / `test` / `doctor` / `models`）。
`make test` はコンテナ内で pytest を実行するため、**ホストに Python を入れる必要はない**。
依存 lock の再生成は `make lock`（これも使い捨てコンテナで動く）。
シェルスクリプトは `doctor.sh`（セットアップ検査と環境診断を兼ねる）と `fetch-models.sh` の 2 本だけ。

前提条件とホスト設定の詳細は [docs/SPEC.md §3](docs/SPEC.md) を参照。

> **重要**: Docker Desktop の仮想化バックエンドは **Apple Virtualization Framework** でなければならない。
> Docker VMM では `/Volumes` 配下の外付けボリュームを bind mount できない（[docker/for-mac#7480](https://github.com/docker/for-mac/issues/7480)）。

## 安全設計

元の録音は、**文字起こし本文と整理済みノートの双方が Obsidian に保存され、実ファイルを読み直した検証に成功した場合にのみ**削除される。
削除は「設定」と「マウント権限」の二重ロックで守られており、既定ではどちらも無効になっている。

詳細は [docs/SPEC.md §14](docs/SPEC.md)。

## 状態

開発中（Phase 0: DJI Mic 3 PoC 未実施）。実装はまだ存在しない。
