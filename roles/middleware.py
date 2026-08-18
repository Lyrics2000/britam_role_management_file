"""Request-scoped middleware: correlation ids and access logging."""

from __future__ import annotations

import logging
import time
import uuid

from django.utils.deprecation import MiddlewareMixin

from roles.logging_utils import get_request_id, reset_request_id, set_request_id

logger = logging.getLogger("roles.access")

# Trusted inbound header. nginx sets it (see nginx.conf) so a single request id
# spans the proxy and the application. A client-supplied value is accepted only
# if it looks like a uuid, so it cannot be used to inject into the log stream.
REQUEST_ID_HEADER = "HTTP_X_REQUEST_ID"
MAX_ID_LENGTH = 36


def _sanitise(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()[:MAX_ID_LENGTH]
    if not value:
        return None
    if not all(c.isalnum() or c in "-_" for c in value):
        return None
    return value


class RequestIDMiddleware(MiddlewareMixin):
    """Binds a correlation id for the lifetime of the request."""

    def process_request(self, request):
        incoming = _sanitise(request.META.get(REQUEST_ID_HEADER))
        request_id = incoming or uuid.uuid4().hex[:16]
        request.request_id = request_id
        request._request_id_token = set_request_id(request_id)

    def process_response(self, request, response):
        request_id = getattr(request, "request_id", get_request_id())
        response["X-Request-ID"] = request_id
        token = getattr(request, "_request_id_token", None)
        if token is not None:
            reset_request_id(token)
        return response

    def process_exception(self, request, exception):
        # Leave the id bound so the 500 handler's log line carries it;
        # process_response still runs afterwards and resets it.
        return None


class AccessLogMiddleware(MiddlewareMixin):
    """One structured line per request, with latency.

    Health probes are logged at DEBUG so the every-30s docker healthcheck does
    not drown the useful lines.
    """

    QUIET_PATHS = {"/healthz", "/readyz"}

    def process_request(self, request):
        request._started_at = time.perf_counter()

    def process_response(self, request, response):
        started = getattr(request, "_started_at", None)
        duration_ms = round((time.perf_counter() - started) * 1000, 2) if started else -1

        user = getattr(request, "user", None)
        level = logging.DEBUG if request.path in self.QUIET_PATHS else logging.INFO
        if response.status_code >= 500:
            level = logging.ERROR
        elif response.status_code >= 400:
            level = logging.WARNING

        logger.log(
            level,
            "%s %s -> %s",
            request.method,
            request.get_full_path(),
            response.status_code,
            extra={
                "http_method": request.method,
                "path": request.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "user": getattr(user, "username", "") if user and user.is_authenticated else "anonymous",
                "client_ip": client_ip(request),
                "user_agent": request.META.get("HTTP_USER_AGENT", "")[:200],
            },
        )
        return response


def client_ip(request) -> str:
    """Real client IP behind the nginx proxy.

    Takes the first entry of X-Forwarded-For. This is only trustworthy because
    nginx overwrites the header (proxy_set_header X-Forwarded-For
    $proxy_add_x_forwarded_for) and the container port is not published beyond
    the compose network. If the app is ever exposed directly, this must be
    replaced with a trusted-proxy-count implementation.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return (request.META.get("REMOTE_ADDR") or "")[:45]
