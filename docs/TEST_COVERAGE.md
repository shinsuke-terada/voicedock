# テストカバレッジ（SPEC §20.1 / §20.2）

**この表は `tests/unit/test_coverage_doc.py` が SPEC と突き合わせている。**
§20.1 に 1 行足して実装を忘れると落ちる — **件数のずれが最も危ない**（#55 の振り返り）。

**未カバーの項目は空欄にせず issue 番号を書く。**空欄を許すと「まだ書いていない」と
「書いたつもり」が区別できない。

## §20.1 Unit Test

| §20.1 の対象 | テスト |
|---|---|
| ファイル名パーサ | `tests/unit/test_device_parse.py` |
| フォルダ名パーサ | `tests/unit/test_device_parse.py` |
| Variant の選別（§5.3） | `tests/unit/test_device_inbox.py` `tests/unit/test_discover.py` |
| デバイス判定 | `tests/unit/test_helper_ingest.py` |
| 安定性判定 | `tests/unit/test_helper_ingest.py` |
| セッション分組 | `tests/unit/test_session_group.py` |
| Block 算出 | `tests/unit/test_blocks.py` |
| SHA-256 ストリーム | `tests/unit/test_normalize.py` |
| 空き容量計算 | `tests/unit/test_normalize.py` |
| **恒久識別子（§8.1）** | `tests/unit/test_paths.py` |
| **スキーマ（§8.2〜§8.5）** | `tests/unit/test_db.py` `tests/unit/test_migration_rules.py` |
| 状態遷移 | `tests/unit/test_states.py` `tests/unit/test_db.py` |
| クラッシュリカバリ | `tests/unit/test_pipeline_contract.py` |
| リトライ | `tests/unit/test_retry.py` `tests/unit/test_db.py` `tests/unit/test_worker_loop.py` |
| JSON 抽出 | `tests/unit/test_json_extract.py` |
| スキーマ生成 | `tests/unit/test_llm_schema.py` |
| Pydantic 検証 | `tests/unit/test_llm_schema.py` |
| Map-Reduce 分割 | `tests/unit/test_chunking.py` `tests/unit/test_reduce.py` |
| Raw レンダリング | `tests/unit/test_raw_render.py` |
| Daily レンダリング | `tests/unit/test_daily_render.py` |
| Timeline 生成 | `tests/unit/test_daily_render.py` |
| WikiLink | `tests/unit/test_wikilink.py` |
| ファイル名 sanitize | `tests/unit/test_sanitize.py` |
| 保存検証 | `tests/unit/test_verify.py` |
| 削除条件 | `tests/unit/test_no_delete.py` |
| 削除対象の同定 | `tests/unit/test_no_delete.py` |
| パス型 | `tests/unit/test_paths.py` `tests/unit/test_no_device_delete.py` |
| **Part 登録（§10.2）** | `tests/unit/test_discover.py` |
| **ffprobe（§10.5(1)）** | `tests/unit/test_probe.py` |
| **ログ規約（§16.4）** | `tests/unit/test_log.py` `tests/unit/test_spec_docs.py` |
| **`status`（§17.2）** | `tests/unit/test_status.py` |
| **Vault インデックス（§13.8）** | `tests/unit/test_wikilink.py` |
| **モデル取得（§18.6）** | `tests/unit/test_fetch_models.py` |
| **文書の整合（§6）** | `tests/unit/test_spec_docs.py` |

## §20.2 Integration Test

| 対象 | テスト |
|---|---|
| 通しフロー（inbox → 変換 → Whisper → Raw → merge → LLM → Daily → 保存検証） | `tests/integration/test_pipeline_flow.py` |
| ネットワークを使わないこと（§14.5 / §14.4 N-7） | `tests/unit/test_no_network.py`（遮断は `tests/conftest.py` の `no_network` が autouse で掛ける） |
| 偽 inbox / 偽ボリュームの組み立て | `tests/unit/test_fake_tree.py` `tests/unit/test_make_wav.py` |
| ホスト側 doctor（§19.2 の DH 表 6 件） | `tests/unit/test_doctor_host.py`（`docker` / `launchctl` は `PATH` の偽物に差し替える） |
| Helper の配備（ラッパのビルドと署名、plist の中身。§3.4(7)） | `tests/unit/test_helper_install.py`（`cc` / `codesign` / `launchctl` は `PATH` の偽物に差し替える） |
| `models` 長構文（注入する変数名と §18.2 の一致） | `tests/unit/test_compose_models.py` |
| 削除禁止 ND-01〜17 / 21〜23 / 30 / 31（コンテナ層。§20.4） | `tests/unit/test_no_delete.py`（CI の `no-delete` job で必須） |
| 削除禁止 ND-18〜20 / 24〜29（reaper 層。§20.4） | `tests/unit/test_reaper.py`（CI の `no-delete` job で必須） |
| reaper 本体（§14.1.1 の 12 項目） | `tests/unit/test_reaper.py` `tests/unit/test_helper_portability.py` |
| 実機採取（P0-8 の `mount` と `heartbeat.json` の一致） | `tests/unit/test_probe.py`（`mount` は `VOICEDOCK_MOUNT_CMD` の偽物に差し替える） |
| 性能判定（§21.2 Phase 2 の 8 時間 / Phase 3 の 30 分） | `tests/unit/test_perf_report.py` |
| 実機手順書（§20.3 との 1 対 1、`voicedock <sub>` の実在） | `tests/unit/test_runbook.py` |

> **統合テストは `needs_ffmpeg` が付いている。**§10.5 の変換は本物の ffmpeg が要るので、
> CI では自動 skip され `make test`（コンテナ内）で走る。**ffmpeg を偽物にしてはならない** —
> 48 kHz → 16 kHz の変換こそがこのフローの要である。

