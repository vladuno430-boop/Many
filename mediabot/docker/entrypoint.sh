#!/usr/bin/env sh
# =============================================================================
# Container entrypoint. One image, several roles, selected by the first
# argument. Every role waits for its dependencies and applies migrations from a
# single place (the "migrate" role, run once by docker-compose) so concurrent
# replicas never race on Alembic.
# =============================================================================
set -eu

ROLE="${1:-bot}"

log() { printf '[entrypoint] %s\n' "$1" >&2; }

wait_for_postgres() {
    log "waiting for PostgreSQL at ${DB__HOST:-postgres}:${DB__PORT:-5432}"
    python - <<'PY'
import os, socket, sys, time

host = os.getenv("DB__HOST", "postgres")
port = int(os.getenv("DB__PORT", "5432"))
deadline = time.time() + 90
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=3):
            sys.exit(0)
    except OSError:
        time.sleep(2)
print(f"PostgreSQL at {host}:{port} is unreachable", file=sys.stderr)
sys.exit(1)
PY
}

wait_for_redis() {
    log "waiting for Redis at ${REDIS__HOST:-redis}:${REDIS__PORT:-6379}"
    python - <<'PY'
import os, socket, sys, time

host = os.getenv("REDIS__HOST", "redis")
port = int(os.getenv("REDIS__PORT", "6379"))
deadline = time.time() + 60
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=3):
            sys.exit(0)
    except OSError:
        time.sleep(2)
print(f"Redis at {host}:{port} is unreachable", file=sys.stderr)
sys.exit(1)
PY
}

case "$ROLE" in
    migrate)
        wait_for_postgres
        log "applying database migrations"
        exec alembic upgrade head
        ;;
    bot)
        wait_for_postgres
        wait_for_redis
        log "starting the Telegram bot"
        exec python -m mediabot bot
        ;;
    api)
        wait_for_postgres
        wait_for_redis
        log "starting the REST API and admin panel"
        exec python -m mediabot api
        ;;
    scheduler)
        wait_for_postgres
        wait_for_redis
        log "starting the scheduler"
        exec python -m mediabot scheduler
        ;;
    worker)
        wait_for_postgres
        wait_for_redis
        log "starting a Celery worker"
        exec celery -A mediabot.infrastructure.queue worker \
            --loglevel="${CELERY_LOGLEVEL:-info}" \
            --concurrency="${CELERY_CONCURRENCY:-4}" \
            --queues="${CELERY_QUEUES:-downloads,notifications,maintenance}" \
            --max-tasks-per-child=50
        ;;
    beat)
        wait_for_redis
        log "starting Celery beat"
        exec celery -A mediabot.infrastructure.queue beat --loglevel=info
        ;;
    flower)
        wait_for_redis
        log "starting Flower"
        exec celery -A mediabot.infrastructure.queue flower --port=5555
        ;;
    shell)
        exec python
        ;;
    *)
        # Anything else is executed verbatim, which keeps `docker run … bash`
        # and one-off maintenance commands working.
        exec "$@"
        ;;
esac
