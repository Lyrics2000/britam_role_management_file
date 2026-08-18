"""ASGI entrypoint. Not used by the default compose stack (gunicorn/WSGI is),
kept so the project can be moved to uvicorn without restructuring."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()
