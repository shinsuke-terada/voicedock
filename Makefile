UV_IMAGE ?= ghcr.io/astral-sh/uv:0.12.13-python3.12-trixie-slim

# UV_PROJECT_ENVIRONMENT でリポジトリ内に .venv を作らせない
# （コンテナの root が作った .venv がホストへ残るのを避ける）
UV_RUN = docker run --rm -v "$(CURDIR)":/w -w /w \
           -e UV_PROJECT_ENVIRONMENT=/tmp/.venv $(UV_IMAGE) sh -c

.PHONY: test lint fmt lock

# テストはコンテナ内で実行する。ホストに Python / pytest を入れない（SPEC §3.3, §17.4）
test:
	docker build --target dev -t voicedock:dev .
	docker run --rm -v "$(CURDIR)/tests:/app/tests:ro" voicedock:dev pytest -q

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
