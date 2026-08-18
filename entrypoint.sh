
#
# Container entrypoint.
#
# Runs on every `docker compose up`, in this order:
#   1. sanity-check required configuration (fail fast, readable message)
#   2. apply database migrations
#   3. seed the database from Britam_Role_Library.html  <- the HTML -> SQLite step
#   4. create the bootstrap superuser (only if none exists)
#   5. collect static files
#   6. exec gunicorn (PID 1, so SIGTERM reaches it and shutdown is graceful)
#
# `set -e` means any failing step aborts the boot rather than serving a
# half-initialised site.

set -eu

log() {
    printf '%s entrypoint: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1"
}

fail() {
    printf '%s entrypoint: ERROR %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# 1. Configuration checks
# ---------------------------------------------------------------------------

: "${DJANGO_SETTINGS_MODULE:=config.settings}"
export DJANGO_SETTINGS_MODULE

: "${DJANGO_SQLITE_PATH:=/data/db.sqlite3}"
export DJANGO_SQLITE_PATH

: "${GUNICORN_WORKERS:=3}"
: "${GUNICORN_THREADS:=2}"
: "${GUNICORN_TIMEOUT:=60}"
: "${GUNICORN_BIND:=0.0.0.0:8000}"

if [ -z "${DJANGO_SECRET_KEY:-}" ]; then
    fail "DJANGO_SECRET_KEY is not set.
       Create a .env file next to docker-compose.yml containing:
         DJANGO_SECRET_KEY=<paste a long random string>
       Generate one with:
         python3 -c 'import secrets; print(secrets.token_urlsafe(64))'
       See .env.example."
fi

DATA_DIR="$(dirname "$DJANGO_SQLITE_PATH")"
if [ ! -d "$DATA_DIR" ]; then
    log "creating data directory $DATA_DIR"
    mkdir -p "$DATA_DIR"
fi
if [ ! -w "$DATA_DIR" ]; then
    fail "data directory $DATA_DIR is not writable by uid $(id -u).
       The named volume is probably owned by root. Fix with:
         docker compose down && docker volume rm britam_role_data && docker compose up -d"
fi

log "starting (version=${APP_VERSION:-dev}, db=$DJANGO_SQLITE_PATH, workers=$GUNICORN_WORKERS)"

# ---------------------------------------------------------------------------
# 2. Migrations
# ---------------------------------------------------------------------------

log "applying migrations"
python manage.py migrate --noinput

# ---------------------------------------------------------------------------
# 3. Seed from the legacy HTML
#
# SEED_MODE controls this step:
#   once  (default) load only when the database has no roles yet. Later edits
#         made by HR in the UI are never overwritten by a restart.
#   sync  re-read the HTML every boot and update changed rows. Use when the
#         HTML file remains the source of truth.
#   off   never seed.
# ---------------------------------------------------------------------------

: "${SEED_MODE:=once}"

case "$SEED_MODE" in
    once)
        log "seeding from HTML (mode=once)"
        python manage.py seed_roles --quiet-if-seeded
        ;;
    sync)
        log "seeding from HTML (mode=sync)"
        python manage.py seed_roles
        ;;
    off)
        log "seeding disabled (SEED_MODE=off)"
        ;;
    *)
        fail "SEED_MODE='$SEED_MODE' is not valid. Use one of: once, sync, off."
        ;;
esac

# ---------------------------------------------------------------------------
# 4. Bootstrap superuser (no-op unless DJANGO_SUPERUSER_* are set)
# ---------------------------------------------------------------------------

log "checking for an editor account"
python manage.py bootstrap_admin --skip-if-any-exists

# ---------------------------------------------------------------------------
# 5. Static files
# ---------------------------------------------------------------------------

log "collecting static files"
python manage.py collectstatic --noinput --clear >/dev/null

# ---------------------------------------------------------------------------
# 6. Serve
#
# ADR-011: sync workers with a small thread pool. The workload is short
# database reads; gevent/eventlet would add a monkey-patching failure mode for
# no benefit, and SQLite writes are serialised anyway.
# Rule of thumb used: workers = (2 x cores) + 1, capped by the memory limit in
# docker-compose.yml.
# ---------------------------------------------------------------------------

log "starting gunicorn on $GUNICORN_BIND"
exec gunicorn config.wsgi:application \
    --bind "$GUNICORN_BIND" \
    --workers "$GUNICORN_WORKERS" \
    --threads "$GUNICORN_THREADS" \
    --timeout "$GUNICORN_TIMEOUT" \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --access-logfile - \
    --error-logfile - \
    --log-level "${GUNICORN_LOG_LEVEL:-info}" \
    --capture-output
