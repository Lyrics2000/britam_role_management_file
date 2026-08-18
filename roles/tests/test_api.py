"""Integration tests for the HTTP API, permissions and the page itself."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from roles.models import AIRequestLog, BusinessUnit, Role, RoleRevision

pytestmark = [pytest.mark.django_db, pytest.mark.integration]

API = "/api/roles/"


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def put_json(client, url, payload):
    return client.put(url, data=json.dumps(payload), content_type="application/json")


class TestReadAccess:
    def test_anonymous_can_list_roles(self, client, role):
        response = client.get(API)
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_list_exposes_legacy_field_aliases(self, client, role):
        record = client.get(API).json()["results"][0]
        for key in ("pos", "bu", "band", "bandN", "exp", "quals", "desc",
                    "focus", "kras", "reports", "techcomp", "leadcomp"):
            assert key in record, f"missing legacy alias {key}"
        assert record["pos"] == role.position
        assert record["bu"] == role.business_unit.name

    def test_hidden_roles_are_invisible_to_anonymous_callers(self, client, role):
        role.is_active = False
        role.save()
        assert client.get(API).json()["count"] == 0

    def test_staff_can_request_hidden_roles(self, staff_client, role):
        role.is_active = False
        role.save()
        response = staff_client.get(API + "?include_inactive=1&active=")
        assert response.json()["count"] == 1

    def test_search_filter(self, client, role):
        assert client.get(API + "?q=audit").json()["count"] == 1
        assert client.get(API + "?q=zzzz").json()["count"] == 0

    def test_bu_and_band_filters(self, client, role):
        assert client.get(API + "?bu=Internal Audit").json()["count"] == 1
        assert client.get(API + "?band=Band 3").json()["count"] == 1
        assert client.get(API + "?band=Band 9").json()["count"] == 0

    def test_band_range_filter(self, client, role):
        assert client.get(API + "?band_min=2&band_max=4").json()["count"] == 1
        assert client.get(API + "?band_min=5").json()["count"] == 0

    def test_meta_endpoint(self, client, role):
        payload = client.get("/api/meta/").json()
        assert payload["counts"]["roles"] == 1
        assert payload["counts"]["business_units"] == 1
        assert payload["bands"] == ["Band 3"]
        assert payload["levels"] == ["Change Leader"]
        assert payload["business_units"][0]["name"] == "Internal Audit"

    def test_meta_omits_business_units_with_no_visible_roles(self, client, role):
        """An empty unit must not leave a dead tab in the BU filter strip."""
        BusinessUnit.objects.create(name="Decommissioned BU")
        payload = client.get("/api/meta/").json()
        names = [bu["name"] for bu in payload["business_units"]]
        assert names == ["Internal Audit"]
        assert payload["counts"]["business_units"] == 1

    def test_meta_omits_a_unit_whose_only_role_is_hidden(self, client, role):
        role.is_active = False
        role.save()
        payload = client.get("/api/meta/").json()
        assert payload["business_units"] == []
        assert payload["counts"]["business_units"] == 0

    def test_meta_sorts_bands_numerically_not_alphabetically(self, client, business_unit):
        for band in ("Band 10", "Band 2", "Band 6.2"):
            Role.objects.create(business_unit=business_unit, position=f"R {band}", band=band)
        assert client.get("/api/meta/").json()["bands"] == ["Band 2", "Band 6.2", "Band 10"]


class TestWritePermissions:
    PAYLOAD = {"business_unit_name": "New BU", "position": "New Role", "band": "Band 5"}

    def test_anonymous_cannot_create(self, client, db):
        response = post_json(client, API, self.PAYLOAD)
        assert response.status_code in (401, 403)
        assert Role.objects.count() == 0

    def test_non_staff_user_cannot_create(self, client, plain_user):
        client.force_login(plain_user)
        response = post_json(client, API, self.PAYLOAD)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "AUTH-FORBIDDEN"

    def test_anonymous_cannot_update(self, client, role):
        response = put_json(client, f"{API}{role.pk}/", {"position": "Hijacked"})
        assert response.status_code in (401, 403)
        role.refresh_from_db()
        assert role.position != "Hijacked"

    def test_anonymous_cannot_delete(self, client, role):
        assert client.delete(f"{API}{role.pk}/").status_code in (401, 403)
        assert Role.objects.count() == 1

    def test_staff_can_create(self, staff_client, db):
        response = post_json(staff_client, API, self.PAYLOAD)
        assert response.status_code == 201, response.content
        assert Role.objects.count() == 1

    def test_history_endpoint_is_staff_only(self, client, staff_client, role):
        assert client.get(f"{API}{role.pk}/history/").status_code in (401, 403)
        assert staff_client.get(f"{API}{role.pk}/history/").status_code == 200


class TestCreate:
    def test_creates_the_business_unit_on_the_fly(self, staff_client, db):
        response = post_json(staff_client, API, {
            "business_unit_name": "Group Treasury", "position": "Treasury Analyst",
        })
        assert response.status_code == 201
        assert BusinessUnit.objects.filter(name="Group Treasury").exists()

    def test_reuses_an_existing_business_unit_case_insensitively(self, staff_client, business_unit):
        post_json(staff_client, API, {
            "business_unit_name": "internal audit", "position": "Audit Analyst",
        })
        assert BusinessUnit.objects.count() == 1

    def test_band_numeric_is_derived(self, staff_client, db):
        response = post_json(staff_client, API, {
            "business_unit_name": "CX", "position": "Analyst", "band": "Band 6.2",
        })
        assert float(response.json()["bandN"]) == 6.2

    def test_duplicate_position_in_same_bu_is_rejected(self, staff_client, role):
        response = post_json(staff_client, API, {
            "business_unit_name": role.business_unit.name, "position": role.position,
        })
        assert response.status_code == 400
        assert "position" in response.json()["error"]["details"]

    def test_missing_position_is_rejected(self, staff_client, db):
        response = post_json(staff_client, API, {"business_unit_name": "CX"})
        assert response.status_code == 400
        assert "position" in response.json()["error"]["details"]

    def test_missing_business_unit_is_rejected(self, staff_client, db):
        response = post_json(staff_client, API, {"position": "Orphan role"})
        assert response.status_code == 400

    def test_null_bytes_are_rejected_outright(self, staff_client, db):
        """DRF's ProhibitNullCharactersValidator refuses these before we see them."""
        response = post_json(staff_client, API, {
            "business_unit_name": "CX", "position": "Clean\x00Title",
        })
        assert response.status_code == 400
        assert Role.objects.count() == 0

    def test_other_control_characters_are_stripped(self, staff_client, db):
        response = post_json(staff_client, API, {
            "business_unit_name": "CX", "position": "Clean\x07Title", "purpose": "a\x0bb",
        })
        assert response.status_code == 201, response.content
        role = Role.objects.get()
        assert role.position == "CleanTitle"
        assert role.purpose == "ab"

    def test_oversized_text_is_rejected(self, staff_client, db):
        response = post_json(staff_client, API, {
            "business_unit_name": "CX", "position": "Big", "purpose": "x" * 8001,
        })
        assert response.status_code == 400
        assert "purpose" in response.json()["error"]["details"]

    def test_invalid_band_label_is_rejected(self, staff_client, db):
        response = post_json(staff_client, API, {
            "business_unit_name": "CX", "position": "Weird band", "band": "<script>",
        })
        assert response.status_code == 400
        assert "band" in response.json()["error"]["details"]

    def test_html_in_a_text_field_is_stored_verbatim_not_executed(self, staff_client, client, db):
        """Escaping is the renderer's job; storage must not silently mangle data."""
        payload = {"business_unit_name": "CX", "position": "XSS probe",
                   "purpose": '<img src=x onerror="alert(1)">'}
        assert post_json(staff_client, API, payload).status_code == 201
        stored = client.get(API).json()["results"][0]["purpose"]
        assert stored == '<img src=x onerror="alert(1)">'

    def test_creation_records_the_actor(self, staff_client, staff_user, db):
        post_json(staff_client, API, {"business_unit_name": "CX", "position": "Tracked"})
        role = Role.objects.get()
        assert role.created_by == staff_user
        assert role.source == "manual"


class TestUpdateAndDelete:
    def test_staff_can_update(self, staff_client, role):
        response = put_json(staff_client, f"{API}{role.pk}/", {
            "business_unit_name": role.business_unit.name,
            "position": role.position,
            "band": "Band 2",
            "purpose": "Revised purpose",
        })
        assert response.status_code == 200, response.content
        role.refresh_from_db()
        assert role.band == "Band 2"
        assert role.purpose == "Revised purpose"

    def test_update_writes_an_audit_revision(self, staff_client, role):
        RoleRevision.objects.all().delete()
        put_json(staff_client, f"{API}{role.pk}/", {
            "business_unit_name": role.business_unit.name,
            "position": role.position, "band": "Band 1",
        })
        revision = RoleRevision.objects.filter(action="update").first()
        assert revision is not None
        assert revision.changes["band"]["to"] == "Band 1"

    def test_hiding_a_role_removes_it_from_the_public_list(self, staff_client, client, role):
        put_json(staff_client, f"{API}{role.pk}/", {
            "business_unit_name": role.business_unit.name,
            "position": role.position, "is_active": False,
        })
        assert client.get(API).json()["count"] == 0

    def test_staff_can_delete(self, staff_client, role):
        assert staff_client.delete(f"{API}{role.pk}/").status_code == 204
        assert Role.objects.count() == 0

    def test_delete_leaves_an_audit_trail(self, staff_client, role):
        staff_client.delete(f"{API}{role.pk}/")
        assert RoleRevision.objects.filter(action="delete").count() == 1

    def test_deleting_a_missing_role_returns_a_coded_404(self, staff_client, db):
        response = staff_client.delete(f"{API}999999/")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RES-NOT-FOUND"


class TestBusinessUnitEndpoint:
    def test_list_includes_role_counts(self, client, role):
        payload = client.get("/api/business-units/").json()
        assert payload[0]["role_count"] == 1

    def test_cannot_delete_a_unit_that_still_has_roles(self, staff_client, role):
        response = staff_client.delete(f"/api/business-units/{role.business_unit.pk}/")
        assert response.status_code == 400
        assert BusinessUnit.objects.count() == 1

    def test_can_delete_an_empty_unit(self, staff_client, db):
        bu = BusinessUnit.objects.create(name="Empty")
        assert staff_client.delete(f"/api/business-units/{bu.pk}/").status_code == 204


class TestErrorEnvelope:
    def test_errors_carry_code_message_and_request_id(self, client, db):
        response = client.get(f"{API}424242/")
        body = response.json()["error"]
        assert body["code"] == "RES-NOT-FOUND"
        assert body["message"]
        assert body["request_id"]

    def test_response_carries_the_request_id_header(self, client, db):
        response = client.get(API)
        assert response["X-Request-ID"]

    def test_inbound_request_id_is_propagated(self, client, db):
        response = client.get(API, HTTP_X_REQUEST_ID="abc-123")
        assert response["X-Request-ID"] == "abc-123"

    def test_malformed_inbound_request_id_is_replaced(self, client, db):
        response = client.get(API, HTTP_X_REQUEST_ID="bad id\nwith newline")
        assert "\n" not in response["X-Request-ID"]
        assert response["X-Request-ID"] != "bad id\nwith newline"


class TestHealthProbes:
    def test_healthz_is_ok_even_with_an_empty_database(self, client, db):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_readyz_reports_not_ready_when_unseeded(self, client, db):
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["checks"]["seed"].startswith("empty")

    def test_readyz_is_ready_once_seeded(self, client, role):
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["checks"]["roles"] == 1


class TestPage:
    def test_page_renders(self, client, role):
        response = client.get("/")
        assert response.status_code == 200
        assert b"Britam Group Role Library" in response.content

    def test_page_no_longer_embeds_the_data(self, client, role):
        content = client.get("/").content
        assert b"const ROLES = [" not in content

    def test_page_never_leaks_the_api_key(self, client, role, settings):
        settings.ANTHROPIC_API_KEY = "sk-ant-secret-value"
        content = client.get("/").content
        assert b"sk-ant-secret-value" not in content

    def test_anonymous_visitor_gets_canedit_false(self, client, role):
        content = client.get("/").content.decode()
        assert '"canEdit": false' in content or '"canEdit":false' in content

    def test_staff_visitor_gets_canedit_true(self, staff_client, role):
        content = staff_client.get("/").content.decode()
        assert '"canEdit": true' in content or '"canEdit":true' in content

    def test_login_page_renders(self, client, db):
        assert client.get("/accounts/login/").status_code == 200


class TestAIProxy:
    def test_returns_503_when_no_key_is_configured(self, client, settings, db):
        settings.ANTHROPIC_API_KEY = ""
        response = post_json(client, "/api/ai/", {"question": "What is Band 3?"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AI-DISABLED"

    def test_rejects_a_too_short_question(self, client, settings, db):
        settings.ANTHROPIC_API_KEY = "sk-test"
        response = post_json(client, "/api/ai/", {"question": "a"})
        assert response.status_code == 400

    def test_rejects_an_oversized_question(self, client, settings, db):
        settings.ANTHROPIC_API_KEY = "sk-test"
        response = post_json(client, "/api/ai/", {"question": "x" * 1001})
        assert response.status_code == 400

    @mock.patch("roles.views.requests.post")
    def test_happy_path_returns_only_the_answer_text(self, mock_post, client, settings, role):
        settings.ANTHROPIC_API_KEY = "sk-ant-secret"
        mock_post.return_value = mock.Mock(
            status_code=200,
            json=lambda: {"content": [{"type": "text", "text": "Band 3 is a Change Leader band."}]},
        )
        response = post_json(client, "/api/ai/", {"question": "What is Band 3?"})
        assert response.status_code == 200
        assert response.json()["answer"] == "Band 3 is a Change Leader band."
        assert b"sk-ant-secret" not in response.content

    @mock.patch("roles.views.requests.post")
    def test_key_is_sent_upstream_but_never_downstream(self, mock_post, client, settings, role):
        settings.ANTHROPIC_API_KEY = "sk-ant-secret"
        mock_post.return_value = mock.Mock(
            status_code=200, json=lambda: {"content": [{"text": "ok"}]}
        )
        post_json(client, "/api/ai/", {"question": "Tell me about audit roles"})
        _args, kwargs = mock_post.call_args
        assert kwargs["headers"]["x-api-key"] == "sk-ant-secret"

    @mock.patch("roles.views.requests.post")
    def test_role_context_is_built_from_the_database(self, mock_post, client, settings, role):
        settings.ANTHROPIC_API_KEY = "sk-test"
        mock_post.return_value = mock.Mock(
            status_code=200, json=lambda: {"content": [{"text": "ok"}]}
        )
        post_json(client, "/api/ai/", {"question": "Who leads internal audit?"})
        _args, kwargs = mock_post.call_args
        sent = kwargs["json"]["messages"][0]["content"]
        assert "Head of Internal Audit" in sent

    @mock.patch("roles.views.requests.post")
    def test_upstream_timeout_is_translated(self, mock_post, client, settings, role):
        import requests as requests_lib

        settings.ANTHROPIC_API_KEY = "sk-test"
        mock_post.side_effect = requests_lib.Timeout()
        response = post_json(client, "/api/ai/", {"question": "Anything at all?"})
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "AI-TIMEOUT"

    @mock.patch("roles.views.requests.post")
    def test_upstream_error_body_is_not_echoed_to_the_client(self, mock_post, client, settings, role):
        settings.ANTHROPIC_API_KEY = "sk-test"
        mock_post.return_value = mock.Mock(
            status_code=401, text='{"error":"invalid x-api-key sk-ant-leaked"}'
        )
        response = post_json(client, "/api/ai/", {"question": "Anything at all?"})
        assert response.status_code == 502
        assert b"sk-ant-leaked" not in response.content

    @mock.patch("roles.views.requests.post")
    def test_calls_are_logged(self, mock_post, client, settings, role):
        settings.ANTHROPIC_API_KEY = "sk-test"
        mock_post.return_value = mock.Mock(
            status_code=200, json=lambda: {"content": [{"text": "ok"}]}
        )
        post_json(client, "/api/ai/", {"question": "Logged question?"})
        entry = AIRequestLog.objects.get()
        assert entry.succeeded is True
        assert entry.question == "Logged question?"

    @mock.patch("roles.views.requests.post")
    def test_daily_budget_is_enforced(self, mock_post, client, settings, role):
        settings.ANTHROPIC_API_KEY = "sk-test"
        settings.AI_DAILY_BUDGET_REQUESTS = 1
        mock_post.return_value = mock.Mock(
            status_code=200, json=lambda: {"content": [{"text": "ok"}]}
        )
        assert post_json(client, "/api/ai/", {"question": "First question?"}).status_code == 200
        second = post_json(client, "/api/ai/", {"question": "Second question?"})
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "AI-BUDGET"
