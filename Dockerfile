FROM python:3.12-slim-bookworm

# FFmpeg renders video, and these fonts stand in for the Windows fonts used on a PC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-liberation fonts-dejavu-core fontconfig libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.12.23

# The app doesn't run as root, which limits the damage if a malicious upload ever got through.
RUN useradd --create-home --uid 10001 vyral
WORKDIR /app
RUN chown vyral:vyral /app
USER vyral

ENV PYTHONPATH=/app UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never VYRAL_DATA=/app/data VYRAL_BIND_HOST=0.0.0.0 VYRAL_HOSTED=1 \
    HF_HUB_DISABLE_TELEMETRY=1

COPY --chown=vyral:vyral pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=vyral:vyral vyral ./vyral
COPY --chown=vyral:vyral migrations ./migrations
COPY --chown=vyral:vyral scripts ./scripts

# AI models are built into the image, so the server never downloads them while running.
RUN mkdir -p models data \
    && curl -fsSL -o models/kokoro-v1.0.fp16.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.fp16.onnx \
    && curl -fsSL -o models/voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin \
    && .venv/bin/python scripts/preload_models.py

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8765/api/auth/me > /dev/null || exit 1

CMD [".venv/bin/python", "-m", "vyral", "--no-browser"]
