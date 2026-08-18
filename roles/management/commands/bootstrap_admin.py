"""
Create the first superuser from environment variables, idempotently.

Called by the container entrypoint. Refuses to run without a strong password
rather than inventing a default — a well-known default admin password on a
public droplet is how these deployments get owned.

    DJANGO_SUPERUSER_USERNAME=hradmin \
    DJANGO_SUPERUSER_EMAIL=hr@britam.com \
    DJANGO_SUPERUSER_PASSWORD='...' \
    python manage.py bootstrap_admin
"""

from __future__ import annotations

import logging
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Create or update the bootstrap superuser from DJANGO_SUPERUSER_* env vars."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-if-any-exists",
            action="store_true",
            help="Do nothing if any superuser already exists (entrypoint default).",
        )
        parser.add_argument(
            "--reset-password",
            action="store_true",
            help="Reset the password of an existing user to the env value.",
        )

    def handle(self, *args, **options):
        User = get_user_model()

        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")

        if not username or not password:
            self.stdout.write(
                self.style.WARNING(
                    "bootstrap_admin: DJANGO_SUPERUSER_USERNAME/PASSWORD not set — skipping. "
                    "Create an editor later with: docker compose exec web python manage.py createsuperuser"
                )
            )
            return

        if options["skip_if_any_exists"] and User.objects.filter(is_superuser=True).exists():
            self.stdout.write(
                self.style.SUCCESS("bootstrap_admin: a superuser already exists; skipping.")
            )
            return

        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError(
                "[BOOT-001] DJANGO_SUPERUSER_PASSWORD is too weak: "
                + "; ".join(exc.messages)
                + " Pick a longer passphrase in your .env file."
            ) from exc

        with transaction.atomic():
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": email, "is_staff": True, "is_superuser": True},
            )
            if created:
                user.set_password(password)
                user.save(update_fields=["password"])
                logger.info("bootstrap superuser created", extra={"username": username})
                self.stdout.write(
                    self.style.SUCCESS(f"bootstrap_admin: created superuser '{username}'.")
                )
                return

            changed = []
            if not user.is_staff or not user.is_superuser:
                user.is_staff = True
                user.is_superuser = True
                changed += ["is_staff", "is_superuser"]
            if email and user.email != email:
                user.email = email
                changed.append("email")
            if options["reset_password"]:
                user.set_password(password)
                changed.append("password")
            if changed:
                user.save(update_fields=changed)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"bootstrap_admin: updated '{username}' ({', '.join(changed)})."
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(f"bootstrap_admin: '{username}' already correct; no change.")
                )
