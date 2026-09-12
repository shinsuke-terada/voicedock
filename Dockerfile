# ============================================================
# Stage: dev（テスト実行専用）
#
# #5 で whisper-builder / runtime ステージが追加され、下の FROM は
# `FROM runtime AS dev` へ差し替わる（SPEC §18.3）。
# そのとき ffmpeg と whisper-cli が使えるようになり、§20.2 の統合テストが動く。
# 本番イメージに pytest / ruff / mypy を含めないため、ステージを分けている（§18.1）。
# ============================================================
FROM python:3.12-slim-bookworm AS dev

WORKDIR /app

# 依存は lock から入れる。pyproject の範囲指定で解決させない（§18.5）
COPY requirements-dev.lock ./
RUN pip install --no-cache-dir -r requirements-dev.lock

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps -e .

CMD ["pytest", "-q"]
