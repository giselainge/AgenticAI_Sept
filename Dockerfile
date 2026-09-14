# syntax=docker/dockerfile:1

FROM python:3.13-slim-bookworm AS python-dependencies

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install --requirement /tmp/requirements.txt


FROM python:3.13-slim-bookworm AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    INVOICE_DATA_ROOT=/app/data \
    RAG_KB_PATH=/app/runtime/knowledge_base.json \
    VECTOR_STORE_DIR=/app/runtime/vector_store \
    HF_HOME=/app/runtime/huggingface

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ghostscript \
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

EXPOSE 8000 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "llm.main:app", "--host", "0.0.0.0", "--port", "8000"]
