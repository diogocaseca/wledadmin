FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependências primeiro (camada cacheada enquanto o uv.lock não mudar)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 1000 --home /app wledadmin \
    && mkdir -p /data && chown wledadmin /data
USER wledadmin

ENV PATH=/app/.venv/bin:$PATH \
    WLEDADMIN_DATA_DIR=/data \
    WLEDADMIN_HOST=0.0.0.0 \
    WLEDADMIN_PORT=8000

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"WLEDADMIN_PORT\",\"8000\")}/healthz', timeout=4)"

CMD ["wledadmin", "serve"]
