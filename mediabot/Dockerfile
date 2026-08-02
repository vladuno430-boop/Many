# syntax=docker/dockerfile:1.7
# =============================================================================
# Multi-stage image shared by every service (bot, api, worker, scheduler).
# Stage 1 builds wheels with the toolchain; stage 2 keeps only the runtime,
# which halves the image size and removes compilers from the attack surface.
# =============================================================================

# --------------------------------------------------------------------------- #
# Stage 1 — builder
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libmagic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# Build a wheel of the application and pre-download every dependency wheel so
# the runtime stage installs offline and deterministically.
RUN python -m pip install --upgrade pip build wheel \
    && python -m build --wheel --outdir /wheels \
    && pip wheel --wheel-dir /wheels "$(ls /wheels/*.whl)[server]"

# --------------------------------------------------------------------------- #
# Stage 2 — runtime
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

# ffmpeg is required for merging/transcoding, libmagic for MIME validation,
# curl for the container health check.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libmagic1 \
        libpq5 \
        curl \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user: the downloader executes untrusted media through
# FFmpeg, so it must never have root on the host volume.
RUN groupadd --system --gid 1000 mediabot \
    && useradd --system --uid 1000 --gid mediabot --create-home --shell /usr/sbin/nologin mediabot

COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels "mediabot[server]" \
    && rm -rf /wheels

COPY --chown=mediabot:mediabot alembic.ini ./
COPY --chown=mediabot:mediabot migrations ./migrations
COPY --chown=mediabot:mediabot docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN mkdir -p /var/lib/mediabot/storage /var/lib/mediabot/tmp /var/log/mediabot \
    && chown -R mediabot:mediabot /var/lib/mediabot /var/log/mediabot

WORKDIR /app
USER mediabot

# tini reaps the zombie processes FFmpeg leaves behind.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["bot"]
