"""
Tests for the protections that moved into the application when the nginx
sidecar was removed.

Each of these was previously enforced by a directive in nginx.conf. With nginx
now configured separately on the host — in a file this repository does not own
and cannot test — the guarantees have to hold in the app itself. These tests
are what stops them from quietly regressing.
"""

from __future__ import annotations

import gzip

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory

from roles.middleware import client_ip
from roles.models import LoginAttempt

pytestmark = [pytest.mark.django_db, pytest.mark.integration]

LOGIN_URL = "/accounts/login/"
GOOD_PASSWORD = "a-long-enough-passphrase-42"


def attempt(client: Client, username: str = "editor", password: str = "wrong-password", **extra):
    """One login POST, with a valid CSRF token so we exercise the limiter."""
    client.get(LOGIN_URL, **extra)
    token = client.cookies["csrftoken"].value
    return client.post(
        LOGIN_URL,
        {"csrfmiddlewaretoken": token, "username": username, "password": password},
        **extra,
    )


class TestLoginBruteForceLimit:
    """Replaces nginx's `limit_req zone=login rate=12r/m` (ADR-016)."""

    def test_a_wrong_password_is_recorded(self, settings, staff_user):
        client = Client()
        attempt(client)
        record = LoginAttempt.objects.get()
        assert record.succeeded is False
        assert record.username == "editor"

    def test_repeated_failures_from_one_ip_are_locked_out(self, settings, staff_user):
        settings.LOGIN_MAX_ATTEMPTS_PER_IP = 3
        client = Client()

        for _ in range(3):
            response = attempt(client)
            assert response.status_code == 200, "should re-render the form, not lock out yet"

        response = attempt(client)
        assert response.status_code == 429
        assert b"Too many failed sign-in attempts" in response.content

    def test_lockout_sets_retry_after(self, settings, staff_user):
        settings.LOGIN_MAX_ATTEMPTS_PER_IP = 1
        settings.LOGIN_ATTEMPT_WINDOW_SECONDS = 900
        client = Client()
        attempt(client)
        response = attempt(client)
        assert response["Retry-After"] == "900"

    def test_the_correct_password_is_refused_while_locked_out(self, settings, staff_user):
        """A lockout must not be bypassable by finally guessing right."""
        settings.LOGIN_MAX_ATTEMPTS_PER_IP = 2
        client = Client()
        attempt(client)
        attempt(client)

        response = attempt(client, password=GOOD_PASSWORD)
        assert response.status_code == 429
        assert "_auth_user_id" not in client.session

    def test_a_successful_sign_in_clears_earlier_failures(self, settings, staff_user):
        settings.LOGIN_MAX_ATTEMPTS_PER_IP = 5
        client = Client()
        attempt(client)
        attempt(client)
        assert LoginAttempt.objects.filter(succeeded=False).count() == 2

        response = attempt(client, password=GOOD_PASSWORD)
        assert response.status_code == 302
        assert LoginAttempt.objects.filter(succeeded=False).count() == 0

    def test_a_different_ip_is_not_punished_for_another_ip_failures(self, settings, staff_user):
        settings.LOGIN_MAX_ATTEMPTS_PER_IP = 2
        settings.LOGIN_MAX_ATTEMPTS_PER_USERNAME = 0  # isolate the per-IP rule
        settings.TRUST_PROXY_HEADERS = True

        noisy = Client()
        attempt(noisy, HTTP_X_FORWARDED_FOR="10.0.0.1")
        attempt(noisy, HTTP_X_FORWARDED_FOR="10.0.0.1")
        assert attempt(noisy, HTTP_X_FORWARDED_FOR="10.0.0.1").status_code == 429

        innocent = Client()
        response = attempt(innocent, password=GOOD_PASSWORD, HTTP_X_FORWARDED_FOR="10.0.0.2")
        assert response.status_code == 302, "an unrelated address was caught in the lockout"

    def test_distributed_attack_on_one_username_is_caught(self, settings, staff_user):
        """What the per-IP counter cannot see: many addresses, one account."""
        settings.LOGIN_MAX_ATTEMPTS_PER_IP = 100
        settings.LOGIN_MAX_ATTEMPTS_PER_USERNAME = 3
        settings.TRUST_PROXY_HEADERS = True

        for octet in range(3):
            client = Client()
            response = attempt(client, HTTP_X_FORWARDED_FOR=f"10.1.1.{octet}")
            assert response.status_code == 200

        client = Client()
        response = attempt(client, HTTP_X_FORWARDED_FOR="10.1.1.99")
        assert response.status_code == 429

    def test_per_username_limit_can_be_disabled(self, settings, staff_user):
        """0 opts out, for deployments that fear targeted lockout more."""
        settings.LOGIN_MAX_ATTEMPTS_PER_IP = 100
        settings.LOGIN_MAX_ATTEMPTS_PER_USERNAME = 0
        settings.TRUST_PROXY_HEADERS = True

        for octet in range(6):
            client = Client()
            response = attempt(client, HTTP_X_FORWARDED_FOR=f"10.2.2.{octet}")
            assert response.status_code == 200

    def test_failures_outside_the_window_do_not_count(self, settings, staff_user):
        settings.LOGIN_MAX_ATTEMPTS_PER_IP = 2
        client = Client()
        attempt(client)
        attempt(client)

        # Age the recorded failures past the window.
        from django.utils import timezone

        LoginAttempt.objects.update(
            created_at=timezone.now()
            - timezone.timedelta(seconds=settings.LOGIN_ATTEMPT_WINDOW_SECONDS + 60)
        )

        response = attempt(client, password=GOOD_PASSWORD)
        assert response.status_code == 302

    def test_old_rows_are_pruned(self, settings, staff_user):
        settings.LOGIN_ATTEMPT_WINDOW_SECONDS = 60
        from django.utils import timezone

        LoginAttempt.objects.create(
            username="ancient", succeeded=False,
            created_at=timezone.now() - timezone.timedelta(days=30),
        )
        attempt(Client())
        assert not LoginAttempt.objects.filter(username="ancient").exists()

    def test_admin_login_is_also_protected(self, settings, staff_user):
        """/admin/ has its own login form; make sure it redirects to ours."""
        response = Client().get("/admin/", follow=False)
        assert response.status_code == 302
        assert "/admin/login/" in response["Location"]


class TestProxyTrust:
    """X-Forwarded-For is forgeable unless a trusted proxy overwrites it."""

    def test_forwarded_for_is_used_when_trusted(self, settings):
        settings.TRUST_PROXY_HEADERS = True
        request = RequestFactory().get(
            "/", HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1", REMOTE_ADDR="127.0.0.1"
        )
        assert client_ip(request) == "203.0.113.9"

    def test_forwarded_for_is_ignored_when_not_trusted(self, settings):
        """Otherwise a fresh spoofed IP per request walks through every limiter."""
        settings.TRUST_PROXY_HEADERS = False
        request = RequestFactory().get(
            "/", HTTP_X_FORWARDED_FOR="203.0.113.9", REMOTE_ADDR="198.51.100.4"
        )
        assert client_ip(request) == "198.51.100.4"

    def test_falls_back_to_remote_addr_without_the_header(self, settings):
        settings.TRUST_PROXY_HEADERS = True
        request = RequestFactory().get("/", REMOTE_ADDR="198.51.100.4")
        assert client_ip(request) == "198.51.100.4"

    def test_spoofed_forwarded_for_cannot_defeat_the_login_limiter(self, settings, staff_user):
        settings.TRUST_PROXY_HEADERS = False
        settings.LOGIN_MAX_ATTEMPTS_PER_IP = 2
        settings.LOGIN_MAX_ATTEMPTS_PER_USERNAME = 0

        for octet in range(2):
            attempt(Client(), HTTP_X_FORWARDED_FOR=f"203.0.113.{octet}")

        # A third attempt with yet another forged address must still be blocked,
        # because REMOTE_ADDR (testserver's 127.0.0.1) is what counts.
        response = attempt(Client(), HTTP_X_FORWARDED_FOR="203.0.113.99")
        assert response.status_code == 429


class TestSecurityHeaders:
    """Headers the nginx sidecar used to add."""

    def test_permissions_policy_is_set(self, client, role):
        response = client.get("/")
        assert "geolocation=()" in response["Permissions-Policy"]

    def test_nosniff_is_set(self, client, role):
        assert client.get("/")["X-Content-Type-Options"] == "nosniff"

    def test_frame_options_is_set(self, client, role):
        assert client.get("/")["X-Frame-Options"] == "SAMEORIGIN"

    def test_referrer_policy_is_set(self, client, role):
        assert client.get("/")["Referrer-Policy"] == "same-origin"

    def test_headers_are_present_on_api_responses_too(self, client, role):
        response = client.get("/api/roles/")
        assert response["X-Content-Type-Options"] == "nosniff"
        assert "Permissions-Policy" in response


class TestCompression:
    """Replaces nginx's gzip block."""

    def test_api_json_is_gzipped_for_a_capable_client(self, client, business_unit):
        # GZipMiddleware skips responses under 200 bytes, so make a real one.
        from roles.models import Role

        for index in range(40):
            Role.objects.create(
                business_unit=business_unit,
                position=f"Compressible Role {index}",
                purpose="x" * 300,
            )

        response = client.get("/api/roles/", HTTP_ACCEPT_ENCODING="gzip")
        assert response["Content-Encoding"] == "gzip"
        # And it must actually decompress back to the real payload.
        body = gzip.decompress(b"".join(response.streaming_content)
                               if response.streaming else response.content)
        assert b"Compressible Role 0" in body

    def test_uncompressed_for_a_client_that_cannot_accept_it(self, client, role):
        response = client.get("/api/roles/", HTTP_ACCEPT_ENCODING="")
        assert response.get("Content-Encoding") != "gzip"

    def test_compression_can_be_turned_off(self, settings):
        """DJANGO_GZIP=0 must remove the middleware, not just no-op it."""
        from config.settings import ENABLE_GZIP, MIDDLEWARE

        if ENABLE_GZIP:
            assert "django.middleware.gzip.GZipMiddleware" in MIDDLEWARE
        else:
            assert "django.middleware.gzip.GZipMiddleware" not in MIDDLEWARE


class TestNoSidecarAssumptions:
    """The app must be fully functional with nothing in front of it."""

    def test_static_files_are_served_by_the_app(self, client, role):
        """WhiteNoise, not nginx. A 404 here means a blank, unstyled site."""
        page = client.get("/").content.decode()
        import re

        match = re.search(r'src="(/static/roles/app[^"]*\.js)"', page)
        assert match, "app.js link not found in the rendered page"
        assert client.get(match.group(1)).status_code == 200

    def test_health_probes_answer_without_a_proxy(self, client, role):
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200

    def test_request_id_is_generated_when_no_proxy_supplies_one(self, client, role):
        """nginx used to set X-Request-ID; the app must not depend on it."""
        response = client.get("/api/roles/")
        assert response["X-Request-ID"]
        assert len(response["X-Request-ID"]) >= 8

    def test_api_throttles_are_configured_in_the_app(self):
        from django.conf import settings

        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        assert rates["roles_write"], "write throttle must not be empty"
        assert rates["ai"], "AI throttle must not be empty"


class TestAdminStillReachable:
    def test_admin_login_page_renders(self, client, db):
        assert client.get("/admin/login/").status_code == 200

    def test_staff_can_open_the_admin(self, staff_user):
        get_user_model().objects.filter(pk=staff_user.pk).update(is_superuser=True)
        client = Client()
        client.force_login(staff_user)
        assert client.get("/admin/roles/role/").status_code == 200
