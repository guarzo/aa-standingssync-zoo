from django.db import migrations


def fix_added_by_from_approver_to_requester(apps, schema_editor):
    """Fix existing StandingsEntry.added_by to point to the requester, not the approver.

    Previously, StandingRequest.approve() incorrectly set added_by=approver
    instead of added_by=requested_by. This data migration corrects all existing
    entries by looking up the original approved StandingRequest.
    """
    StandingsEntry = apps.get_model("standingsmanager", "StandingsEntry")
    StandingRequest = apps.get_model("standingsmanager", "StandingRequest")

    entries = list(StandingsEntry.objects.all())
    if not entries:
        return

    # Batch-load the most recent approved request per entity
    entity_ids = [e.eve_entity_id for e in entries]
    approved_requests = StandingRequest.objects.filter(
        eve_entity_id__in=entity_ids,
        state="approved",
    ).order_by("eve_entity_id", "-action_date")

    request_by_entity = {}
    for req in approved_requests:
        if req.eve_entity_id not in request_by_entity:
            request_by_entity[req.eve_entity_id] = req

    to_update = []
    for entry in entries:
        req = request_by_entity.get(entry.eve_entity_id)
        if not req:
            continue

        if entry.added_by_id != req.requested_by_id:
            entry.added_by_id = req.requested_by_id
            to_update.append(entry)

    if to_update:
        StandingsEntry.objects.bulk_update(to_update, ["added_by_id"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("standingsmanager", "0005_add_missing_permissions"),
    ]

    operations = [
        migrations.RunPython(
            fix_added_by_from_approver_to_requester,
            migrations.RunPython.noop,
        ),
    ]
