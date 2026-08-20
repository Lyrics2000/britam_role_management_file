"""
Data layer for the Britam Group Role Library.

The shape mirrors the 13 fields that existed in the legacy in-browser ROLES
array, so nothing is lost in the migration from the single HTML file:

    bu, pos, band, bandN, level, exp, quals, desc, focus, kras, reports,
    techcomp, leadcomp

with `bu` promoted to its own table (it is a controlled vocabulary of 19
values, and the UI filters and groups by it).
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

# Fields that make up a role's content fingerprint. Used by the seeder to
# decide whether a row actually changed (see RoleQuerySet.upsert_from_legacy).
CONTENT_FIELDS = (
    "position",
    "band",
    "band_numeric",
    "level",
    "experience",
    "qualifications",
    "purpose",
    "focus_areas",
    "kras",
    "direct_reports",
    "technical_competencies",
    "leadership_competencies",
)

BAND_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_band_numeric(band: str) -> Decimal | None:
    """Derive the sortable numeric band from a label like 'Band 6.2' -> 6.2.

    Returns None when the label carries no number (e.g. 'Executive'), which the
    UI renders last. Never raises: bad input becomes None, because a malformed
    band label must not block an HR editor from saving the rest of the role.
    """
    if not band:
        return None
    match = BAND_NUMBER_RE.search(band)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:  # pragma: no cover - regex guarantees a number
        return None


class TimeStampedModel(models.Model):
    """created_at/updated_at plus who did it, on every editable table."""

    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="%(class)s_created",
        editable=False,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="%(class)s_updated",
        editable=False,
    )

    class Meta:
        abstract = True


class BusinessUnit(TimeStampedModel):
    """A BU or Group function, e.g. 'Internal Audit', 'BLA', 'CX'."""

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    display_order = models.PositiveIntegerField(
        default=100,
        help_text="Lower numbers appear first in the BU tab strip. Ties break alphabetically.",
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ("display_order", "name")
        verbose_name = "business unit"
        verbose_name_plural = "business units"
        indexes = [models.Index(fields=["is_active", "display_order"])]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "bu"
            candidate = base
            suffix = 2
            # Slug collisions are possible ("BLA (KE)" and "BLA KE" both slug
            # to "bla-ke"); walk until free rather than raising on the unique
            # constraint, which would surface as a 500 to the editor.
            while (
                BusinessUnit.objects.filter(slug=candidate)
                .exclude(pk=self.pk)
                .exists()
            ):
                candidate = f"{base}-{suffix}"
                suffix += 1
            self.slug = candidate
        super().save(*args, **kwargs)


class RoleQuerySet(models.QuerySet):
    def active(self) -> "RoleQuerySet":
        return self.filter(is_active=True)

    def with_bu(self) -> "RoleQuerySet":
        return self.select_related("business_unit")

    def search(self, term: str) -> "RoleQuerySet":
        """Free-text search across the fields the old client-side filter used.

        ADR-006: plain icontains rather than SQLite FTS5. 290 rows fit in a few
        hundred KB; a full scan is sub-millisecond, and FTS5 would add a shadow
        table to keep in sync on every write plus a rebuild step in the seeder.
        Revisit past ~50k roles.
        """
        term = (term or "").strip()
        if not term:
            return self
        return self.filter(
            models.Q(position__icontains=term)
            | models.Q(business_unit__name__icontains=term)
            | models.Q(purpose__icontains=term)
            | models.Q(focus_areas__icontains=term)
            | models.Q(qualifications__icontains=term)
        )


class Role(TimeStampedModel):
    """One position in the role library."""

    class Level(models.TextChoices):
        # The legacy data uses free text and contains near-duplicates
        # ("Leader of Leaders" / "Leaders of Leaders"). Kept as a CharField
        # with choices offered as guidance rather than enforced, so the seeder
        # never drops a row it cannot classify.
        LEADER_OF_LEADERS = "Leader of Leaders", "Leader of Leaders"
        CHANGE_LEADER = "Change Leader", "Change Leader"
        EMERGING_LEADER = "Emerging Leader", "Emerging Leader"
        INDIVIDUAL_LEADER = "Individual Leader", "Individual Leader"
        DIRECT_CONTRIBUTOR = "Direct Contributor", "Direct Contributor"

    business_unit = models.ForeignKey(
        BusinessUnit,
        on_delete=models.PROTECT,
        related_name="roles",
        help_text="BU or Group function this role sits in.",
    )
    position = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Job title exactly as it appears in the grading structure.",
    )
    band = models.CharField(
        max_length=40,
        blank=True,
        db_index=True,
        help_text="Band label, e.g. 'Band 6.2'.",
    )
    band_numeric = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        null=True,
        blank=True,
        db_index=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("99"))],
        help_text="Auto-derived from the band label; used for sorting and career-path maths.",
    )
    level = models.CharField(
        max_length=80,
        blank=True,
        db_index=True,
        help_text="Leadership level, e.g. 'Change Leader'.",
    )
    experience = models.TextField(blank=True, help_text="Experience required.")
    qualifications = models.TextField(blank=True, help_text="Academic and professional qualifications.")
    purpose = models.TextField(blank=True, help_text="Role purpose / description.")
    focus_areas = models.TextField(
        blank=True, help_text="Key focus areas. Comma-separated in the legacy data."
    )
    kras = models.TextField(blank=True, help_text="Key performance measures / KRAs.")
    direct_reports = models.TextField(blank=True, help_text="Direct reports, or 'None'.")
    technical_competencies = models.TextField(
        blank=True, help_text="Technical / functional competencies. Pipe-separated in the legacy data."
    )
    leadership_competencies = models.TextField(
        blank=True, help_text="Leadership competencies. Pipe-separated in the legacy data."
    )

    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Unticked roles are hidden from the public site but kept for history.",
    )
    source = models.CharField(
        max_length=20,
        default="manual",
        editable=False,
        help_text="'seed' if it came from the legacy HTML, 'manual' if a person created it.",
    )
    content_hash = models.CharField(max_length=64, editable=False, blank=True, db_index=True)

    objects = RoleQuerySet.as_manager()

    class Meta:
        ordering = ("business_unit__display_order", "business_unit__name", "band_numeric", "position")
        constraints = [
            models.UniqueConstraint(
                fields=["business_unit", "position"],
                name="uniq_role_per_bu",
                violation_error_message=(
                    "A role with this title already exists in this business unit."
                ),
            )
        ]
        indexes = [
            models.Index(fields=["is_active", "business_unit"], name="role_active_bu_idx"),
            models.Index(fields=["band_numeric", "position"], name="role_band_pos_idx"),
            models.Index(fields=["level"], name="role_level_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.position} ({self.business_unit_id and self.business_unit.name})"

    def compute_hash(self) -> str:
        """Stable fingerprint of the content fields, used for idempotent seeding."""
        parts = []
        for field in CONTENT_FIELDS:
            value = getattr(self, field)
            if value is None:
                parts.append("")
            elif isinstance(value, Decimal):
                # Decimal('9.00') and Decimal('9') are the same band; without
                # normalize() the seeder would rewrite the row on every boot.
                parts.append(str(value.normalize()))
            else:
                parts.append(str(value))
        payload = "\x1f".join(parts)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        # band_numeric is derived, never hand-entered, so the two can't drift.
        self.band_numeric = parse_band_numeric(self.band)
        self.content_hash = self.compute_hash()
        super().save(*args, **kwargs)

    # -- presentation helpers used by the API serializer --------------------

    @property
    def focus_list(self) -> list[str]:
        return [p.strip() for p in re.split(r"[,|]", self.focus_areas or "") if p.strip()]

    @property
    def technical_list(self) -> list[str]:
        return [p.strip() for p in (self.technical_competencies or "").split("|") if p.strip()]

    @property
    def leadership_list(self) -> list[str]:
        return [p.strip() for p in (self.leadership_competencies or "").split("|") if p.strip()]


class RoleRevision(models.Model):
    """Append-only audit trail.

    ADR-007: a hand-rolled revision table rather than django-reversion. We need
    exactly one thing — "who changed what, when" for an HR audit — and
    django-reversion brings a serializer registry, admin integration and its
    own migrations. One model with a JSON snapshot is less to maintain and
    survives model changes because it stores plain dicts.
    """

    class Action(models.TextChoices):
        CREATE = "create", "Created"
        UPDATE = "update", "Updated"
        DELETE = "delete", "Deleted"
        SEED = "seed", "Seeded from HTML"

    role = models.ForeignKey(
        Role,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revisions",
    )
    role_label = models.CharField(
        max_length=320,
        help_text="Denormalised '<BU> / <position>' so the trail survives deletion.",
    )
    action = models.CharField(max_length=10, choices=Action.choices, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    actor_label = models.CharField(max_length=150, blank=True)
    changes = models.JSONField(
        default=dict,
        blank=True,
        help_text="{field: {'from': old, 'to': new}} for updates; full snapshot for create/delete.",
    )
    request_id = models.CharField(max_length=36, blank=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "role revision"
        verbose_name_plural = "role revisions"
        indexes = [models.Index(fields=["-created_at", "action"], name="rev_created_action_idx")]

    def __str__(self) -> str:
        return f"{self.get_action_display()} {self.role_label} @ {self.created_at:%Y-%m-%d %H:%M}"


class AIRequestLog(models.Model):
    """One row per AI Assistant call.

    Doubles as the global rate limiter: DRF's throttle cache is per gunicorn
    worker, which would multiply the intended limit by the worker count. This
    table is shared, so the daily budget is enforced exactly once.
    """

    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    question = models.TextField()
    succeeded = models.BooleanField(default=False, db_index=True)
    error_code = models.CharField(max_length=40, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    request_id = models.CharField(max_length=36, blank=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "AI request log"
        verbose_name_plural = "AI request logs"
        indexes = [models.Index(fields=["-created_at", "succeeded"], name="ai_created_ok_idx")]

    def __str__(self) -> str:
        status = "ok" if self.succeeded else f"fail:{self.error_code or '?'}"
        return f"AI {status} {self.created_at:%Y-%m-%d %H:%M} {self.question[:40]!r}"


class LoginAttempt(models.Model):
    """One row per sign-in attempt, and the brute-force limiter's storage.

    ADR-016: this moved into the application when the nginx sidecar was
    removed. Previously /accounts/login/ was protected only by an nginx
    `limit_req zone=login rate=12r/m`; with nginx configured separately on the
    host, that protection would have depended on someone remembering to copy a
    rate-limit zone into a file this repo does not own. Authentication
    hardening should not be optional infrastructure.

    Stored in the database rather than the cache because Django's LocMemCache
    is per gunicorn worker: with 3 workers a cache-based counter would let
    through 3x the intended attempts, and would reset on every deploy.
    """

    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    username = models.CharField(max_length=150, db_index=True)
    succeeded = models.BooleanField(default=False, db_index=True)
    user_agent = models.CharField(max_length=200, blank=True)
    request_id = models.CharField(max_length=36, blank=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "login attempt"
        verbose_name_plural = "login attempts"
        indexes = [
            models.Index(fields=["ip", "-created_at"], name="login_ip_time_idx"),
            models.Index(fields=["username", "-created_at"], name="login_user_time_idx"),
        ]

    def __str__(self) -> str:
        outcome = "ok" if self.succeeded else "failed"
        return f"{outcome} login for {self.username!r} from {self.ip} at {self.created_at:%Y-%m-%d %H:%M}"
