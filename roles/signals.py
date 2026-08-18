"""
Audit trail.

Every create/update/delete of a Role writes a RoleRevision row. Implemented
with signals rather than in the serializer so that changes made through the
Django admin, a management command or a shell session are captured too.

The actor is threaded through via `Role._audit_actor`, set by the view/admin
before save. When it is absent (e.g. the seeder) the revision records the
action with a null actor and an explanatory label.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from roles.logging_utils import get_request_id
from roles.models import CONTENT_FIELDS, Role, RoleRevision

logger = logging.getLogger(__name__)

TRACKED_FIELDS = CONTENT_FIELDS + ("is_active", "business_unit_id")


def _canonical(value: object) -> str | None:
    """Render a field value as a comparable string.

    Decimals need care: the database returns band_numeric as Decimal('3.00')
    (decimal_places=2) while the freshly-parsed in-memory value is
    Decimal('3'). Comparing str() of those two reports a change on every save,
    which filled the audit trail with phantom band_numeric edits.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value.normalize())
    return str(value)


def _snapshot(role: Role) -> dict[str, object]:
    data: dict[str, object] = {}
    for field in TRACKED_FIELDS:
        data[field] = _canonical(getattr(role, field, None))
    data["business_unit"] = role.business_unit.name if role.business_unit_id else None
    return data


def _label(role: Role) -> str:
    bu = role.business_unit.name if role.business_unit_id else "?"
    return f"{bu} / {role.position}"[:320]


def _actor_of(role: Role):
    return getattr(role, "_audit_actor", None) or role.updated_by


@receiver(pre_save, sender=Role)
def capture_previous_state(sender, instance: Role, **kwargs):
    """Stash the pre-save row so post_save can diff against it."""
    if not instance.pk:
        instance._previous_state = None
        return
    try:
        previous = Role.objects.select_related("business_unit").get(pk=instance.pk)
    except Role.DoesNotExist:  # deleted between the fetch and the save
        instance._previous_state = None
        return
    instance._previous_state = _snapshot(previous)


@receiver(post_save, sender=Role)
def write_revision(sender, instance: Role, created: bool, **kwargs):
    actor = _actor_of(instance)
    actor_label = getattr(actor, "username", "") or getattr(instance, "_audit_actor_label", "system")

    if created:
        changes = {"created": _snapshot(instance)}
        action = (
            RoleRevision.Action.SEED
            if instance.source == "seed"
            else RoleRevision.Action.CREATE
        )
    else:
        previous = getattr(instance, "_previous_state", None) or {}
        current = _snapshot(instance)
        changes = {
            field: {"from": previous.get(field), "to": current.get(field)}
            for field in current
            if previous.get(field) != current.get(field)
        }
        if not changes:
            # A save that changed nothing (common when the seeder re-runs) is
            # not worth an audit row.
            return
        action = (
            RoleRevision.Action.SEED
            if getattr(instance, "_audit_is_seed", False)
            else RoleRevision.Action.UPDATE
        )

    RoleRevision.objects.create(
        role=instance,
        role_label=_label(instance),
        action=action,
        actor=actor if actor is not None and getattr(actor, "pk", None) else None,
        actor_label=actor_label[:150],
        changes=changes,
        request_id=get_request_id()[:36],
    )


@receiver(post_delete, sender=Role)
def write_delete_revision(sender, instance: Role, **kwargs):
    actor = _actor_of(instance)
    RoleRevision.objects.create(
        role=None,
        role_label=_label(instance),
        action=RoleRevision.Action.DELETE,
        actor=actor if actor is not None and getattr(actor, "pk", None) else None,
        actor_label=(getattr(actor, "username", "") or "system")[:150],
        changes={"deleted": _snapshot(instance)},
        request_id=get_request_id()[:36],
    )
    logger.info("role deleted", extra={"role": _label(instance)})
