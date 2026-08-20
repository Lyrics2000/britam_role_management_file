"""Query filters for the role list endpoint."""

from __future__ import annotations

import django_filters as filters

from roles.models import Role


class RoleFilter(filters.FilterSet):
    """?q=&bu=&band=&level=&active=

    Every parameter is optional; unknown parameters are ignored rather than
    erroring, because the front-end appends cache-busting params.
    """

    q = filters.CharFilter(method="filter_search", label="Free text search")
    bu = filters.CharFilter(field_name="business_unit__name", lookup_expr="iexact")
    bu_id = filters.NumberFilter(field_name="business_unit_id")
    band = filters.CharFilter(field_name="band", lookup_expr="iexact")
    band_min = filters.NumberFilter(field_name="band_numeric", lookup_expr="gte")
    band_max = filters.NumberFilter(field_name="band_numeric", lookup_expr="lte")
    level = filters.CharFilter(field_name="level", lookup_expr="iexact")
    active = filters.BooleanFilter(field_name="is_active")

    class Meta:
        model = Role
        fields = ("q", "bu", "bu_id", "band", "band_min", "band_max", "level", "active")

    # Band filters are editor-only. Without this, masking would be trivially
    # defeated: an anonymous caller could send ?band=Band%201 and read the
    # grading of every role back out of which ones the filter returns.
    # See ADR-019 in roles/serializers.py.
    BAND_FILTERS = ("band", "band_min", "band_max")

    def filter_queryset(self, queryset):
        request = getattr(self, "request", None)
        user = getattr(request, "user", None)
        may_filter_by_band = bool(user and user.is_authenticated and user.is_staff)

        for name, value in self.form.cleaned_data.items():
            if name in self.BAND_FILTERS and not may_filter_by_band:
                continue
            queryset = self.filters[name].filter(queryset, value)
        return queryset

    def filter_search(self, queryset, name, value):
        return queryset.search(value)
