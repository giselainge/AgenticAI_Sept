FROM ghcr.io/astral-sh/uv:0.12.13 AS uv-bin

FROM python:3.13-slim-bookworm AS python-dependencies

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv-bin /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.13-slim-bookworm AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    INVOICE_DATA_ROOT=/app/data \
    INVOICE_RUNTIME_ROOT=/app/runtime \
    RAG_DB_PATH=/app/runtime/knowledge_base.sqlite3 \
    VECTOR_STORE_DIR=/app/runtime/vector_store \
    OCR_LANGUAGES=por+eng \
    OCR_DPI=300 \
    OCR_TIMEOUT_SECONDS=900 \
    OCR_IMAGE_MIN_DIMENSION=1800 \
    OCR_IMAGE_MAX_PIXELS=24000000 \
    OCR_IMAGE_MAX_SCALE=3.0 \
    OMP_THREAD_LIMIT=2 \
    HEALTHCHECK_PORT=8000 \
    HEALTHCHECK_PATH=/ready

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ghostscript \
        libgomp1 \
        libmagic1 \
        pngquant \
        qpdf \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-por \
        tini \
        unpaper \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-dependencies /opt/venv /opt/venv

WORKDIR /app
COPY --chown=${APP_UID}:${APP_GID} invoice_parser ./invoice_parser
COPY --chown=${APP_UID}:${APP_GID} llm ./llm
COPY --chown=${APP_UID}:${APP_GID} rag ./rag
COPY --chown=${APP_UID}:${APP_GID} scripts ./scripts
COPY --chown=${APP_UID}:${APP_GID} vector_store ./vector_store

RUN groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/runtime \
    && chown -R app:app /app/data /app/runtime

USER app

EXPOSE 8000 7860 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; port = os.environ.get('HEALTHCHECK_PORT', '8000'); path = os.environ.get('HEALTHCHECK_PATH', '/health'); urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=3).read()"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "llm.main:app", "--host", "0.0.0.0", "--port", "8000"]
