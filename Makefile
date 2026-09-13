UV_IMAGE ?= ghcr.io/astral-sh/uv:0.12.13-python3.12-trixie-slim

# UV_PROJECT_ENVIRONMENT でリポジトリ内に .venv を作らせない
# （コンテナの root が作った .venv がホストへ残るのを避ける）
UV_RUN = docker run --rm -v "$(CURDIR)":/w -w /w \
           -e UV_PROJECT_ENVIRONMENT=/tmp/.venv $(UV_IMAGE) sh -c

.PHONY: models up down logs status test lint fmt lock helper-install helper-status

# bind mount 先を用意する。本来は helper/install.sh の仕事だが、それまでは make up が面倒を見る
VOICEDOCK_HOME_DIRS = inbox queue state

up:
	@test -f .env || { echo "ERROR: .env がありません。cp .env.example .env してください"; exit 1; }
	@test -f config/config.yaml || { echo "ERROR: config/config.yaml がありません。cp config/config.example.yaml config/config.yaml してください"; exit 1; }
	@set -a; . ./.env; set +a; \
	  test -n "$$VOICEDOCK_HOME" || { echo "ERROR: .env の VOICEDOCK_HOME が空です"; exit 1; }; \
	  for d in $(VOICEDOCK_HOME_DIRS); do mkdir -p "$$VOICEDOCK_HOME/$$d"; done
	docker compose up -d --build

down:   ; docker compose down

# ホスト側 Helper（SPEC §3.4(6), §4.1, §17.4）。**Docker には依存しない**
# （Docker Desktop の起動前にデバイスが挿されうるため。§7.4）
helper-install: ; ./helper/install.sh
helper-status:  ; ./helper/install.sh --status
logs:   ; docker compose logs -f voicedock

# voicedock status は #33 まで未実装。終了コード 1 を返すが、ターゲットは隠さない
status: ; docker compose exec voicedock voicedock status

# テストはコンテナ内で実行する。ホストに Python / pytest を入れない（SPEC §3.3, §17.4）
# docs/ と config/ と compose.yaml も渡す。仕様・設定・マウント点と実装の整合を
# テストで固定するため（§17.4, tests/spec_sync.py, tests/unit/test_paths.py）
#
# helper/ も渡す。ホスト側 Helper（bash）の挙動をコンテナ内の pytest から
# subprocess で検証するため（§20.1 / §20.4 の Helper 層）。**ro で渡す**ので
# テストが helper/ を書き換えることはない
test:
	docker build --target dev -t voicedock:dev .
	docker run --rm \
	  -v "$(CURDIR)/tests:/app/tests:ro" \
	  -v "$(CURDIR)/docs:/app/docs:ro" \
	  -v "$(CURDIR)/config:/app/config:ro" \
	  -v "$(CURDIR)/helper:/app/helper:ro" \
	  -v "$(CURDIR)/scripts:/app/scripts:ro" \
	  -v "$(CURDIR)/compose.yaml:/app/compose.yaml:ro" \
	  -v "$(CURDIR)/Dockerfile:/app/Dockerfile:ro" \
	  -v "$(CURDIR)/Makefile:/app/Makefile:ro" \
	  voicedock:dev pytest -q

# モデルの取得（§18.6）。**image へ埋め込まない**（18.6 GB ある）。
# named volume は root 所有で作られるので、スクリプトが root で取得して uid 1000 へ移す
models:
	./scripts/fetch-models.sh

# Lint と型検査も使い捨てコンテナで行う。ホストに ruff / mypy を入れない（§3.3）
lint:
	$(UV_RUN) 'uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run mypy'

fmt:
	$(UV_RUN) 'uv sync --frozen && uv run ruff check --fix . && uv run ruff format .'

# lock の生成も使い捨てコンテナで行う。ホストに uv を入れない（§3.3, §18.5）
lock:
	$(UV_RUN) 'uv lock && \
	  uv export --frozen --no-dev --no-emit-project -o requirements.lock && \
	  uv export --frozen          --no-emit-project -o requirements-dev.lock'
