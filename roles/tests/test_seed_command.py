"""Integration tests for the HTML -> SQLite seeding step."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from roles.models import BusinessUnit, Role, RoleRevision

pytestmark = [pytest.mark.django_db, pytest.mark.integration]


def seed(path, **options):
    out, err = StringIO(), StringIO()
    call_command("seed_roles", file=str(path), stdout=out, stderr=err, **options)
    return out.getvalue(), err.getvalue()


class TestSeedRoles:
    def test_first_run_loads_every_role(self, legacy_html):
        out, _ = seed(legacy_html)
        assert Role.objects.count() == 5
        assert "created=5" in out

    def test_business_units_are_created_once(self, legacy_html):
        seed(legacy_html)
        assert BusinessUnit.objects.count() == 4
        assert set(BusinessUnit.objects.values_list("name", flat=True)) == {
            "Internal Audit", "BLA", "CX", "Group Exco"
        }

    def test_business_unit_display_order_follows_the_html(self, legacy_html):
        seed(legacy_html)
        names = list(
            BusinessUnit.objects.order_by("display_order").values_list("name", flat=True)
        )
        assert names == ["Internal Audit", "BLA", "CX", "Group Exco"]

    def test_field_values_are_loaded_faithfully(self, legacy_html):
        seed(legacy_html)
        role = Role.objects.get(position="Claims Assistant")
        assert role.business_unit.name == "BLA"
        assert role.band == "Band 9"
        assert float(role.band_numeric) == 9.0
        assert role.level == "Direct Contributor"
        assert "medical claims" in role.technical_competencies
        assert role.source == "seed"

    def test_values_with_quotes_braces_and_commas_survive(self, legacy_html):
        seed(legacy_html)
        role = Role.objects.get(position__startswith="Marketing Executive")
        assert role.purpose == 'Coordinates campaigns, uses commas, braces {like this} and "quotes".'

    def test_running_twice_changes_nothing(self, legacy_html):
        seed(legacy_html)
        first_ids = set(Role.objects.values_list("pk", flat=True))
        out, _ = seed(legacy_html)
        assert Role.objects.count() == 5
        assert set(Role.objects.values_list("pk", flat=True)) == first_ids
        assert "created=0" in out
        assert "unchanged=5" in out

    def test_second_run_writes_no_audit_noise(self, legacy_html):
        seed(legacy_html)
        before = RoleRevision.objects.count()
        seed(legacy_html)
        assert RoleRevision.objects.count() == before

    def test_changed_html_updates_the_row(self, legacy_html):
        seed(legacy_html)
        text = legacy_html.read_text(encoding="utf-8")
        legacy_html.write_text(
            text.replace("Conducts internal audits.", "Conducts risk-based internal audits."),
            encoding="utf-8",
        )
        out, _ = seed(legacy_html)
        assert "updated=1" in out
        assert Role.objects.get(position="Internal Auditor").purpose == (
            "Conducts risk-based internal audits."
        )

    def test_edits_made_in_the_app_are_not_clobbered_by_quiet_if_seeded(self, legacy_html):
        seed(legacy_html)
        role = Role.objects.get(position="Internal Auditor")
        role.purpose = "Edited by HR in the Manage tab"
        role.save()

        seed(legacy_html, quiet_if_seeded=True)

        role.refresh_from_db()
        assert role.purpose == "Edited by HR in the Manage tab"

    def test_dry_run_writes_nothing(self, legacy_html):
        out, _ = seed(legacy_html, dry_run=True)
        assert Role.objects.count() == 0
        assert BusinessUnit.objects.count() == 0
        assert "would create" in out

    def test_force_rewrites_unchanged_rows(self, legacy_html):
        seed(legacy_html)
        out, _ = seed(legacy_html, force=True)
        assert "updated=5" in out

    def test_prune_deactivates_roles_removed_from_the_html(self, legacy_html):
        seed(legacy_html)
        text = legacy_html.read_text(encoding="utf-8")
        start = text.index('  {bu:"Internal Audit",pos:"Internal Auditor"')
        end = text.index('  {bu:"BLA"')
        legacy_html.write_text(text[:start] + text[end:], encoding="utf-8")

        seed(legacy_html, prune=True)

        assert Role.objects.get(position="Internal Auditor").is_active is False
        assert Role.objects.active().count() == 4

    def test_prune_leaves_manually_created_roles_alone(self, legacy_html):
        seed(legacy_html)
        bu = BusinessUnit.objects.get(name="CX")
        manual = Role.objects.create(business_unit=bu, position="Hand-added role", source="manual")

        seed(legacy_html, prune=True)

        manual.refresh_from_db()
        assert manual.is_active is True

    def test_missing_file_fails_loudly(self, tmp_path):
        with pytest.raises(CommandError) as excinfo:
            seed(tmp_path / "absent.html")
        assert "SEED-001" in str(excinfo.value)

    def test_file_without_roles_array_fails_loudly(self, tmp_path):
        path = tmp_path / "bad.html"
        path.write_text("<html>no data</html>", encoding="utf-8")
        with pytest.raises(CommandError) as excinfo:
            seed(path)
        assert "SEED-002" in str(excinfo.value)

    def test_quiet_if_seeded_skips_when_data_exists(self, legacy_html, role):
        out, _ = seed(legacy_html, quiet_if_seeded=True)
        assert "already holds" in out
        assert Role.objects.count() == 1

    def test_seeding_the_real_file_loads_the_whole_library(self):
        from django.conf import settings

        if not settings.LEGACY_HTML_PATH.is_file():
            pytest.skip("production HTML not present in this checkout")
        out, _ = seed(settings.LEGACY_HTML_PATH)
        assert Role.objects.count() > 250
        assert "failed=0" in out
        # Every seeded role must be reachable through the public queryset.
        assert Role.objects.active().count() == Role.objects.count()


class TestExportRoles:
    def test_json_export_round_trips(self, legacy_html):
        seed(legacy_html)
        out = StringIO()
        call_command("export_roles", format="json", stdout=out, stderr=StringIO())
        import json

        rows = json.loads(out.getvalue())
        assert len(rows) == 5
        assert {row["pos"] for row in rows} >= {"Internal Auditor", "Claims Assistant"}

    def test_csv_export_has_a_header_row(self, legacy_html):
        seed(legacy_html)
        out = StringIO()
        call_command("export_roles", format="csv", stdout=out, stderr=StringIO())
        lines = out.getvalue().strip().splitlines()
        assert lines[0].startswith("bu,pos,band")
        assert len(lines) == 6

    def test_js_export_is_shaped_like_the_legacy_array(self, legacy_html):
        seed(legacy_html)
        out = StringIO()
        call_command("export_roles", format="js", stdout=out, stderr=StringIO())
        text = out.getvalue()
        assert text.startswith("const ROLES = [")
        assert text.rstrip().endswith("];")


class TestBootstrapAdmin:
    def test_skips_without_env(self, monkeypatch):
        monkeypatch.delenv("DJANGO_SUPERUSER_USERNAME", raising=False)
        monkeypatch.delenv("DJANGO_SUPERUSER_PASSWORD", raising=False)
        out = StringIO()
        call_command("bootstrap_admin", stdout=out)
        assert "skipping" in out.getvalue()

    def test_creates_the_superuser(self, monkeypatch):
        from django.contrib.auth import get_user_model

        monkeypatch.setenv("DJANGO_SUPERUSER_USERNAME", "hradmin")
        monkeypatch.setenv("DJANGO_SUPERUSER_EMAIL", "hr@example.com")
        monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "correct-horse-battery-staple")
        call_command("bootstrap_admin", stdout=StringIO())

        user = get_user_model().objects.get(username="hradmin")
        assert user.is_superuser and user.is_staff
        assert user.check_password("correct-horse-battery-staple")

    def test_refuses_a_weak_password(self, monkeypatch):
        monkeypatch.setenv("DJANGO_SUPERUSER_USERNAME", "hradmin")
        monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "password")
        with pytest.raises(CommandError) as excinfo:
            call_command("bootstrap_admin", stdout=StringIO())
        assert "BOOT-001" in str(excinfo.value)

    def test_is_idempotent(self, monkeypatch):
        from django.contrib.auth import get_user_model

        monkeypatch.setenv("DJANGO_SUPERUSER_USERNAME", "hradmin")
        monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "correct-horse-battery-staple")
        call_command("bootstrap_admin", stdout=StringIO())
        call_command("bootstrap_admin", skip_if_any_exists=True, stdout=StringIO())
        assert get_user_model().objects.filter(username="hradmin").count() == 1
