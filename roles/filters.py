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

    def filter_search(self, queryset, name, value):
        return queryset.search(value)
