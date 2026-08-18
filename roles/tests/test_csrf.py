"""
Regression tests for the login CSRF failure.

Symptom reported from the droplet: submitting the sign-in form returned
"Forbidden (403) — CSRF verification failed. Request aborted."

Cause: nginx forwarded `Host` via `$host`, which drops the port. A browser on
http://example.com:6519 sends `Origin: http://example.com:6519`; Django
reconstructs its own origin from `Host` as `http://example.com`; Django >= 4.0
compares the two on every unsafe request and rejects the mismatch.

The original suite missed it because it drove gunicorn directly (no proxy) with
Django's test client, which sends no Origin header. These tests use
`enforce_csrf_checks=True` and set Origin explicitly, so the proxy's behaviour
is reproduced in-process.
"""

from __future__ import annotations

import pytest
from django.test import Client

from config.settings import derive_csrf_trusted_origins

pytestmark = [pytest.mark.django_db, pytest.mark.integration]

LOGIN_URL = "/accounts/login/"
PASSWORD = "a-long-enough-passphrase-42"


def csrf_client() -> Client:
    """A client that enforces CSRF, the way a real browser session does."""
    return Client(enforce_csrf_checks=True)


def login_token(client: Client, server_name: str) -> str:
    response = client.get(LOGIN_URL, SERVER_NAME=server_name)
    assert response.status_code == 200
    return response.cookies["csrftoken"].value


class TestDeriveCsrfTrustedOrigins:
    """Pure unit tests for the origin derivation — no database, no requests."""

    def test_includes_the_bare_host_and_the_published_port(self):
        origins = derive_csrf_trusted_origins(["roles.example.com"], "http", "6519")
        assert origins == ["http://roles.example.com", "http://roles.example.com:6519"]

    def test_uses_https_when_behind_tls(self):
        origins = derive_csrf_trusted_origins(["roles.example.com"], "https", "")
        assert origins == ["https://roles.example.com"]

    def test_handles_several_hosts(self):
        origins = derive_csrf_trusted_origins(["a.example.com", "1.2.3.4"], "http", "6519")
        assert "http://a.example.com:6519" in origins
        assert "http://1.2.3.4:6519" in origins

    def test_translates_a_wildcard_host(self):
        origins = derive_csrf_trusted_origins([".example.com"], "https", "")
        assert origins == ["https://*.example.com"]

    def test_never_emits_a_bare_wildcard(self):
        """'*' in ALLOWED_HOSTS must not become a trust-everything origin."""
        origins = derive_csrf_trusted_origins(["*", "safe.example.com"], "http", "")
        assert origins == ["http://safe.example.com"]
        assert not any(origin.endswith("://*") for origin in origins)

    def test_explicit_entries_come_first_and_are_kept(self):
        origins = derive_csrf_trusted_origins(
            ["roles.example.com"], "http", "6519", explicit=["https://cdn.example.com"]
        )
        assert origins[0] == "https://cdn.example.com"
        assert "http://roles.example.com:6519" in origins

    def test_does_not_duplicate_an_explicit_entry(self):
        origins = derive_csrf_trusted_origins(
            ["roles.example.com"], "http", "6519", explicit=["http://roles.example.com:6519"]
        )
        assert origins.count("http://roles.example.com:6519") == 1

    def test_blank_and_whitespace_hosts_are_ignored(self):
        assert derive_csrf_trusted_origins(["", "   "], "http", "6519") == []


class TestLoginCsrf:
    def test_login_succeeds_when_the_origin_carries_the_published_port(
        self, settings, staff_user
    ):
        """The exact request the browser makes through nginx on :6519."""
        settings.ALLOWED_HOSTS = ["roles.example.com"]
        settings.CSRF_TRUSTED_ORIGINS = derive_csrf_trusted_origins(
            ["roles.example.com"], "http", "6519"
        )
        client = csrf_client()
        token = login_token(client, "roles.example.com")

        response = client.post(
            LOGIN_URL,
            {"csrfmiddlewaretoken": token, "username": "editor", "password": PASSWORD},
            SERVER_NAME="roles.example.com",
            HTTP_ORIGIN="http://roles.example.com:6519",
            HTTP_REFERER="http://roles.example.com:6519/accounts/login/",
        )
        assert response.status_code == 302, "sign-in was rejected by the CSRF check"
        assert response.headers["Location"].startswith("/")

    def test_login_succeeds_on_the_default_port(self, settings, staff_user):
        settings.ALLOWED_HOSTS = ["roles.example.com"]
        settings.CSRF_TRUSTED_ORIGINS = derive_csrf_trusted_origins(
            ["roles.example.com"], "http", "6519"
        )
        client = csrf_client()
        token = login_token(client, "roles.example.com")

        response = client.post(
            LOGIN_URL,
            {"csrfmiddlewaretoken": token, "username": "editor", "password": PASSWORD},
            SERVER_NAME="roles.example.com",
            HTTP_ORIGIN="http://roles.example.com",
        )
        assert response.status_code == 302

    def test_login_is_still_rejected_from_a_foreign_origin(self, settings, staff_user):
        """The fix must not turn into 'trust any origin'."""
        settings.ALLOWED_HOSTS = ["roles.example.com"]
        settings.CSRF_TRUSTED_ORIGINS = derive_csrf_trusted_origins(
            ["roles.example.com"], "http", "6519"
        )
        client = csrf_client()
        token = login_token(client, "roles.example.com")

        response = client.post(
            LOGIN_URL,
            {"csrfmiddlewaretoken": token, "username": "editor", "password": PASSWORD},
            SERVER_NAME="roles.example.com",
            HTTP_ORIGIN="http://attacker.example.net",
        )
        assert response.status_code == 403

    def test_login_is_rejected_without_a_token(self, settings, staff_user):
        settings.ALLOWED_HOSTS = ["roles.example.com"]
        client = csrf_client()
        login_token(client, "roles.example.com")

        response = client.post(
            LOGIN_URL,
            {"username": "editor", "password": PASSWORD},
            SERVER_NAME="roles.example.com",
            HTTP_ORIGIN="http://roles.example.com:6519",
        )
        assert response.status_code == 403


class TestApiCsrf:
    """The Manage tab posts JSON with an X-CSRFToken header; same origin rules."""

    def test_api_write_succeeds_with_the_header_and_a_ported_origin(
        self, settings, staff_user
    ):
        settings.ALLOWED_HOSTS = ["roles.example.com"]
        settings.CSRF_TRUSTED_ORIGINS = derive_csrf_trusted_origins(
            ["roles.example.com"], "http", "6519"
        )
        client = csrf_client()
        client.force_login(staff_user)
        token = client.get("/", SERVER_NAME="roles.example.com").cookies["csrftoken"].value

        response = client.post(
            "/api/roles/",
            data='{"business_unit_name":"CX","position":"CSRF Test Role"}',
            content_type="application/json",
            SERVER_NAME="roles.example.com",
            HTTP_ORIGIN="http://roles.example.com:6519",
            HTTP_X_CSRFTOKEN=token,
        )
        assert response.status_code == 201, response.content

    def test_api_write_without_the_header_is_rejected(self, settings, staff_user):
        settings.ALLOWED_HOSTS = ["roles.example.com"]
        settings.CSRF_TRUSTED_ORIGINS = derive_csrf_trusted_origins(
            ["roles.example.com"], "http", "6519"
        )
        client = csrf_client()
        client.force_login(staff_user)

        response = client.post(
            "/api/roles/",
            data='{"business_unit_name":"CX","position":"No Token Role"}',
            content_type="application/json",
            SERVER_NAME="roles.example.com",
            HTTP_ORIGIN="http://roles.example.com:6519",
        )
        assert response.status_code == 403

    def test_the_page_sets_a_readable_csrf_cookie(self, client, role):
        """CSRF_COOKIE_HTTPONLY must stay False or the Manage tab cannot read it."""
        response = client.get("/")
        assert "csrftoken" in response.cookies
        assert response.cookies["csrftoken"]["httponly"] == ""
