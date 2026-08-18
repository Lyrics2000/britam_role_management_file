############################################################################
# ADR-012: multi-stage build on python:3.12-slim.
#
# The previous image was nginx:alpine serving a static file — perfect for what
# it did, but the app now needs a Python runtime. Choices made here:
#   * slim, not alpine: alpine's musl libc has no manylinux wheels, so pip
#     compiles from source and the build goes from ~40s to several minutes.
#   * two stages: build tooling (gcc, headers) stays out of the runtime image.
#   * non-root user: the process cannot write anywhere except /data.
# Final image is ~180 MB.
############################################################################

# ---------------------------------------------------------------------------
# Stage 1 — build the virtualenv
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip==24.2 setuptools==75.1.0 wheel==0.44.0 \
    && pip install -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# wget is used by the container HEALTHCHECK; sqlite3 makes on-box debugging
# ("why is this row missing?") possible without a second container.
RUN apt-get update \
    && apt-get install --no-install-recommends -y wget sqlite3 tini \
    && rm -rf /var/lib/apt/lists/*

# Baked in so /healthz reports which build is running even if the compose
# environment is missing it.
ARG APP_VERSION=dev

ENV APP_VERSION=${APP_VERSION} \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=random \
    DJANGO_SETTINGS_MODULE=config.settings \
    DJANGO_SQLITE_PATH=/data/db.sqlite3 \
    DJANGO_STATIC_ROOT=/app/staticfiles

COPY --from=builder /opt/venv /opt/venv

# uid/gid 10001: high enough not to collide with a host user, fixed so the
# named volume's ownership stays correct across image rebuilds.
RUN groupadd --gid 10001 britam \
    && useradd --uid 10001 --gid britam --shell /bin/sh --create-home britam \
    && mkdir -p /app /data /app/staticfiles \
    && chown -R britam:britam /app /data

WORKDIR /app

COPY --chown=britam:britam . /app

RUN chmod +x /app/entrypoint.sh \
    && python -c "import compileall, sys; sys.exit(0 if compileall.compile_dir('/app', quiet=1, force=True) else 1)"

USER britam

VOLUME ["/data"]
EXPOSE 8000

# tini reaps zombies and forwards signals, so `docker compose stop` shuts
# gunicorn down gracefully instead of hitting the 10s kill timeout.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD wget --quiet --spider http://localhost:8000/healthz || exit 1
