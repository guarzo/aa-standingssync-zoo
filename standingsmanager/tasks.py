"""Tasks for standingsmanager - Refactored for AA Standings Manager."""

from celery import shared_task

from django.db.models import Q
from eveuniverse.core.esitools import is_esi_online
from eveuniverse.tasks import update_unresolved_eve_entities

from allianceauth.services.hooks import get_extension_logger
from app_utils.logging import LoggerAddTag

from . import __title__
from .app_settings import STANDINGS_AUTO_VALIDATE_INTERVAL, STANDINGS_SYNC_INTERVAL
from .models import StandingRevocation, StandingsEntry, SyncedCharacter

logger = LoggerAddTag(get_extension_logger(__name__), __title__)


DEFAULT_TASK_PRIORITY = 6


# ============================================================================
# Main Sync Tasks
# ============================================================================


@shared_task
def sync_all_characters():
    """Sync contacts for all synced characters.

    This is the main periodic task that runs at the configured interval.
    """
    if not is_esi_online():
        logger.warning("ESI is not online. Aborting sync.")
        return

    # Get all synced characters
    synced_characters = SyncedCharacter.objects.all()
    count = synced_characters.count()

    if count == 0:
        logger.info("No synced characters found. Nothing to sync.")
        return

    logger.info("Starting sync for %d synced characters", count)

    # Queue sync task for each character (staggered to avoid rate limiting)
    for idx, synced_char in enumerate(synced_characters):
        # Stagger tasks by 5 seconds each to avoid hitting ESI rate limits
        countdown = idx * 5
        sync_character.apply_async(
            args=[synced_char.pk],
            countdown=countdown,
            priority=DEFAULT_TASK_PRIORITY,
        )

    logger.info("Queued sync tasks for %d characters", count)

    # Update unresolved EVE entities after all syncs
    update_unresolved_eve_entities.apply_async(
        countdown=count * 5 + 60,  # Wait for all syncs to complete
        priority=DEFAULT_TASK_PRIORITY,
    )


@shared_task
def sync_character(synced_char_pk: int):
    """Sync contacts for a single character.

    Args:
        synced_char_pk: Primary key of SyncedCharacter to sync

    Returns:
        True if successful, False if character was deleted, None if no update needed
    """
    try:
        synced_character = SyncedCharacter.objects.get(pk=synced_char_pk)
    except SyncedCharacter.DoesNotExist:
        logger.warning("Synced character %d not found. Skipping.", synced_char_pk)
        return False

    logger.info("Starting sync for %s", synced_character)
    result = synced_character.run_sync()

    if result is False:
        logger.info("%s was deactivated during sync", synced_character)
    elif result is None:
        logger.info("%s: No update needed", synced_character)
    else:
        logger.info("%s: Sync completed successfully", synced_character)

    return result


# ============================================================================
# Validation Tasks
# ============================================================================


@shared_task
def validate_all_synced_characters():
    """Validate eligibility for all synced characters.

    Characters that are no longer eligible (lost permissions, invalid tokens, etc.)
    will be automatically removed.

    This runs periodically to ensure only authorized characters remain synced.
    """
    synced_characters = SyncedCharacter.objects.select_related(
        "character_ownership__user"
    )
    count = synced_characters.count()

    if count == 0:
        logger.info("No synced characters to validate.")
        return

    logger.info("Starting validation for %d synced characters", count)

    valid_count = 0
    invalid_count = 0

    for synced_char in synced_characters:
        if synced_char.is_eligible():
            valid_count += 1
        else:
            invalid_count += 1
            logger.info("%s: Removed due to failed eligibility check", synced_char)

    logger.info("Validation complete: %d valid, %d removed", valid_count, invalid_count)


@shared_task
def validate_synced_character(synced_char_pk: int):
    """Validate eligibility for a single synced character.

    Args:
        synced_char_pk: Primary key of SyncedCharacter to validate

    Returns:
        True if valid, False if removed
    """
    try:
        synced_character = SyncedCharacter.objects.get(pk=synced_char_pk)
    except SyncedCharacter.DoesNotExist:
        logger.warning("Synced character %d not found.", synced_char_pk)
        return False

    is_valid = synced_character.is_eligible()

    if is_valid:
        logger.info("%s: Validation passed", synced_character)
    else:
        logger.info("%s: Validation failed, removed", synced_character)

    return is_valid


@shared_task
def validate_all_standings():
    """Validate that all standings entries are still valid.

    For character standings, checks that the character's current owner
    (via CharacterOwnership) still has the required permission. This
    catches users who leave the corporation/alliance and lose permission,
    including all their alt characters.
    """
    from django.contrib.auth.models import Permission, User

    from allianceauth.authentication.models import CharacterOwnership

    character_standings = list(
        StandingsEntry.objects.all_characters().select_related("eve_entity", "added_by")
    )

    if not character_standings:
        logger.info("No character standings to validate.")
        return

    logger.info(
        "Starting standings validation for %d character entries",
        len(character_standings),
    )

    # Batch-load user IDs that have the required permission
    perm = Permission.objects.get(
        content_type__app_label="standingsmanager",
        codename="add_syncedcharacter",
    )
    users_with_perm = set(
        User.objects.filter(
            Q(user_permissions=perm)
            | Q(groups__permissions=perm)
            | Q(is_superuser=True),
            is_active=True,
        ).values_list("id", flat=True)
    )

    # Batch-load entity IDs that already have pending revocations
    entity_ids = [s.eve_entity_id for s in character_standings]
    pending_revocation_entity_ids = set(
        StandingRevocation.objects.pending()
        .filter(eve_entity_id__in=entity_ids)
        .values_list("eve_entity_id", flat=True)
    )

    # Batch-load character ownership: eve_character_id -> owner_user_id
    ownership_map = dict(
        CharacterOwnership.objects.filter(
            character__character_id__in=entity_ids
        ).values_list("character__character_id", "user_id")
    )

    # Also build a reverse map for logging: user_id -> username
    owner_user_ids = set(ownership_map.values())
    owner_usernames = dict(
        User.objects.filter(id__in=owner_user_ids).values_list("id", "username")
    )

    to_delete = []
    valid_count = 0
    skipped_count = 0
    no_owner_count = 0
    owner_lost_perm_count = 0

    for standing in character_standings:
        # Skip if already has a pending revocation
        if standing.eve_entity_id in pending_revocation_entity_ids:
            skipped_count += 1
            continue

        owner_user_id = ownership_map.get(standing.eve_entity_id)

        if owner_user_id is None:
            logger.info(
                "Auto-revoking standing for %s: no character owner found in Auth",
                standing.eve_entity.name,
            )
            to_delete.append(standing.eve_entity_id)
            no_owner_count += 1
            continue

        if owner_user_id not in users_with_perm:
            owner_name = owner_usernames.get(owner_user_id, "unknown")
            logger.info(
                "Auto-revoking standing for %s: character owner '%s' lost permission",
                standing.eve_entity.name,
                owner_name,
            )
            to_delete.append(standing.eve_entity_id)
            owner_lost_perm_count += 1
            continue

        valid_count += 1

    if to_delete:
        StandingsEntry.objects.filter(eve_entity_id__in=to_delete).delete()

    logger.info(
        "Standings validation complete: %d valid, %d revoked "
        "(no_owner=%d, owner_lost_perm=%d), %d skipped (pending revocation)",
        valid_count,
        len(to_delete),
        no_owner_count,
        owner_lost_perm_count,
        skipped_count,
    )


# ============================================================================
# Manual Trigger Tasks
# ============================================================================


@shared_task
def trigger_sync_after_approval():
    """Trigger sync for all characters after a standing request is approved.

    This ensures that all synced characters receive the new standing immediately.
    """
    logger.info("Triggering sync after standing approval")
    sync_all_characters.apply_async(
        priority=DEFAULT_TASK_PRIORITY + 1
    )  # Higher priority


@shared_task
def trigger_sync_after_revocation():
    """Trigger sync for all characters after a standing revocation is approved.

    This ensures that all synced characters remove the revoked standing immediately.
    """
    logger.info("Triggering sync after standing revocation")
    sync_all_characters.apply_async(
        priority=DEFAULT_TASK_PRIORITY + 1
    )  # Higher priority


@shared_task
def force_sync_character(synced_char_pk: int):
    """Force sync for a character (admin function).

    This bypasses normal scheduling and forces an immediate sync.

    Args:
        synced_char_pk: Primary key of SyncedCharacter to force sync

    Returns:
        Sync result
    """
    logger.info("Force sync requested for character %d", synced_char_pk)
    return sync_character(synced_char_pk)


# ============================================================================
# Periodic Task Configuration
# ============================================================================

# These tasks are registered with Celery Beat for periodic execution


@shared_task
def run_regular_sync():
    """Main periodic task: sync all characters and validate eligibility.

    This task should be scheduled to run at the STANDINGS_SYNC_INTERVAL.
    It triggers syncs for all characters.
    """
    logger.info("Running regular sync (interval: %d minutes)", STANDINGS_SYNC_INTERVAL)

    if not is_esi_online():
        logger.warning("ESI is not online. Aborting regular sync.")
        return

    # Sync all characters
    sync_all_characters.apply_async(priority=DEFAULT_TASK_PRIORITY)


@shared_task
def run_regular_validation():
    """Periodic task: validate all synced characters.

    This task should be scheduled to run at the STANDINGS_AUTO_VALIDATE_INTERVAL.
    It validates eligibility for all synced characters and removes those that
    no longer meet requirements.
    """
    logger.info(
        "Running regular validation (interval: %d minutes)",
        STANDINGS_AUTO_VALIDATE_INTERVAL,
    )

    validate_all_synced_characters.apply_async(priority=DEFAULT_TASK_PRIORITY)

    validate_all_standings.apply_async(priority=DEFAULT_TASK_PRIORITY)


# ============================================================================
# Celery Beat Schedule (for reference)
# ============================================================================

# Add this to your Alliance Auth settings to enable periodic tasks:
#
# from celery.schedules import crontab
#
# CELERYBEAT_SCHEDULE = {
#     'standingsmanager_run_regular_sync': {
#         'task': 'standingsmanager.tasks.run_regular_sync',
#         'schedule': crontab(minute=f'*/{STANDINGS_SYNC_INTERVAL}'),
#     },
#     'standingsmanager_run_regular_validation': {
#         'task': 'standingsmanager.tasks.run_regular_validation',
#         'schedule': crontab(minute=f'*/{STANDINGS_AUTO_VALIDATE_INTERVAL}'),
#     },
# }
