"""
HTTP layer: the public page, the JSON API, the AI proxy and the ops probes.
"""

from __future__ import annotations

import logging
import time

import requests
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.views.generic import TemplateView
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from roles.exceptions import build_error
from roles.filters import RoleFilter
from roles.middleware import client_ip
from roles.models import AIRequestLog, BusinessUnit, Role, RoleRevision
from roles.permissions import ReadOnlyOrStaff
from roles.serializers import (
    AIQuestionSerializer,
    BusinessUnitSerializer,
    RoleRevisionSerializer,
    RoleSerializer,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public page
# ---------------------------------------------------------------------------


class RoleLibraryView(TemplateView):
    """Serves the single-page app.

    The page no longer carries data in its source; it fetches /api/roles/ on
    load. Only the flags the template needs are rendered server-side.
    """

    template_name = "roles/role_library.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        can_edit = bool(user.is_authenticated and user.is_staff)
        # Rendered through |json_script, which escapes </script> and friends,
        # so this cannot be used to break out of the tag.
        context["app_config"] = {
            "canEdit": can_edit,
            "aiEnabled": bool(settings.ANTHROPIC_API_KEY),
            "version": settings.APP_VERSION,
        }
        context["can_edit"] = can_edit
        return context


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class BusinessUnitViewSet(viewsets.ModelViewSet):
    """CRUD for BUs. Read is public; write is staff-only."""

    serializer_class = BusinessUnitSerializer
    permission_classes = [ReadOnlyOrStaff]
    throttle_scope = "roles_read"
    pagination_class = None
    ordering_fields = ("display_order", "name")

    def get_queryset(self):
        return (
            BusinessUnit.objects.all()
            .annotate(role_count=Count("roles", filter=None, distinct=True))
            .order_by("display_order", "name")
        )

    def get_throttles(self):
        self.throttle_scope = "roles_read" if self.request.method in ("GET", "HEAD", "OPTIONS") else "roles_write"
        return super().get_throttles()

    def perform_destroy(self, instance):
        # PROTECT on Role.business_unit would raise a 500-shaped ProtectedError;
        # convert it into a clear 409 before it gets that far.
        if instance.roles.exists():
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                {
                    "detail": (
                        f"'{instance.name}' still has {instance.roles.count()} role(s). "
                        f"Move or delete them first."
                    )
                }
            )
        logger.info("business unit deleted", extra={"bu": instance.name})
        instance.delete()


class RoleViewSet(viewsets.ModelViewSet):
    """CRUD for roles. Read is public; write requires an active staff session."""

    serializer_class = RoleSerializer
    permission_classes = [ReadOnlyOrStaff]
    filterset_class = RoleFilter
    ordering_fields = ("position", "band_numeric", "updated_at", "business_unit__name")
    ordering = ("business_unit__display_order", "band_numeric", "position")

    def get_queryset(self):
        queryset = Role.objects.with_bu()
        # Anonymous callers only ever see active roles. Staff can ask for the
        # full set with ?active=false or ?include_inactive=1 to manage them.
        user = self.request.user
        include_inactive = self.request.query_params.get("include_inactive") in ("1", "true", "yes")
        if not (user.is_authenticated and user.is_staff and include_inactive):
            if "active" not in self.request.query_params:
                queryset = queryset.active()
        return queryset

    def get_throttles(self):
        self.throttle_scope = (
            "roles_read" if self.request.method in ("GET", "HEAD", "OPTIONS") else "roles_write"
        )
        return super().get_throttles()

    def perform_create(self, serializer):
        role = serializer.save()
        role._audit_actor = self.request.user
        logger.info(
            "role created",
            extra={"role_id": role.pk, "position": role.position, "bu": role.business_unit.name},
        )

    def perform_update(self, serializer):
        serializer.instance._audit_actor = self.request.user
        role = serializer.save()
        logger.info("role updated", extra={"role_id": role.pk, "position": role.position})

    def perform_destroy(self, instance):
        instance._audit_actor = self.request.user
        with transaction.atomic():
            instance.delete()

    @action(detail=True, methods=["get"], permission_classes=[IsAdminUser], url_path="history")
    def history(self, request, pk=None):
        """Audit trail for one role. Staff only — it exposes previous values."""
        role = self.get_object()
        revisions = role.revisions.select_related("actor")[:100]
        return Response(RoleRevisionSerializer(revisions, many=True).data)


class MetaView(APIView):
    """Filter vocabularies and headline counts, in one round trip.

    The old page derived these client-side from the embedded array. Computing
    them in SQL keeps the payload small and the numbers correct when a filter
    limits what the browser has loaded.
    """

    permission_classes = [AllowAny]
    throttle_scope = "roles_read"

    def get(self, request):
        active = Role.objects.active()
        bands = [
            row["band"]
            for row in active.exclude(band="").values("band").distinct().order_by("band")
        ]

        def band_sort_key(label: str) -> float:
            digits = "".join(c for c in label if c.isdigit() or c == ".")
            try:
                return float(digits)
            except ValueError:
                return 999.0

        # Only units that actually have a visible role. Deleting the last role
        # in a unit would otherwise leave a dead tab in the BU strip that
        # filters to an empty grid.
        populated_units = (
            BusinessUnit.objects.filter(is_active=True)
            .annotate(role_count=Count("roles", filter=Q(roles__is_active=True)))
            .filter(role_count__gt=0)
            .order_by("display_order", "name")
        )

        payload = {
            "business_units": list(populated_units.values("id", "name", "slug", "role_count")),
            "bands": sorted(bands, key=band_sort_key),
            "levels": sorted(
                row["level"]
                for row in active.exclude(level="").values("level").distinct()
            ),
            "counts": {
                "roles": active.count(),
                "business_units": populated_units.count(),
                "bands": len(bands),
                "levels": active.exclude(level="").values("level").distinct().count(),
            },
            "ai_enabled": bool(settings.ANTHROPIC_API_KEY),
            "version": settings.APP_VERSION,
            "generated_at": timezone.now().isoformat(),
        }
        return Response(payload)


class AIView(APIView):
    """Server-side proxy to the Anthropic Messages API.

    ADR-008: the legacy page called api.anthropic.com straight from the
    browser with `const AI_KEY = ''` waiting to be filled in. Doing that ships
    the key to every visitor — view-source is enough to steal it, and the key
    is org-wide and billable. It also cannot be rate limited, so one script
    could run up an unbounded bill.

    Here the key lives only in the container's environment. The browser posts a
    question; this view builds the role context from the database, calls
    Anthropic server-side, and returns just the answer text.

    Two limits apply:
      * per-IP burst, via DRF's ScopedRateThrottle (default 10/min)
      * a global daily request budget counted in AIRequestLog, which is shared
        across gunicorn workers (the throttle cache is not)
    """

    permission_classes = [AllowAny]
    throttle_scope = "ai"

    ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
    ANTHROPIC_VERSION = "2023-06-01"
    MAX_CONTEXT_ROLES = 320

    SYSTEM_PROMPT = (
        "You are the Britam Group Role Library assistant. Answer strictly from "
        "the role data supplied in the user message. If the answer is not in "
        "that data, say so plainly and suggest which business unit or band the "
        "person should look at instead. Be concise: at most 150 words. Never "
        "invent salary figures, headcount or policy."
    )

    def post(self, request):
        started = time.perf_counter()
        ip = client_ip(request)

        serializer = AIQuestionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        question = serializer.validated_data["question"]

        if not settings.ANTHROPIC_API_KEY:
            self._log_call(request, ip, question, False, "AI-DISABLED", started)
            return build_error(
                "AI-DISABLED",
                "The AI assistant is not configured on this server. Ask IT to set "
                "ANTHROPIC_API_KEY in the .env file and restart the stack.",
                http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Global daily budget. Counted in the DB so it holds across workers.
        since = timezone.now() - timezone.timedelta(days=1)
        used = AIRequestLog.objects.filter(created_at__gte=since, succeeded=True).count()
        if used >= settings.AI_DAILY_BUDGET_REQUESTS:
            self._log_call(request, ip, question, False, "AI-BUDGET", started)
            logger.warning("ai daily budget exhausted", extra={"used": used})
            return build_error(
                "AI-BUDGET",
                "The assistant has reached today's usage limit. Please try again tomorrow.",
                http_status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        context_block = self._build_context()

        try:
            response = requests.post(
                self.ANTHROPIC_URL,
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": self.ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": settings.ANTHROPIC_MODEL,
                    "max_tokens": settings.ANTHROPIC_MAX_TOKENS,
                    "system": self.SYSTEM_PROMPT,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"Role library data:\n{context_block}\n\n"
                                f"Question: {question}"
                            ),
                        }
                    ],
                },
                timeout=settings.ANTHROPIC_TIMEOUT,
            )
        except requests.Timeout:
            self._log_call(request, ip, question, False, "AI-TIMEOUT", started)
            logger.warning("anthropic call timed out", extra={"timeout": settings.ANTHROPIC_TIMEOUT})
            return build_error(
                "AI-TIMEOUT",
                "The assistant took too long to respond. Please try again.",
                http_status=status.HTTP_504_GATEWAY_TIMEOUT,
            )
        except requests.RequestException as exc:
            self._log_call(request, ip, question, False, "AI-UPSTREAM", started)
            logger.exception("anthropic call failed", extra={"exc_type": type(exc).__name__})
            return build_error(
                "AI-UPSTREAM",
                "Could not reach the assistant service. Please try again shortly.",
                http_status=status.HTTP_502_BAD_GATEWAY,
            )

        if response.status_code == 429:
            self._log_call(request, ip, question, False, "AI-UPSTREAM-429", started)
            return build_error(
                "AI-UPSTREAM-429",
                "The assistant is busy right now. Please try again in a moment.",
                http_status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        if response.status_code >= 400:
            # Body may contain the key in an error echo; log the status only.
            self._log_call(request, ip, question, False, f"AI-HTTP-{response.status_code}", started)
            logger.error(
                "anthropic returned an error",
                extra={"status": response.status_code, "body_prefix": response.text[:200]},
            )
            return build_error(
                "AI-UPSTREAM",
                "The assistant returned an error. Ask IT to check the API key and model name.",
                http_status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            body = response.json()
            answer = "".join(
                block.get("text", "") for block in body.get("content", []) if isinstance(block, dict)
            ).strip()
        except ValueError:
            self._log_call(request, ip, question, False, "AI-BAD-JSON", started)
            logger.exception("anthropic response was not json")
            return build_error(
                "AI-UPSTREAM",
                "The assistant returned an unreadable response.",
                http_status=status.HTTP_502_BAD_GATEWAY,
            )

        if not answer:
            self._log_call(request, ip, question, False, "AI-EMPTY", started)
            return build_error(
                "AI-EMPTY",
                "The assistant returned an empty answer. Try rephrasing the question.",
                http_status=status.HTTP_502_BAD_GATEWAY,
            )

        self._log_call(request, ip, question, True, "", started)
        return Response({"answer": answer, "model": settings.ANTHROPIC_MODEL})

    def _build_context(self) -> str:
        rows = (
            Role.objects.active()
            .with_bu()
            .order_by("business_unit__name", "band_numeric")[: self.MAX_CONTEXT_ROLES]
        )
        lines = []
        for role in rows:
            lines.append(
                f"- {role.position} ({role.business_unit.name}, {role.band}, {role.level}): "
                f"{role.purpose} Quals: {role.qualifications} Exp: {role.experience} "
                f"Focus: {role.focus_areas}"
            )
        return "\n".join(lines)

    def _log_call(self, request, ip, question, succeeded, error_code, started) -> None:
        try:
            AIRequestLog.objects.create(
                client_ip=ip or None,
                user=request.user if request.user.is_authenticated else None,
                question=question[:1000],
                succeeded=succeeded,
                error_code=error_code[:40],
                latency_ms=int((time.perf_counter() - started) * 1000),
                request_id=getattr(request, "request_id", "")[:36],
            )
        except DatabaseError:
            # Never let the audit write break the user-facing call.
            logger.exception("failed to write AIRequestLog")


# ---------------------------------------------------------------------------
# Ops probes
# ---------------------------------------------------------------------------


class HealthView(APIView):
    """Liveness. Cheap on purpose: no DB, no disk. Docker restarts on failure."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes: list = []

    def get(self, request):
        return Response({"status": "ok", "version": settings.APP_VERSION})


class ReadyView(APIView):
    """Readiness. Proves the database is reachable and seeded."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes: list = []

    def get(self, request):
        checks: dict[str, object] = {}
        healthy = True

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            checks["database"] = "ok"
        except DatabaseError as exc:
            healthy = False
            checks["database"] = f"error: {type(exc).__name__}"
            logger.exception("readiness probe: database unreachable")

        if healthy:
            try:
                count = Role.objects.count()
                checks["roles"] = count
                if count == 0:
                    healthy = False
                    checks["seed"] = "empty — seed_roles has not run successfully"
                else:
                    checks["seed"] = "ok"
            except DatabaseError:
                healthy = False
                checks["roles"] = "error"

        return Response(
            {"status": "ready" if healthy else "not-ready", "checks": checks,
             "version": settings.APP_VERSION},
            status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )
