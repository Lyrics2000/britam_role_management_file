"""Unit tests for the data layer."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import IntegrityError

from roles.models import BusinessUnit, Role, RoleRevision, parse_band_numeric

pytestmark = pytest.mark.django_db


class TestParseBandNumeric:
    @pytest.mark.parametrize(
        "label,expected",
        [
            ("Band 3", Decimal("3")),
            ("Band 6.2", Decimal("6.2")),
            ("band 9", Decimal("9")),
            ("Band 10", Decimal("10")),
            ("Executive", None),
            ("", None),
            (None, None),
            ("Band", None),
        ],
    )
    def test_parses(self, label, expected):
        assert parse_band_numeric(label) == expected


class TestBusinessUnit:
    def test_slug_is_generated(self):
        bu = BusinessUnit.objects.create(name="Foundation & IR")
        assert bu.slug == "foundation-ir"

    def test_slug_collisions_are_resolved(self):
        first = BusinessUnit.objects.create(name="BLA (KE)")
        second = BusinessUnit.objects.create(name="BLA KE")
        assert first.slug != second.slug
        assert second.slug.endswith("-2")

    def test_name_is_unique(self, business_unit):
        with pytest.raises(IntegrityError):
            BusinessUnit.objects.create(name="Internal Audit")


class TestRole:
    def test_band_numeric_is_derived_on_save(self, business_unit):
        role = Role.objects.create(
            business_unit=business_unit, position="Analyst", band="Band 6.2"
        )
        assert role.band_numeric == Decimal("6.2")

    def test_band_numeric_updates_when_band_changes(self, role):
        role.band = "Band 5"
        role.save()
        role.refresh_from_db()
        assert role.band_numeric == Decimal("5")

    def test_band_without_number_leaves_band_numeric_null(self, business_unit):
        role = Role.objects.create(
            business_unit=business_unit, position="MD", band="Executive"
        )
        assert role.band_numeric is None

    def test_position_is_unique_within_a_business_unit(self, role, business_unit):
        with pytest.raises(IntegrityError):
            Role.objects.create(business_unit=business_unit, position=role.position)

    def test_same_position_allowed_in_a_different_business_unit(self, role):
        other = BusinessUnit.objects.create(name="BLA")
        duplicate = Role.objects.create(business_unit=other, position=role.position)
        assert duplicate.pk != role.pk

    def test_content_hash_is_stable_for_identical_content(self, role):
        original = role.content_hash
        role.save()
        role.refresh_from_db()
        assert role.content_hash == original

    def test_content_hash_changes_when_content_changes(self, role):
        original = role.content_hash
        role.purpose = "Something different"
        role.save()
        assert role.content_hash != original

    def test_focus_list_splits_on_commas_and_pipes(self, role):
        role.focus_areas = "Governance, Risk Management | ESG"
        assert role.focus_list == ["Governance", "Risk Management", "ESG"]

    def test_competency_lists_split_on_pipes_only(self, role):
        role.technical_competencies = "Audit, standards | IFRS knowledge"
        assert role.technical_list == ["Audit, standards", "IFRS knowledge"]

    def test_empty_competencies_produce_empty_list(self, role):
        role.leadership_competencies = ""
        assert role.leadership_list == []

    def test_search_matches_position(self, role):
        assert Role.objects.search("internal audit").count() == 1

    def test_search_matches_purpose(self, role):
        assert Role.objects.search("audit services").count() == 1

    def test_search_is_case_insensitive(self, role):
        assert Role.objects.search("HEAD OF INTERNAL").count() == 1

    def test_search_with_blank_term_returns_everything(self, role):
        assert Role.objects.search("").count() == Role.objects.count()

    def test_active_filters_hidden_roles(self, role):
        role.is_active = False
        role.save()
        assert Role.objects.active().count() == 0
        assert Role.objects.count() == 1


class TestAuditTrail:
    def test_creation_writes_a_revision(self, business_unit):
        Role.objects.create(business_unit=business_unit, position="New role")
        revision = RoleRevision.objects.get()
        assert revision.action == RoleRevision.Action.CREATE
        assert "New role" in revision.role_label

    def test_seeded_creation_is_marked_as_seed(self, business_unit):
        Role.objects.create(business_unit=business_unit, position="Seeded", source="seed")
        assert RoleRevision.objects.get().action == RoleRevision.Action.SEED

    def test_update_records_only_changed_fields(self, role):
        RoleRevision.objects.all().delete()
        role.band = "Band 2"
        role.save()
        revision = RoleRevision.objects.get()
        assert revision.action == RoleRevision.Action.UPDATE
        assert set(revision.changes) == {"band", "band_numeric"}
        assert revision.changes["band"]["from"] == "Band 3"
        assert revision.changes["band"]["to"] == "Band 2"

    def test_no_op_save_writes_no_revision(self, role):
        RoleRevision.objects.all().delete()
        role.save()
        assert RoleRevision.objects.count() == 0

    def test_delete_writes_a_revision_that_survives_the_role(self, role):
        label = f"{role.business_unit.name} / {role.position}"
        role.delete()
        revision = RoleRevision.objects.filter(action=RoleRevision.Action.DELETE).get()
        assert revision.role is None
        assert revision.role_label == label
        assert "deleted" in revision.changes

    def test_actor_is_recorded_when_supplied(self, role, staff_user):
        RoleRevision.objects.all().delete()
        role._audit_actor = staff_user
        role.level = "Leader of Leaders"
        role.save()
        assert RoleRevision.objects.get().actor_label == "editor"
