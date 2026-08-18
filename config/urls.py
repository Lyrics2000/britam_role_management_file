"""Root URL configuration."""

from __future__ import annotations

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from roles.views import HealthView, ReadyView, RoleLibraryView

urlpatterns = [
    # --- public site -------------------------------------------------------
    path("", RoleLibraryView.as_view(), name="role-library"),
    # --- api ---------------------------------------------------------------
    path("api/", include(("roles.urls", "roles"), namespace="api")),
    # --- auth --------------------------------------------------------------
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(
            template_name="roles/login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path(
        "accounts/logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),
    # --- back office -------------------------------------------------------
    path("admin/", admin.site.urls),
    # --- ops ---------------------------------------------------------------
    # /healthz: liveness. Answers as long as the process is up.
    # /readyz:  readiness. Also proves the database answers a query.
    path("healthz", HealthView.as_view(), name="healthz"),
    path("readyz", ReadyView.as_view(), name="readyz"),
]

admin.site.site_header = "Britam Group Role Library — administration"
admin.site.site_title = "Britam Role Library"
admin.site.index_title = "Role data"
