import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse


class SecureErrorMiddleware:
    """Middleware that hides internal errors from end users in production."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.logger = logging.getLogger(__name__)

    def __call__(self, request):
        try:
            return self.get_response(request)
        except Exception:
            # Log full traceback for developer visibility.
            self.logger.exception("Unhandled exception captured by SecureErrorMiddleware")

            if settings.DEBUG:
                # Re-raise so Django can show the default debug page during development.
                raise

            message = "Unexpected error occurred. Please try again later."
            if request.path.startswith("/api/"):
                return JsonResponse({"success": False, "message": message}, status=500)
            return HttpResponse(message, status=500, content_type="text/plain")
