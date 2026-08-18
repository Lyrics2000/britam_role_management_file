"""
Uniform error envelope.

Every API error comes back as:

    {
      "error": {
        "code": "ROLE-VALIDATION",
        "message": "One or more fields are invalid.",
        "details": {"position": ["This field is required."]},
        "request_id": "3f2a91c0d4e15b78"
      }
    }

so the browser can show `message`, and support can grep the container logs by
`request_id` to find the matching stack trace. The stack trace itself is logged
server-side and never returned to the client.
"""

from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied, ValidationError as DjangoValidationError
from django.db import DatabaseError, IntegrityError
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from roles.logging_utils import get_request_id

logger = logging.getLogger(__name__)

# Maps DRF/Django exception classes onto stable, greppable codes.
CODE_BY_STATUS = {
    400: "REQ-BAD-REQUEST",
    401: "AUTH-REQUIRED",
    403: "AUTH-FORBIDDEN",
    404: "RES-NOT-FOUND",
    405: "REQ-METHOD-NOT-ALLOWED",
    406: "REQ-NOT-ACCEPTABLE",
    409: "RES-CONFLICT",
    415: "REQ-UNSUPPORTED-MEDIA",
    429: "RATE-LIMITED",
    500: "SRV-INTERNAL",
    503: "SRV-UNAVAILABLE",
}


def build_error(code: str, message: str, details=None, http_status: int = 400) -> Response:
    """Construct the standard error response."""
    return Response(
        {
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": get_request_id(),
            }
        },
        status=http_status,
    )


def coded_exception_handler(exc, context):
    """DRF exception handler producing the envelope above."""
    view = context.get("view")
    view_name = view.__class__.__name__ if view else "unknown"

    # Translate the Django-level exceptions DRF does not handle natively.
    if isinstance(exc, DjangoValidationError):
        logger.warning(
            "model validation failed",
            extra={"view": view_name, "messages": exc.messages},
        )
        return build_error(
            "ROLE-VALIDATION",
            "One or more fields are invalid.",
            details=getattr(exc, "message_dict", {"non_field_errors": exc.messages}),
            http_status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, IntegrityError):
        # Almost always the (business_unit, position) unique constraint, hit by
        # two editors submitting the same new role concurrently.
        logger.warning("integrity error", extra={"view": view_name}, exc_info=True)
        return build_error(
            "ROLE-DUPLICATE",
            "A role with this title already exists in this business unit.",
            details={"position": ["Must be unique within the business unit."]},
            http_status=status.HTTP_409_CONFLICT,
        )

    if isinstance(exc, Http404):
        return build_error(
            "RES-NOT-FOUND", "The requested item does not exist.", http_status=404
        )

    if isinstance(exc, PermissionDenied):
        return build_error(
            "AUTH-FORBIDDEN", "You do not have permission to perform this action.", http_status=403
        )

    response = drf_exception_handler(exc, context)

    if response is None:
        # Nothing above matched: a genuine unhandled error. Log the full stack
        # with the request id, return an opaque message.
        logger.exception(
            "unhandled exception in api view",
            extra={"view": view_name, "exc_type": type(exc).__name__},
        )
        if isinstance(exc, DatabaseError):
            return build_error(
                "SRV-DATABASE",
                "The database is temporarily unavailable. Please retry.",
                http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return build_error(
            "SRV-INTERNAL",
            "An unexpected error occurred. Quote the request id when reporting this.",
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    code = CODE_BY_STATUS.get(response.status_code, f"HTTP-{response.status_code}")
    detail = response.data

    if isinstance(detail, dict) and "detail" in detail and len(detail) == 1:
        message = str(detail["detail"])
        details = {}
    elif isinstance(detail, dict):
        message = "One or more fields are invalid."
        code = "ROLE-VALIDATION" if response.status_code == 400 else code
        details = detail
    else:
        message = "Request failed."
        details = {"non_field_errors": detail}

    logger.warning(
        "api error response",
        extra={"view": view_name, "status": response.status_code, "code": code},
    )

    response.data = {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": get_request_id(),
        }
    }
    return response
