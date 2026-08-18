"""
Load the legacy `Britam_Role_Library.html` data into SQLite.

This is what makes `docker compose up` populate the database. It runs from the
container entrypoint on every boot and is *idempotent*: the first run inserts,
subsequent runs are a no-op unless the HTML actually changed.

    python manage.py seed_roles                  # insert new, update changed
    python manage.py seed_roles --dry-run        # report, change nothing
    python manage.py seed_roles --prune          # also deactivate rows no
                                                 # longer present in the HTML
    python manage.py seed_roles --force          # rewrite every row
    python manage.py seed_roles --file other.html

Concurrency: with `replicas: 2` or a rolling restart, two containers can run
this at the same moment. Each row is written inside its own atomic block via
get_or_create on the (business_unit, position) unique constraint, so the loser
of a race updates rather than crashing. The whole command retries on
"database is locked" with exponential backoff.
"""

from __future__ import annotations

import logging
import random
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from roles.legacy_html import LegacyParseError, extract_roles, iter_business_units
from roles.models import BusinessUnit, Role, parse_band_numeric

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 0.25

CONTENT_ASSIGNMENTS = (
    ("position", "position"),
    ("band", "band"),
    ("level", "level"),
    ("experience", "experience"),
    ("qualifications", "qualifications"),
    ("purpose", "purpose"),
    ("focus_areas", "focus_areas"),
    ("kras", "kras"),
    ("direct_reports", "direct_reports"),
    ("technical_competencies", "technical_competencies"),
    ("leadership_competencies", "leadership_competencies"),
)


class Command(BaseCommand):
    help = "Seed or refresh the role library from the legacy Britam_Role_Library.html file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            dest="html_file",
            default=str(settings.LEGACY_HTML_PATH),
            help="Path to the legacy HTML file. Defaults to SEED_HTML_PATH.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report what would change without writing to the database.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Rewrite every row even when the content hash is unchanged.",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Deactivate seeded roles that no longer appear in the HTML file. "
                 "Manually created roles are never touched.",
        )
        parser.add_argument(
            "--quiet-if-seeded",
            action="store_true",
            help="Exit immediately (status 0) if the database already holds roles. "
                 "Used by the container entrypoint on warm restarts.",
        )

    def handle(self, *args, **options):
        started = time.perf_counter()
        html_file = options["html_file"]
        dry_run = options["dry_run"]
        force = options["force"]
        prune = options["prune"]

        if options["quiet_if_seeded"]:
            existing = self._retry(Role.objects.count)
            if existing:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"seed_roles: database already holds {existing} role(s); nothing to do."
                    )
                )
                return

        try:
            extraction = extract_roles(html_file)
        except LegacyParseError as exc:
            # A parse failure must be loud: the site would otherwise come up empty.
            logger.error("seed failed to parse legacy html", extra={"error": str(exc)})
            raise CommandError(str(exc)) from exc

        if not extraction.records:
            raise CommandError(
                f"[SEED-003] parsed {html_file} but found zero usable role records."
            )

        for index, reason in extraction.skipped:
            self.stderr.write(self.style.WARNING(f"  skipped record #{index}: {reason}"))

        self.stdout.write(
            f"seed_roles: parsed {extraction.count} role(s) from {html_file}"
            + (" [DRY RUN]" if dry_run else "")
        )

        bu_by_name = self._sync_business_units(extraction, dry_run=dry_run)

        stats = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
        seen_keys: set[tuple[int, str]] = set()

        for record in extraction.records:
            bu = bu_by_name.get(record["business_unit_name"])
            if bu is None:
                # Only reachable in --dry-run, where new BUs were not written.
                stats["unchanged"] += 1
                continue
            try:
                outcome = self._upsert_role(record, bu, dry_run=dry_run, force=force)
            except (IntegrityError, OperationalError) as exc:
                stats["failed"] += 1
                logger.exception(
                    "seed row failed",
                    extra={"bu": bu.name, "position": record["position"], "exc": str(exc)},
                )
                self.stderr.write(
                    self.style.ERROR(
                        f"  FAILED {bu.name} / {record['position']}: {type(exc).__name__}: {exc}"
                    )
                )
                continue
            stats[outcome] += 1
            if bu.pk:
                seen_keys.add((bu.pk, record["position"].casefold()))

        pruned = 0
        if prune and not dry_run:
            pruned = self._prune(seen_keys)

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        summary = (
            f"seed_roles: created={stats['created']} updated={stats['updated']} "
            f"unchanged={stats['unchanged']} failed={stats['failed']} pruned={pruned} "
            f"in {elapsed_ms}ms"
        )
        logger.info(
            "seed complete",
            extra={
                # NB: 'created'/'msecs'/'module' etc. are reserved LogRecord
                # attribute names — passing them via extra= raises KeyError.
                "created_count": stats["created"],
                "updated_count": stats["updated"],
                "unchanged_count": stats["unchanged"],
                "failed_count": stats["failed"],
                "pruned_count": pruned,
                "duration_ms": elapsed_ms,
                "source": html_file,
            },
        )
        style = self.style.WARNING if stats["failed"] else self.style.SUCCESS
        self.stdout.write(style(summary))

        if stats["failed"]:
            raise CommandError(
                f"[SEED-004] {stats['failed']} role(s) could not be written. "
                f"See the errors above; the rest of the data loaded successfully."
            )

    # -- steps -------------------------------------------------------------

    def _sync_business_units(self, extraction, *, dry_run: bool) -> dict[str, BusinessUnit]:
        """Create any missing BUs, preserving first-appearance order."""
        mapping: dict[str, BusinessUnit] = {}
        for order, name in enumerate(iter_business_units(extraction), start=1):
            existing = BusinessUnit.objects.filter(name__iexact=name).first()
            if existing:
                mapping[name] = existing
                continue
            if dry_run:
                self.stdout.write(f"  would create business unit: {name}")
                continue
            bu, created = self._retry(
                BusinessUnit.objects.get_or_create,
                name=name,
                defaults={"display_order": order},
            )
            if created:
                logger.info("business unit created by seeder", extra={"bu": name})
                self.stdout.write(f"  + business unit: {name}")
            mapping[name] = bu
        return mapping

    def _upsert_role(self, record: dict, bu: BusinessUnit, *, dry_run: bool, force: bool) -> str:
        """Insert or refresh one role. Returns 'created' | 'updated' | 'unchanged'."""
        position = record["position"]

        candidate = Role(business_unit=bu)
        for record_key, model_field in CONTENT_ASSIGNMENTS:
            setattr(candidate, model_field, record[record_key] or "")
        # Derived the same way Role.save() derives it, so the candidate hash
        # is comparable with the stored one. Using the HTML's bandN directly
        # produced Decimal('9.0') vs the model's Decimal('9') and made every
        # boot report the row as "updated".
        candidate.band_numeric = parse_band_numeric(candidate.band)
        target_hash = candidate.compute_hash()

        existing = (
            Role.objects.select_related("business_unit")
            .filter(business_unit=bu, position__iexact=position)
            .first()
        )

        if existing is None:
            if dry_run:
                self.stdout.write(f"  would create: {bu.name} / {position}")
                return "created"
            role = Role(business_unit=bu, source="seed")
            for record_key, model_field in CONTENT_ASSIGNMENTS:
                setattr(role, model_field, record[record_key] or "")
            role._audit_actor_label = "seed"
            try:
                self._retry(self._save_new, role)
            except IntegrityError:
                # Another container inserted the same row microseconds ago.
                # Fall through to the update path rather than failing the boot.
                logger.info(
                    "seed lost an insert race; switching to update",
                    extra={"bu": bu.name, "position": position},
                )
                existing = Role.objects.filter(business_unit=bu, position__iexact=position).first()
                if existing is None:
                    raise
            else:
                return "created"

        if existing.content_hash == target_hash and not force:
            return "unchanged"

        if dry_run:
            self.stdout.write(f"  would update: {bu.name} / {position}")
            return "updated"

        for record_key, model_field in CONTENT_ASSIGNMENTS:
            setattr(existing, model_field, record[record_key] or "")
        # A role a human later edited stays flagged 'manual' so --prune skips it.
        if existing.source == "seed":
            existing._audit_is_seed = True
        existing._audit_actor_label = "seed"
        self._retry(self._save_existing, existing)
        return "updated"

    @staticmethod
    @transaction.atomic
    def _save_new(role: Role) -> None:
        role.save()

    @staticmethod
    @transaction.atomic
    def _save_existing(role: Role) -> None:
        role.save()

    def _prune(self, seen_keys: set[tuple[int, str]]) -> int:
        """Deactivate seeded roles absent from the current HTML.

        Deactivate, never delete: an HR audit needs the row to still exist, and
        a mistyped --prune must be reversible with one checkbox.
        """
        pruned = 0
        for role in Role.objects.filter(source="seed", is_active=True).select_related("business_unit"):
            if (role.business_unit_id, role.position.casefold()) in seen_keys:
                continue
            role.is_active = False
            role._audit_actor_label = "seed-prune"
            self._retry(self._save_existing, role)
            pruned += 1
            self.stdout.write(
                self.style.WARNING(f"  - deactivated: {role.business_unit.name} / {role.position}")
            )
        if pruned:
            logger.warning("seed pruned roles", extra={"pruned": pruned, "at": timezone.now().isoformat()})
        return pruned

    # -- retry -------------------------------------------------------------

    def _retry(self, func, *args, **kwargs):
        """Run `func`, retrying SQLite lock contention with exponential backoff + jitter.

        Only OperationalErrors whose message mentions a lock are retried;
        anything else (a schema error, say) fails immediately rather than being
        tried five times.
        """
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return func(*args, **kwargs)
            except OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                last_error = exc
                if attempt == MAX_ATTEMPTS:
                    break
                delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                delay += random.uniform(0, delay * 0.25)  # jitter, avoids lockstep retries
                logger.warning(
                    "database locked, retrying",
                    extra={"attempt": attempt, "delay_s": round(delay, 3)},
                )
                time.sleep(delay)
        raise CommandError(
            f"[SEED-005] database stayed locked after {MAX_ATTEMPTS} attempts: {last_error}"
        )
