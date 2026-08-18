"""App config. Also the place SQLite is tuned for concurrent access."""

from __future__ import annotations

import logging

from django.apps import AppConfig
from django.conf import settings
from django.db.backends.signals import connection_created
from django.dispatch import receiver

logger = logging.getLogger(__name__)


class RolesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "roles"
    verbose_name = "Role library"

    def ready(self) -> None:
        # Importing registers the signal receivers that write the audit trail.
        from roles import signals  # noqa: F401


@receiver(connection_created)
def configure_sqlite(sender, connection, **kwargs):
    """Apply the pragmas that make SQLite safe under multiple gunicorn workers.

    Runs on every new connection because pragmas are per-connection, not stored
    in the file (journal_mode is the exception — it is persistent — but setting
    it again is a cheap no-op).

      journal_mode=WAL   readers do not block the writer and vice versa
      busy_timeout       a blocked writer retries for N ms instead of raising
                         "database is locked" straight back at the user
      synchronous=NORMAL fsync on checkpoint rather than every commit; with WAL
                         this is durable across process crashes, and only at
                         risk on host power loss — acceptable for this data
      foreign_keys=ON    SQLite disables FK enforcement by default
    """
    if connection.vendor != "sqlite":
        return

    timeout_ms = int(settings.DATABASES["default"]["OPTIONS"].get("timeout", 20)) * 1000
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute(f"PRAGMA busy_timeout={timeout_ms};")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA temp_store=MEMORY;")
        # 64 MiB page cache. Negative value = KiB units in SQLite's API.
        cursor.execute("PRAGMA cache_size=-65536;")
