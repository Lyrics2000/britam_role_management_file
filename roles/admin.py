"""Django admin: bulk editing, search, CSV export and the audit trail."""

from __future__ import annotations

import csv

from django.contrib import admin, messages
from django.db.models import Count
from django.http import HttpResponse
from django.utils.html import format_html

from roles.models import AIRequestLog, BusinessUnit, Role, RoleRevision

ROLE_EXPORT_COLUMNS = [
    ("business_unit__name", "BU / Function"),
    ("position", "Position"),
    ("band", "Band"),
    ("band_numeric", "Band (numeric)"),
    ("level", "Leadership level"),
    ("experience", "Experience required"),
    ("qualifications", "Qualifications"),
    ("purpose", "Role purpose"),
    ("focus_areas", "Key focus areas"),
    ("kras", "Key performance measures"),
    ("direct_reports", "Direct reports"),
    ("technical_competencies", "Technical competencies"),
    ("leadership_competencies", "Leadership competencies"),
    ("is_active", "Active"),
]


@admin.register(BusinessUnit)
class BusinessUnitAdmin(admin.ModelAdmin):
    list_display = ("name", "display_order", "role_count", "is_active", "updated_at")
    list_editable = ("display_order", "is_active")
    search_fields = ("name",)
    list_filter = ("is_active",)
    ordering = ("display_order", "name")
    readonly_fields = ("slug", "created_at", "updated_at", "created_by", "updated_by")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_role_count=Count("roles"))

    @admin.display(description="Roles", ordering="_role_count")
    def role_count(self, obj):
        return obj._role_count

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("position", "business_unit", "band", "level", "is_active", "updated_at")
    list_filter = ("business_unit", "band", "level", "is_active", "source")
    search_fields = (
        "position",
        "purpose",
        "qualifications",
        "focus_areas",
        "business_unit__name",
    )
    list_select_related = ("business_unit",)
    autocomplete_fields = ("business_unit",)
    ordering = ("business_unit__display_order", "band_numeric", "position")
    list_per_page = 50
    save_on_top = True
    actions = ("export_as_csv", "mark_active", "mark_inactive")

    fieldsets = (
        ("Identity", {"fields": ("business_unit", "position", "band", "band_numeric", "level", "is_active")}),
        ("Requirements", {"fields": ("experience", "qualifications")}),
        ("Content", {"fields": ("purpose", "focus_areas", "kras", "direct_reports")}),
        ("Competencies", {
            "fields": ("technical_competencies", "leadership_competencies"),
            "description": "Separate individual competencies with a pipe character ( | ).",
        }),
        ("Audit", {
            "classes": ("collapse",),
            "fields": ("source", "content_hash", "created_at", "created_by",
                       "updated_at", "updated_by", "revision_link"),
        }),
    )
    readonly_fields = (
        "band_numeric", "source", "content_hash", "created_at", "created_by",
        "updated_at", "updated_by", "revision_link",
    )

    @admin.display(description="Change history")
    def revision_link(self, obj):
        if not obj.pk:
            return "—"
        count = obj.revisions.count()
        return format_html(
            '<a href="/admin/roles/rolerevision/?role__id__exact={}">{} revision(s)</a>',
            obj.pk,
            count,
        )

    def save_model(self, request, obj, form, change):
        # Threaded through to roles.signals so the audit row names the editor.
        obj._audit_actor = request.user
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        obj._audit_actor = request.user
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            obj._audit_actor = request.user
            obj.delete()

    @admin.action(description="Export selected roles to CSV")
    def export_as_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="britam_roles.csv"'
        response.write("﻿")  # BOM so Excel opens UTF-8 correctly
        writer = csv.writer(response)
        writer.writerow([label for _field, label in ROLE_EXPORT_COLUMNS])
        for role in queryset.select_related("business_unit"):
            row = []
            for field, _label in ROLE_EXPORT_COLUMNS:
                if field == "business_unit__name":
                    row.append(role.business_unit.name)
                else:
                    row.append(getattr(role, field))
            writer.writerow(row)
        self.message_user(request, f"Exported {queryset.count()} role(s).", messages.SUCCESS)
        return response

    @admin.action(description="Mark selected roles active (visible on the site)")
    def mark_active(self, request, queryset):
        updated = 0
        for role in queryset:
            if not role.is_active:
                role.is_active = True
                role._audit_actor = request.user
                role.updated_by = request.user
                role.save()
                updated += 1
        self.message_user(request, f"{updated} role(s) marked active.", messages.SUCCESS)

    @admin.action(description="Mark selected roles inactive (hidden from the site)")
    def mark_inactive(self, request, queryset):
        updated = 0
        for role in queryset:
            if role.is_active:
                role.is_active = False
                role._audit_actor = request.user
                role.updated_by = request.user
                role.save()
                updated += 1
        self.message_user(request, f"{updated} role(s) hidden.", messages.WARNING)


@admin.register(RoleRevision)
class RoleRevisionAdmin(admin.ModelAdmin):
    """Read-only by design: an audit trail you can edit is not an audit trail."""

    list_display = ("created_at", "action", "role_label", "actor_label", "request_id")
    list_filter = ("action", "created_at")
    search_fields = ("role_label", "actor_label", "request_id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AIRequestLog)
class AIRequestLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "succeeded", "error_code", "latency_ms", "client_ip", "short_question")
    list_filter = ("succeeded", "error_code", "created_at")
    search_fields = ("question", "client_ip", "request_id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    @admin.display(description="Question")
    def short_question(self, obj):
        return obj.question[:80] + ("…" if len(obj.question) > 80 else "")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
