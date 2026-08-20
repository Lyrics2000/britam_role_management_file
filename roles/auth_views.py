"""
Sign-in with brute-force protection.

Replaces the `limit_req zone=login rate=12r/m` that lived in the nginx sidecar
before the sidecar was removed. See ADR-016 in roles/models.py for why this is
in the application rather than left to whatever nginx the host happens to run.

Two independent counters, both over a rolling window:

  per IP        LOGIN_MAX_ATTEMPTS_PER_IP failures from one address.
                Stops the ordinary case — a script hammering one host.

  per username  LOGIN_MAX_ATTEMPTS_PER_USERNAME failures against one account
                from *any* address. Stops credential stuffing spread across a
                botnet, which the per-IP counter cannot see.

The per-username counter is deliberately set much higher than the per-IP one,
because it carries a denial-of-service trade-off: anyone who knows a username
can lock that account out by failing at it repeatedly. Set it high enough that
only an attack reaches it, never a person mistyping a password. It can be
disabled entirely with LOGIN_MAX_ATTEMPTS_PER_USERNAME=0 if you would rather
accept brute-force exposure than lockout risk.

A successful sign-in clears that identity's failures, so someone who fumbles
their password three times and then gets it right starts from a clean slate.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.db import DatabaseError
from django.utils import timezone

from roles.logging_utils import get_request_id
from roles.middleware import client_ip
from roles.models import LoginAttempt

logger = logging.getLogger(__name__)

# Rows older than this multiple of the window are pruned opportunistically, so
# the table cannot grow without bound on a site nobody cleans up after.
RETENTION_MULTIPLIER = 8


class ThrottledLoginView(auth_views.LoginView):
    """Django's LoginView plus a database-backed attempt limiter."""

    template_name = "roles/login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        ip = client_ip(request) or None
        username = (request.POST.get("username") or "")[:150].strip()

        blocked_reason = self._blocked_reason(ip, username)
        if blocked_reason:
            logger.warning(
                "login blocked by rate limit",
                extra={"client_ip": ip, "username": username, "reason": blocked_reason},
            )
            self._record(request, ip, username, succeeded=False)
            form = self.get_form()
            form.is_valid()  # populate cleaned_data/errors so the template renders
            form.add_error(None, self._lockout_message())
            response = self.render_to_response(self.get_context_data(form=form))
            response.status_code = 429
            response["Retry-After"] = str(settings.LOGIN_ATTEMPT_WINDOW_SECONDS)
            return response

        response = super().post(request, *args, **kwargs)

        # LoginView redirects (302) on success and re-renders the form (200) on
        # failure, so the status code is the authoritative outcome signal.
        succeeded = response.status_code in (301, 302)
        self._record(request, ip, username, succeeded=succeeded)

        if succeeded:
            self._clear_failures(ip, username)
            logger.info("login succeeded", extra={"client_ip": ip, "username": username})
        else:
            logger.warning("login failed", extra={"client_ip": ip, "username": username})

        return response

    # -- limiter ----------------------------------------------------------

    def _blocked_reason(self, ip: str | None, username: str) -> str:
        """Return a short reason string when this attempt must be refused."""
        window_start = timezone.now() - timezone.timedelta(
            seconds=settings.LOGIN_ATTEMPT_WINDOW_SECONDS
        )
        try:
            failures = LoginAttempt.objects.filter(succeeded=False, created_at__gte=window_start)

            ip_limit = settings.LOGIN_MAX_ATTEMPTS_PER_IP
            if ip and ip_limit and failures.filter(ip=ip).count() >= ip_limit:
                return "ip"

            user_limit = settings.LOGIN_MAX_ATTEMPTS_PER_USERNAME
            if username and user_limit and failures.filter(username=username).count() >= user_limit:
                return "username"
        except DatabaseError:
            # Fail closed would lock everyone out of a working site over a
            # transient database blip; fail open and shout about it instead.
            logger.exception("login rate limiter could not read the database")
            return ""
        return ""

    def _lockout_message(self) -> str:
        minutes = max(1, settings.LOGIN_ATTEMPT_WINDOW_SECONDS // 60)
        return (
            f"Too many failed sign-in attempts. Please wait {minutes} minutes and try "
            f"again, or ask an administrator to reset your password."
        )

    def _record(self, request, ip: str | None, username: str, *, succeeded: bool) -> None:
        try:
            LoginAttempt.objects.create(
                ip=ip,
                username=username,
                succeeded=succeeded,
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:200],
                request_id=get_request_id()[:36],
            )
            self._prune()
        except DatabaseError:
            # Never let audit bookkeeping break a sign-in.
            logger.exception("could not record login attempt")

    def _clear_failures(self, ip: str | None, username: str) -> None:
        try:
            stale = LoginAttempt.objects.filter(succeeded=False)
            if username:
                stale.filter(username=username).delete()
            if ip:
                stale.filter(ip=ip).delete()
        except DatabaseError:
            logger.exception("could not clear login failures after a successful sign-in")

    def _prune(self) -> None:
        cutoff = timezone.now() - timezone.timedelta(
            seconds=settings.LOGIN_ATTEMPT_WINDOW_SECONDS * RETENTION_MULTIPLIER
        )
        LoginAttempt.objects.filter(created_at__lt=cutoff).delete()
