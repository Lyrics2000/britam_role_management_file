"""
Django settings for the Britam Group Role Library.

Every environment-specific value is read from the environment. Nothing secret
is committed. See `.env.example` for the full list and `README.md` for how the
values are wired into docker compose.

ADR-002: single settings module driven by env vars, rather than the
settings/base.py + settings/prod.py split. The deployment target is one
docker-compose stack on one droplet; a second module would add an import graph
to reason about for zero operational benefit. DJANGO_DEBUG is the only switch
that meaningfully changes behaviour, and it is refused in production below.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Small env helpers. Deliberately strict: a typo in the compose file should
# crash the container at boot with a readable message, not silently disable
# security controls.
# ---------------------------------------------------------------------------

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def env_str(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(
            f"[CFG-001] Required environment variable {name} is missing or empty. "
            f"Set it in your .env file (see .env.example)."
        )
    return value or ""


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise ImproperlyConfigured(
        f"[CFG-002] Environment variable {name}={raw!r} is not a boolean. "
        f"Use one of {sorted(TRUE_VALUES | FALSE_VALUES)}."
    )


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"[CFG-003] Environment variable {name}={raw!r} is not an integer."
        ) from exc


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


RUNNING_TESTS = "pytest" in sys.modules or "test" in sys.argv

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

DEBUG = env_bool("DJANGO_DEBUG", default=False)

if RUNNING_TESTS:
    SECRET_KEY = env_str("DJANGO_SECRET_KEY", "insecure-key-used-only-by-the-test-suite")
else:
    SECRET_KEY = env_str("DJANGO_SECRET_KEY", required=True)
    if len(SECRET_KEY) < 32:
        raise ImproperlyConfigured(
            "[CFG-004] DJANGO_SECRET_KEY must be at least 32 characters. "
            "Generate one with: python -c \"import secrets;print(secrets.token_urlsafe(64))\""
        )

# ALLOWED_HOSTS must be explicit in production. "*" is refused unless DEBUG.
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
if not DEBUG and "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "[CFG-005] DJANGO_ALLOWED_HOSTS='*' is refused with DJANGO_DEBUG=0. "
        "List the real hostnames, e.g. 'roles.britam.com,164.90.x.x'."
    )

# Django >= 4 requires the scheme in CSRF_TRUSTED_ORIGINS.
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "roles.apps.RolesConfig",
]

MIDDLEWARE = [
    # RequestIDMiddleware first so every downstream log line carries the id.
    "roles.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "roles.middleware.AccessLogMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database
#
# ADR-003: SQLite, per the requirement. It is a correct choice here — the data
# set is ~300 rows, reads dominate by orders of magnitude, and writes come from
# a handful of HR editors. Two things make it safe under gunicorn's multiple
# worker processes, both applied in roles/apps.py on every new connection:
#   1. journal_mode=WAL   -> readers never block on the writer
#   2. busy_timeout       -> a concurrent writer waits instead of raising
#                            "database is locked"
# The file lives on a named docker volume so it survives `docker compose down`.
# Migration path if this ever outgrows SQLite: swap this block for Postgres and
# run `manage.py dumpdata roles | manage.py loaddata`. Nothing else changes.
# ---------------------------------------------------------------------------


SQLITE_PATH = Path(env_str("DJANGO_SQLITE_PATH", str(BASE_DIR / "data" / "db.sqlite3")))
SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(SQLITE_PATH),
        "OPTIONS": {
            # Seconds the driver waits for a write lock before erroring.
            # (The `transaction_mode` option is Django 5.1+; on 5.0 the same
            # protection comes from busy_timeout, set in roles/apps.py.)
            "timeout": env_int("DJANGO_SQLITE_TIMEOUT", 20),
        },
        "ATOMIC_REQUESTS": False,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/?page=manage"
LOGOUT_REDIRECT_URL = "/"

# ---------------------------------------------------------------------------
# i18n / tz
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-gb"
TIME_ZONE = env_str("DJANGO_TIME_ZONE", "Africa/Nairobi")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files
#
# ADR-004: WhiteNoise rather than a shared static volume between the gunicorn
# and nginx containers. A shared volume introduces a boot-order race (nginx can
# serve a 404 for a hashed asset while collectstatic is still running) for no
# measurable gain at this traffic level. nginx still proxies and caches.
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = Path(env_str("DJANGO_STATIC_ROOT", str(BASE_DIR / "staticfiles")))
STATICFILES_DIRS: list[Path] = []

# The manifest backend hashes filenames for far-future caching, but it needs
# `collectstatic` to have run — which is true in the container (entrypoint.sh)
# and false during tests and `runserver`. Falling back keeps both working.
STATICFILES_BACKEND = (
    "django.contrib.staticfiles.storage.StaticFilesStorage"
    if (DEBUG or RUNNING_TESTS)
    else "whitenoise.storage.CompressedManifestStaticFilesStorage"
)

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": STATICFILES_BACKEND},
}
WHITENOISE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days, matching the old nginx policy

# ---------------------------------------------------------------------------
# Security headers / cookies
#
# Behind nginx, Django only learns the original scheme from X-Forwarded-Proto.
# DJANGO_BEHIND_TLS_PROXY must be turned on once a certificate is in front,
# otherwise secure cookies would never be sent and login would silently fail.
# ---------------------------------------------------------------------------

BEHIND_TLS_PROXY = env_bool("DJANGO_BEHIND_TLS_PROXY", default=False)

if BEHIND_TLS_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 60 * 60 * 24 * 30)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = False

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = env_int("DJANGO_SESSION_AGE", 60 * 60 * 8)  # one working day
CSRF_COOKIE_HTTPONLY = False  # the Manage tab reads it to set X-CSRFToken
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "SAMEORIGIN"

DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024  # 5 MB; role payloads are ~4 KB
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "roles.permissions.ReadOnlyOrStaff",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "roles.pagination.RolePagination",
    "PAGE_SIZE": 100,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "roles_read": env_str("THROTTLE_ROLES_READ", "600/min"),
        "roles_write": env_str("THROTTLE_ROLES_WRITE", "60/min"),
        "ai": env_str("THROTTLE_AI", "10/min"),
    },
    "EXCEPTION_HANDLER": "roles.exceptions.coded_exception_handler",
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}

# Throttling needs a cache. LocMemCache is per-process, so with N gunicorn
# workers the effective limit is N x the configured rate. That is acceptable
# for the read/write scopes. For the AI scope — which spends real money — the
# limit must be global, so it is enforced in the database instead
# (roles.models.AIRequestLog). See ADR-008 in roles/views.py.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "britam-role-library",
        "TIMEOUT": 300,
    }
}

# ---------------------------------------------------------------------------
# Application settings
# ---------------------------------------------------------------------------

# Absolute path to the legacy single-file HTML that seeds the database.
LEGACY_HTML_PATH = Path(
    env_str("SEED_HTML_PATH", str(BASE_DIR / "Britam_Role_Library.html"))
)

# Anthropic proxy. The key NEVER reaches the browser; see roles/views.py AIView.
ANTHROPIC_API_KEY = env_str("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = env_str("ANTHROPIC_MODEL", "claude-sonnet-4-5")
ANTHROPIC_TIMEOUT = env_int("ANTHROPIC_TIMEOUT", 45)
ANTHROPIC_MAX_TOKENS = env_int("ANTHROPIC_MAX_TOKENS", 1024)
AI_DAILY_BUDGET_REQUESTS = env_int("AI_DAILY_BUDGET_REQUESTS", 500)

APP_VERSION = env_str("APP_VERSION", "dev")

# ---------------------------------------------------------------------------
# Logging
#
# ADR-005: JSON to stdout only. The container is the log shipper's unit of
# work; writing files inside it would need rotation, a volume and a reaper.
# docker compose already caps json-file at 5 MB x 2.
# ---------------------------------------------------------------------------

LOG_LEVEL = env_str("DJANGO_LOG_LEVEL", "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "roles.logging_utils.RequestIDFilter"},
    },
    "formatters": {
        "json": {"()": "roles.logging_utils.JSONFormatter"},
        "console": {
            "format": "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"
        },
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": sys.stdout,
            "filters": ["request_id"],
            "formatter": "console" if DEBUG else "json",
        },
    },
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
        "django.db.backends": {
            "handlers": ["stdout"],
            # SQL logging is deafening; opt in explicitly when debugging.
            "level": env_str("DJANGO_SQL_LOG_LEVEL", "WARNING").upper(),
            "propagate": False,
        },
        "roles": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
    },
}
