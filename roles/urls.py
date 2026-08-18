"""API routes, mounted at /api/ by config/urls.py."""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from roles.views import AIView, BusinessUnitViewSet, MetaView, RoleViewSet

router = DefaultRouter()
router.register("roles", RoleViewSet, basename="role")
router.register("business-units", BusinessUnitViewSet, basename="business-unit")

urlpatterns = [
    path("meta/", MetaView.as_view(), name="meta"),
    path("ai/", AIView.as_view(), name="ai"),
    path("", include(router.urls)),
]
