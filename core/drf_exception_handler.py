import logging
from typing import Any, Dict

from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import (
    AuthenticationFailed,
    MethodNotAllowed,
    NotAuthenticated,
    NotFound,
    ParseError,
    PermissionDenied,
    Throttled,
    ValidationError,
)
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


def _friendly_message_for_exception(exc: Exception) -> str:
    mappings: Dict[type, str] = {
        ValidationError: "Invalid data provided. Please correct the errors and try again.",
        AuthenticationFailed: "Authentication failed. Please log in again.",
        NotAuthenticated: "Authentication credentials were not provided.",
        PermissionDenied: "You do not have permission to perform this action.",
        NotFound: "The requested resource was not found.",
        MethodNotAllowed: "This method is not allowed for this endpoint.",
        ParseError: "Malformed request. Please check your input and try again.",
        Throttled: "Request was throttled. Please wait before retrying.",
    }

    for exc_type, message in mappings.items():
        if isinstance(exc, exc_type):
            if isinstance(exc, Throttled) and exc.wait is not None:
                return f"Request was throttled. Please wait {int(exc.wait)} seconds before retrying."
            return message

    return "Unexpected error occurred. Please try again later."


def secure_exception_handler(exc: Exception, context: Dict[str, Any]):
    """DRF exception handler that hides internal details in production."""
    response = drf_exception_handler(exc, context)

    # Always log the exception with traceback for developer diagnostics.
    logger.exception("DRF handled exception")

    if settings.DEBUG:
        return response

    if response is not None:
        friendly_message = _friendly_message_for_exception(exc)
        return Response({"success": False, "message": friendly_message}, status=response.status_code)

    # Fallback for unhandled exceptions; the middleware will handle the final response
    # but we return a safe response here to be explicit.
    return Response(
        {"success": False, "message": "Unexpected error occurred. Please try again later."},
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
