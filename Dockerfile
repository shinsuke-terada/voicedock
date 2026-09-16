# ============================================================
# Stage 1: whisper.cpp のビルド（SPEC §18.3）
# ============================================================
# **base image は digest で固定する**（§18.5）。`latest` / `master` / `main` への
# 無条件依存を禁止し、更新は手動 PR としてのみ行う。tag を併記するのは人が読むためで、
# **解決に使われるのは digest だけ**である。
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 AS whisper-builder

ARG WHISPER_CPP_REF=v1.9.4

RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${WHISPER_CPP_REF}" \
        https://github.com/ggml-org/whisper.cpp.git /src/whisper.cpp

WORKDIR /src/whisper.cpp
RUN cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DWHISPER_BUILD_TESTS=OFF \
        -DWHISPER_BUILD_SERVER=OFF \
        -DGGML_NATIVE=OFF \
        -DGGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16 \
    && cmake --build build --config Release -j "$(nproc)"

# ============================================================
# Stage 2: runtime（本番イメージ）
# ============================================================
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=whisper-builder /src/whisper.cpp/build/bin/whisper-cli \
     /usr/local/bin/whisper-cli

# 静的リンクのため追加の共有ライブラリは不要。念のため検証する（§18.3）
RUN ldd /usr/local/bin/whisper-cli || true \
    && /usr/local/bin/whisper-cli --help > /dev/null

# /inbox /queue /state は Helper と共有する作業領域（v4.0 / §4.1）。
# v3.x の /host-volumes はもう作らない（コンテナはデバイスに到達しない。§5.6, N-3）
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin voicedock \
    && mkdir -p /data /models/whisper /obsidian /inbox /queue /state \
    && chown -R voicedock:voicedock /data /models

WORKDIR /app

# 依存は lock から入れる。pyproject の範囲指定で解決させない（§18.5）
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps . \
    && rm -rf /root/.cache

USER voicedock

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VOICEDOCK_CONFIG=/app/config/config.yaml

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "voicedock.main", "service"]

# ============================================================
# Stage 3: dev（テスト実行専用。本番イメージには含めない）
# ============================================================
FROM runtime AS dev

USER root
COPY requirements-dev.lock ./
RUN pip install --no-cache-dir -r requirements-dev.lock \
    && rm -rf /root/.cache \
    && chown -R voicedock:voicedock /app
USER voicedock

CMD ["pytest", "-q"]
