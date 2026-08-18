"""Permission classes.

Access model chosen for this deployment:
  * anyone may READ the role library (it is internal-careers content)
  * only authenticated STAFF may create, update or delete
"""

from __future__ import annotations

from rest_framework import permissions


class ReadOnlyOrStaff(permissions.BasePermission):
    """Safe methods for everyone; writes for `is_staff` users only.

    `is_staff` rather than a custom group so one checkbox in /admin/ grants
    both the API and the Django admin, and there is a single place to revoke.
    """

    message = "You must be signed in as a role-library editor to change roles."

    def has_permission(self, request, view) -> bool:
        if request.method in permissions.SAFE_METHODS:
            return True
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.is_staff and user.is_active)

    def has_object_permission(self, request, view, obj) -> bool:
        return self.has_permission(request, view)
