"""Shared fixtures.

The test database is a real on-disk SQLite file created by pytest-django, not
an in-memory one, so the WAL/busy_timeout pragmas applied in roles/apps.py are
exercised exactly as they are in production.
"""

from __future__ import annotations

import os

import pytest
from django.contrib.auth import get_user_model

from roles.models import BusinessUnit, Role

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

LEGACY_HTML_FIXTURE = """<!DOCTYPE html>
<html><head><title>t</title><style>.x{color:red}</style></head>
<body>
<script>
const ROLES = [
  {bu:"Internal Audit",pos:"Head of Internal Audit",band:"Band 3",bandN:3,level:"Change Leader",\
exp:"8-10 years, 4-5 in managerial capacity",quals:"Bachelor's in Finance; CPA(K)",\
desc:"Integrates audit services across the Group.",focus:"Governance,Risk Management",\
kras:"Audit Plan Coverage",reports:"IS Audit Manager"},
  {bu:"Internal Audit",pos:"Internal Auditor",band:"Band 7",bandN:7,level:"Emerging Leader",\
exp:"3 years in audit",quals:"Bachelor's; CPA(K) required",desc:"Conducts internal audits.",\
focus:"Audit,Internal Controls",kras:"Quality Assurance",reports:"None"},
  {bu:"BLA",pos:"Claims Assistant",band:"Band 9",bandN:9.0,level:"Direct Contributor",\
exp:"At least 1-2 year experience in a similar position.",quals:"Bachelor's degree",\
desc:"Processes medical claims, controlling member benefits.",focus:"",kras:"",reports:"",\
techcomp:"Knowledge of medical claims | Knowledge of insurance products",\
leadcomp:"Learning and Researching | Working with People"},
  {bu:"CX",pos:"Marketing Executive with a quote's apostrophe",band:"Band 8",bandN:8,\
level:"Individual Leader",exp:"2-4 years",quals:"Bachelor's in Marketing",\
desc:"Coordinates campaigns, uses commas, braces {like this} and \\"quotes\\".",\
focus:"Marketing campaigns,Brand coordination",kras:"Campaign ROI",reports:"None"},
  {bu:"Group Exco",pos:"Group Managing Director",band:"Executive",level:"Leader of Leaders",\
exp:"20+ years",quals:"MBA",desc:"Leads the Group.",focus:"Strategy",kras:"Group performance",\
reports:"All directors"},
];
</script>
</body></html>
"""


@pytest.fixture
def legacy_html(tmp_path):
    """A small but representative copy of the legacy file."""
    path = tmp_path / "Britam_Role_Library.html"
    path.write_text(LEGACY_HTML_FIXTURE, encoding="utf-8")
    return path


@pytest.fixture
def business_unit(db):
    return BusinessUnit.objects.create(name="Internal Audit", display_order=1)


@pytest.fixture
def role(db, business_unit):
    return Role.objects.create(
        business_unit=business_unit,
        position="Head of Internal Audit",
        band="Band 3",
        level="Change Leader",
        experience="8-10 years",
        qualifications="Bachelor's in Finance; CPA(K)",
        purpose="Integrates audit services across the Group.",
        focus_areas="Governance,Risk Management",
        kras="Audit Plan Coverage",
        direct_reports="IS Audit Manager",
        source="seed",
    )


@pytest.fixture
def staff_user(db):
    return get_user_model().objects.create_user(
        username="editor",
        password="a-long-enough-passphrase-42",
        is_staff=True,
    )


@pytest.fixture
def plain_user(db):
    return get_user_model().objects.create_user(
        username="viewer",
        password="another-long-passphrase-42",
        is_staff=False,
    )


@pytest.fixture
def staff_client(staff_user):
    """A *separate* Client from the `client` fixture.

    Reusing pytest-django's `client` and calling force_login on it would
    authenticate the same object a test also uses to assert anonymous
    behaviour — which silently turned permission tests into no-ops.
    """
    from django.test import Client

    authenticated = Client()
    authenticated.force_login(staff_user)
    return authenticated


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    """DRF throttling counts live in LocMemCache, which survives between tests.

    Without this, a test that makes two AI calls inherits the previous test's
    counter and gets a 429 instead of the behaviour under test.
    """
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
