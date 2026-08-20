"""
Job bands are visible to signed-in editors only (ADR-019).

Bands map onto the salary structure, so they are stripped server-side rather
than hidden in CSS or JavaScript — a cosmetic mask leaves the values one "View
source" or one /api/roles/ request away.

What the public *does* still get is `rank`: an opaque ordinal over the distinct
bands, 1 = most senior. Browse, Compare and the career-path builder all need to
order roles by seniority; rank lets them, while saying "fourth rung down"
rather than "Band 6.2".
"""

from __future__ import annotations

import pytest

from roles.models import Role

pytestmark = [pytest.mark.django_db, pytest.mark.integration]

API = "/api/roles/"
META = "/api/meta/"


# Titles deliberately contain no band text, so the raw-payload leak check
# below cannot pass or fail for the wrong reason.
LADDER = [
    ("Chief Operating Officer", "Band 1"),
    ("Head of Internal Audit", "Band 3"),
    ("Senior Analyst", "Band 6.2"),
    ("Claims Assistant", "Band 9"),
]


@pytest.fixture
def ladder(db, business_unit):
    """A band ladder from most to least senior, plus one role with no number."""
    for position, band in LADDER:
        Role.objects.create(business_unit=business_unit, position=position, band=band)
    Role.objects.create(
        business_unit=business_unit, position="Group Managing Director", band="Executive"
    )
    return Role.objects.all()


class TestAnonymousCannotSeeBands:
    def test_band_label_is_blank(self, client, ladder):
        for record in client.get(API).json()["results"]:
            assert record["band"] == "", f"leaked band {record['band']!r}"

    def test_numeric_band_is_null(self, client, ladder):
        for record in client.get(API).json()["results"]:
            assert record["band_numeric"] is None
            assert record["bandN"] is None

    def test_no_band_string_appears_anywhere_in_the_payload(self, client, ladder):
        """Belt and braces: grep the raw bytes, not just the parsed fields."""
        body = client.get(API + "?page_size=500").content
        for leak in (b"Band 1", b"Band 3", b"Band 6.2", b"Band 9", b"6.20", b"Executive"):
            assert leak not in body, f"{leak!r} leaked in the anonymous payload"

    def test_meta_exposes_no_band_vocabulary(self, client, ladder):
        payload = client.get(META).json()
        assert payload["bands"] == []
        assert payload["counts"]["bands"] == 0
        assert payload["bands_masked"] is True

    def test_detail_endpoint_is_masked_too(self, client, ladder):
        role = Role.objects.first()
        record = client.get(f"{API}{role.pk}/").json()
        assert record["band"] == ""
        assert record["bandN"] is None

    def test_a_non_staff_login_does_not_unlock_bands(self, client, plain_user, ladder):
        """Being signed in is not enough — it takes editor rights."""
        client.force_login(plain_user)
        assert all(r["band"] == "" for r in client.get(API).json()["results"])


class TestBandFilteringIsBlocked:
    """The masking would be pointless if the values were enumerable."""

    def test_band_filter_is_ignored_for_anonymous(self, client, ladder):
        """?band=Band 1 must NOT narrow the result set, or it discloses which
        roles are Band 1 by omission."""
        total = client.get(API).json()["count"]
        filtered = client.get(API + "?band=Band 1").json()["count"]
        assert filtered == total, "band filter leaked grading by narrowing the results"

    def test_band_range_filter_is_ignored_for_anonymous(self, client, ladder):
        total = client.get(API).json()["count"]
        assert client.get(API + "?band_min=5").json()["count"] == total
        assert client.get(API + "?band_max=2").json()["count"] == total

    def test_other_filters_still_work_for_anonymous(self, client, ladder):
        """Blocking band filters must not break search or BU filtering."""
        assert client.get(API + "?q=Managing").json()["count"] == 1
        assert client.get(API + "?bu=Internal Audit").json()["count"] > 0

    def test_staff_can_still_filter_by_band(self, staff_client, ladder):
        total = staff_client.get(API).json()["count"]
        filtered = staff_client.get(API + "?band=Band 1").json()["count"]
        assert filtered == 1 < total


class TestStaffSeeBands:
    def test_band_label_is_present(self, staff_client, ladder):
        bands = {r["band"] for r in staff_client.get(API).json()["results"]}
        assert "Band 1" in bands
        assert "Band 6.2" in bands

    def test_numeric_band_is_present(self, staff_client, ladder):
        record = next(
            r for r in staff_client.get(API).json()["results"] if r["band"] == "Band 6.2"
        )
        assert float(record["bandN"]) == 6.2

    def test_meta_lists_the_vocabulary(self, staff_client, ladder):
        payload = staff_client.get(META).json()
        assert payload["bands"] == ["Band 1", "Band 3", "Band 6.2", "Band 9", "Executive"]
        assert payload["bands_masked"] is False


class TestRankKeepsTheUiWorking:
    """`rank` is what Browse/Compare/Career-path order by when bands are hidden."""

    def test_rank_is_present_for_anonymous(self, client, ladder):
        for record in client.get(API).json()["results"]:
            assert "rank" in record

    def test_rank_orders_from_most_to_least_senior(self, client, ladder):
        by_position = {r["pos"]: r["rank"] for r in client.get(API).json()["results"]}
        assert by_position["Chief Operating Officer"] == 1
        assert by_position["Head of Internal Audit"] == 2
        assert by_position["Senior Analyst"] == 3
        assert by_position["Claims Assistant"] == 4

    def test_rank_is_dense_not_the_band_number(self, client, ladder):
        """Band 6.2 must surface as 3, never as 6.2 — that is the whole point."""
        record = next(
            r for r in client.get(API).json()["results"] if r["pos"] == "Senior Analyst"
        )
        assert record["rank"] == 3
        assert record["rank"] != 6.2

    def test_unbanded_roles_have_no_rank(self, client, ladder):
        record = next(
            r for r in client.get(API).json()["results"]
            if r["pos"] == "Group Managing Director"
        )
        assert record["rank"] is None

    def test_staff_get_rank_as_well(self, staff_client, ladder):
        """So the same front-end code path works for both audiences."""
        record = next(
            r for r in staff_client.get(API).json()["results"] if r["pos"] == "Head of Internal Audit"
        )
        assert record["rank"] == 2
        assert record["band"] == "Band 3"

    def test_rank_is_computed_in_one_query_regardless_of_row_count(
        self, client, ladder, django_assert_max_num_queries
    ):
        """A SerializerMethodField is a classic N+1 trap; this pins it shut."""
        with django_assert_max_num_queries(6):
            client.get(API + "?page_size=500")


class TestOrderingStillWorks:
    def test_default_ordering_is_by_seniority(self, client, ladder):
        ranks = [r["rank"] for r in client.get(API).json()["results"] if r["rank"]]
        assert ranks == sorted(ranks), "roles are no longer ordered by seniority"


class TestExportsAreUnaffected:
    """Editors exporting data still get the real values — this is a display
    control for the public site, not a data-retention change."""

    def test_csv_export_contains_bands(self, ladder):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("export_roles", format="csv", stdout=out, stderr=StringIO())
        assert "Band 6.2" in out.getvalue()
