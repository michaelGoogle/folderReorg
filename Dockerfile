# syntax=docker/dockerfile:1.7
#
# folder-reorg — application image
#
# Used by these compose services:
#   · chat-personal / chat-360f  (Streamlit chat UI per variant)
#   · pipeline                   (run.py / kb.py / status.py — oneshot invocations)
#
# Ollama lives on the HOST and is NOT in the compose. The chat + pipeline
# services run with `network_mode: host` so they reach Ollama (and the
# Qdrant containers' published 127.0.0.1 ports) at localhost addresses.
#
# Build:
#   docker compose build
# or:
#   docker build -t folderreorg:latest .
#
FROM python:3.12-slim AS base

# Avoid Python writing .pyc files / stdout buffering for cleaner logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System packages needed by the extractors:
#   · tesseract-ocr + deu/eng — OCR for image-only PDFs and standalone images
#   · antiword                — legacy .doc text extraction
#   · libgomp1                — required by hdbscan / scipy
#   · build-essential, gcc    — only during pip install of native wheels;
#                                stripped after install in the next stage
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
        antiword \
        libgomp1 \
        ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# uv is faster than pip and matches the host's tooling
RUN pip install --no-cache-dir uv

WORKDIR /app

# Install Python dependencies first for layer caching: only re-runs when
# pyproject.toml changes, not on every source edit.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -e .

# Source code last so edits don't bust the dep layer
COPY src       ./src
COPY kb        ./kb
COPY chat_ui   ./chat_ui
COPY review_ui ./review_ui
COPY run.py kb.py status.py ./

# Where the indexer / chat read the NAS mount from (bind-mounted in
# from the host's SSHFS at runtime — see docker-compose.yml).
ENV KB_NAS_MOUNT=/app/nas

# Non-root user matching the host's typical UID so files written to
# bind-mounted dirs (data/, target_local/, logs/) are owned correctly.
# Override at run time with `--user $(id -u):$(id -g)` if your host
# UID differs from 1000.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} app \
 && useradd -m -u ${APP_UID} -g ${APP_GID} -s /bin/bash app \
 && chown -R app:app /app
USER app

# No ENTRYPOINT — services pass their own command via compose.
# Default to a friendly help message if someone runs the image bare.
CMD ["python", "-c", "print('folderreorg image. Use docker compose to invoke a specific service or `docker compose run pipeline <command>`.')"]
