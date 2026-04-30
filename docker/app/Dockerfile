FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOME=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    tesseract-ocr \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    antiword \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY kb /app/kb
COPY chat_ui /app/chat_ui
COPY review_ui /app/review_ui
COPY dashboard /app/dashboard
COPY docker /app/docker
COPY run.py /app/run.py
COPY kb.py /app/kb.py
COPY status.py /app/status.py

RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install -e .

RUN useradd --create-home --shell /bin/bash appuser && \
    mkdir -p /app/data /app/logs /app/source_local /app/target_local /app/kb/data /nas && \
    chmod +x /app/docker/kb/run_daily_reindex.sh && \
    chown -R appuser:appuser /app /nas

USER appuser

CMD ["python", "--version"]
