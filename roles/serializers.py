"""
API serialization and input validation.

Every externally-supplied field is length-bounded and whitespace-normalised
here. The model layer is the last line of defence (constraints, validators);
this is the first.
"""

from __future__ import annotations

import re

from django.db import transaction
from rest_framework import serializers

from roles.models import AIRequestLog, BusinessUnit, Role, RoleRevision, parse_band_numeric

# Control characters other than tab/newline have no business in this data and
# are a classic vector for breaking downstream CSV/log consumers.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
BAND_LABEL_RE = re.compile(r"^[A-Za-z0-9 .\-/&()]{0,40}$")

MAX_TEXT = 8000
MAX_LINE = 255


def scrub(value: str | None, *, max_length: int, collapse_ws: bool = False) -> str:
    """Strip control characters, normalise whitespace, enforce a hard cap."""
    if value is None:
        return ""
    text = CONTROL_CHARS_RE.sub("", str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if collapse_ws:
        text = re.sub(r"\s+", " ", text)
    text = text.strip()
    if len(text) > max_length:
        raise serializers.ValidationError(
            f"Must be at most {max_length} characters (received {len(text)})."
        )
    return text


class BusinessUnitSerializer(serializers.ModelSerializer):
    role_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = BusinessUnit
        fields = ("id", "name", "slug", "display_order", "is_active", "role_count")
        read_only_fields = ("id", "slug", "role_count")

    def validate_name(self, value: str) -> str:
        name = scrub(value, max_length=120, collapse_ws=True)
        if not name:
            raise serializers.ValidationError("Business unit name cannot be blank.")
        qs = BusinessUnit.objects.filter(name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(f"A business unit named '{name}' already exists.")
        return name


class RoleSerializer(serializers.ModelSerializer):
    """Read/write representation of a role.

    Writers may identify the business unit either by `business_unit` (pk) or by
    `business_unit_name` (string, created on the fly). The Manage tab uses the
    name form so an editor can add a role for a brand-new BU without a second
    round trip.
    """

    business_unit = serializers.PrimaryKeyRelatedField(
        queryset=BusinessUnit.objects.all(), required=False, allow_null=True
    )
    business_unit_name = serializers.CharField(
        required=False, allow_blank=True, write_only=True, max_length=120
    )
    bu = serializers.CharField(source="business_unit.name", read_only=True)
    bu_id = serializers.IntegerField(source="business_unit_id", read_only=True)

    # Legacy-compatible aliases, so the front-end keeps its original field
    # names and the career-path/compare code needs no rewrite.
    pos = serializers.CharField(source="position", read_only=True)
    bandN = serializers.SerializerMethodField()
    exp = serializers.CharField(source="experience", read_only=True)
    quals = serializers.CharField(source="qualifications", read_only=True)
    desc = serializers.CharField(source="purpose", read_only=True)
    focus = serializers.CharField(source="focus_areas", read_only=True)
    reports = serializers.CharField(source="direct_reports", read_only=True)
    techcomp = serializers.CharField(source="technical_competencies", read_only=True)
    leadcomp = serializers.CharField(source="leadership_competencies", read_only=True)

    class Meta:
        model = Role
        fields = (
            "id",
            "business_unit",
            "business_unit_name",
            "bu",
            "bu_id",
            "position",
            "pos",
            "band",
            "band_numeric",
            "bandN",
            "level",
            "experience",
            "exp",
            "qualifications",
            "quals",
            "purpose",
            "desc",
            "focus_areas",
            "focus",
            "kras",
            "direct_reports",
            "reports",
            "technical_competencies",
            "techcomp",
            "leadership_competencies",
            "leadcomp",
            "is_active",
            "updated_at",
        )
        read_only_fields = ("id", "band_numeric", "updated_at")
        # ADR-013: suppress the UniqueTogetherValidator DRF auto-generates from
        # the `uniq_role_per_bu` constraint. That validator calls
        # enforce_required_fields(), which forces `business_unit` to be
        # supplied as a primary key — defeating the business_unit_name
        # convenience the Manage tab relies on. Uniqueness is still enforced,
        # twice: in validate() below (friendly field-level message) and by the
        # database constraint (which surfaces as a 409 via
        # roles.exceptions.coded_exception_handler if two writers race).
        validators: list = []

    def get_bandN(self, obj: Role) -> float | None:
        return float(obj.band_numeric) if obj.band_numeric is not None else None

    # -- field level validation -------------------------------------------

    def validate_position(self, value: str) -> str:
        position = scrub(value, max_length=MAX_LINE, collapse_ws=True)
        if not position:
            raise serializers.ValidationError("Job title is required.")
        if len(position) < 2:
            raise serializers.ValidationError("Job title must be at least 2 characters.")
        return position

    def validate_band(self, value: str) -> str:
        band = scrub(value, max_length=40, collapse_ws=True)
        if band and not BAND_LABEL_RE.match(band):
            raise serializers.ValidationError(
                "Band may only contain letters, digits, spaces and . - / & ( )."
            )
        return band

    def validate_level(self, value: str) -> str:
        return scrub(value, max_length=80, collapse_ws=True)

    def validate_experience(self, value: str) -> str:
        return scrub(value, max_length=MAX_TEXT)

    def validate_qualifications(self, value: str) -> str:
        return scrub(value, max_length=MAX_TEXT)

    def validate_purpose(self, value: str) -> str:
        return scrub(value, max_length=MAX_TEXT)

    def validate_focus_areas(self, value: str) -> str:
        return scrub(value, max_length=MAX_TEXT)

    def validate_kras(self, value: str) -> str:
        return scrub(value, max_length=MAX_TEXT)

    def validate_direct_reports(self, value: str) -> str:
        return scrub(value, max_length=MAX_TEXT)

    def validate_technical_competencies(self, value: str) -> str:
        return scrub(value, max_length=MAX_TEXT)

    def validate_leadership_competencies(self, value: str) -> str:
        return scrub(value, max_length=MAX_TEXT)

    def validate_business_unit_name(self, value: str) -> str:
        return scrub(value, max_length=120, collapse_ws=True)

    # -- object level ------------------------------------------------------

    def validate(self, attrs):
        bu = attrs.get("business_unit")
        bu_name = attrs.pop("business_unit_name", "").strip()

        if not bu and bu_name:
            # Case-insensitive match first so "Internal audit" does not create a
            # duplicate of "Internal Audit".
            bu = BusinessUnit.objects.filter(name__iexact=bu_name).first()
            if bu is None:
                bu = BusinessUnit(name=bu_name, display_order=100)
                bu.full_clean(exclude=["slug"])
                self._pending_business_unit = bu
            attrs["business_unit"] = bu

        if not attrs.get("business_unit") and not getattr(self, "_pending_business_unit", None):
            if self.instance is None:
                raise serializers.ValidationError(
                    {"business_unit": ["Select an existing business unit or supply a name."]}
                )

        position = attrs.get("position") or (self.instance.position if self.instance else "")
        target_bu = attrs.get("business_unit") or (
            self.instance.business_unit if self.instance else None
        )

        # Pre-empt the DB constraint so the editor gets a field-level message
        # instead of a 409. The constraint still exists for the race window.
        if position and target_bu and getattr(target_bu, "pk", None):
            clash = Role.objects.filter(business_unit=target_bu, position__iexact=position)
            if self.instance:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"position": [f"'{position}' already exists in {target_bu.name}."]}
                )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        pending_bu = getattr(self, "_pending_business_unit", None)
        if pending_bu is not None and pending_bu.pk is None:
            # get_or_create closes the race where two editors add the same new
            # BU at the same moment; the loser reuses the winner's row.
            bu, _created = BusinessUnit.objects.get_or_create(
                name=pending_bu.name, defaults={"display_order": pending_bu.display_order}
            )
            validated_data["business_unit"] = bu
            self._pending_business_unit = None

        user = self._actor()
        validated_data["source"] = "manual"
        role = Role(**validated_data)
        role.created_by = user
        role.updated_by = user
        role.full_clean(exclude=["slug", "content_hash", "band_numeric"])
        role.save()
        return role

    @transaction.atomic
    def update(self, instance, validated_data):
        pending_bu = getattr(self, "_pending_business_unit", None)
        if pending_bu is not None and pending_bu.pk is None:
            bu, _created = BusinessUnit.objects.get_or_create(
                name=pending_bu.name, defaults={"display_order": pending_bu.display_order}
            )
            validated_data["business_unit"] = bu
            self._pending_business_unit = None

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.updated_by = self._actor()
        instance.full_clean(exclude=["slug", "content_hash", "band_numeric"])
        instance.save()
        return instance

    def _actor(self):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return user if user is not None and user.is_authenticated else None


class RoleRevisionSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = RoleRevision
        fields = (
            "id",
            "role",
            "role_label",
            "action",
            "action_display",
            "actor_label",
            "changes",
            "request_id",
            "created_at",
        )
        read_only_fields = fields


class AIQuestionSerializer(serializers.Serializer):
    """Input contract for the AI Assistant proxy."""

    question = serializers.CharField(max_length=1000, trim_whitespace=True)

    def validate_question(self, value: str) -> str:
        question = scrub(value, max_length=1000, collapse_ws=True)
        if len(question) < 3:
            raise serializers.ValidationError("Please ask a slightly longer question.")
        return question


class AIRequestLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIRequestLog
        fields = ("id", "created_at", "question", "succeeded", "error_code", "latency_ms")
        read_only_fields = fields


def role_band_numeric_preview(band: str) -> float | None:
    """Exposed for the admin's live band preview and for tests."""
    value = parse_band_numeric(band)
    return float(value) if value is not None else None
