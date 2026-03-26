"""Models for standingsmanager - Refactored for AA Standings Manager.

This module contains the new persistent standings database models
and simplified sync character management.
"""

import datetime as dt
from typing import Optional

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.utils.timezone import now
from esi.errors import TokenExpiredError, TokenInvalidError
from esi.models import Token
from eveuniverse.models import EveEntity

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from allianceauth.notifications import notify
from allianceauth.services.hooks import get_extension_logger
from app_utils.logging import LoggerAddTag

from . import __title__
from .app_settings import (
    STANDINGS_DEFAULT_STANDING,
    STANDINGS_LABEL_NAME,
    STANDINGS_SYNC_TIMEOUT,
)
from .core import esi_api
from .core.esi_contacts import EsiContact, EsiContactsContainer
from .managers import (
    AuditLogManager,
    StandingRequestManager,
    StandingRevocationManager,
    StandingsEntryManager,
    SyncedCharacterManager,
)

logger = LoggerAddTag(get_extension_logger(__name__), __title__)


# ============================================================================
# Core Standings Models
# ============================================================================


class StandingsEntry(models.Model):
    """Persistent standings database entry - the source of truth for standings.

    This model represents entities (characters, corporations, alliances) that
    have been approved to receive standings. It replaces the old alliance-level
    contact dependency with a plugin-managed database.
    """

    class EntityType(models.TextChoices):
        """Entity type choices."""

        CHARACTER = "character", "Character"
        CORPORATION = "corporation", "Corporation"
        ALLIANCE = "alliance", "Alliance"

    eve_entity = models.OneToOneField(
        EveEntity,
        on_delete=models.CASCADE,
        primary_key=True,
        help_text="EVE entity to receive standing",
    )
    standing = models.FloatField(
        validators=[MinValueValidator(-10.0), MaxValueValidator(10.0)],
        help_text="Standing value (-10.0 to +10.0)",
    )
    entity_type = models.CharField(
        max_length=20,
        choices=EntityType.choices,
        help_text="Type of entity (character, corporation, or alliance)",
    )
    added_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="standings_added",
        help_text="User who requested this standing",
    )
    added_date = models.DateTimeField(
        auto_now_add=True,
        help_text="When this standing was added",
    )
    notes = models.TextField(
        blank=True,
        help_text="Optional notes about this standing",
    )

    objects = StandingsEntryManager()

    class Meta:
        verbose_name = "Standing Entry"
        verbose_name_plural = "Standing Entries"
        ordering = ["entity_type", "eve_entity__name"]
        permissions = [
            ("manage_standings", "Can manage standings database"),
        ]

    def __str__(self):
        return f"{self.eve_entity.name} ({self.get_entity_type_display()}): {self.standing}"

    def clean(self):
        """Validate the model."""
        super().clean()
        # Validate standing is within range
        if not -10.0 <= self.standing <= 10.0:
            raise ValidationError(
                {"standing": "Standing must be between -10.0 and +10.0"}
            )

        # Validate entity type matches eve_entity category
        entity_category_map = {
            self.EntityType.CHARACTER: EveEntity.CATEGORY_CHARACTER,
            self.EntityType.CORPORATION: EveEntity.CATEGORY_CORPORATION,
            self.EntityType.ALLIANCE: EveEntity.CATEGORY_ALLIANCE,
        }
        expected_category = entity_category_map.get(self.entity_type)
        if expected_category and self.eve_entity.category != expected_category:
            raise ValidationError(
                {
                    "entity_type": f"Entity type {self.entity_type} does not match "
                    f"EVE entity category {self.eve_entity.category}"
                }
            )


class StandingRequest(models.Model):
    """User request to add an entity to the standings database.

    Requires approval by a user with approve_standings permission.
    """

    class State(models.TextChoices):
        """Request state choices."""

        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class EntityType(models.TextChoices):
        """Entity type choices."""

        CHARACTER = "character", "Character"
        CORPORATION = "corporation", "Corporation"
        ALLIANCE = "alliance", "Alliance"

    eve_entity = models.ForeignKey(
        EveEntity,
        on_delete=models.CASCADE,
        help_text="Entity being requested for standing",
    )
    entity_type = models.CharField(
        max_length=20,
        choices=EntityType.choices,
        help_text="Type of entity being requested",
    )
    requested_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="standing_requests",
        help_text="User who requested this standing",
    )
    request_date = models.DateTimeField(
        auto_now_add=True,
        help_text="When this request was created",
    )
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PENDING,
        help_text="Current state of the request",
    )
    actioned_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="standing_requests_actioned",
        help_text="User who approved or rejected this request",
    )
    action_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this request was actioned",
    )

    objects = StandingRequestManager()

    class Meta:
        verbose_name = "Standing Request"
        verbose_name_plural = "Standing Requests"
        unique_together = [("eve_entity", "state")]
        ordering = ["-request_date"]
        permissions = [
            ("approve_standings", "Can approve standing requests"),
        ]

    def __str__(self):
        return (
            f"{self.eve_entity.name} ({self.get_entity_type_display()}) "
            f"- {self.get_state_display()}"
        )

    def approve(
        self, approver: User, standing: Optional[float] = None, notes: str = ""
    ):
        """Approve this request and create a StandingsEntry.

        Args:
            approver: User approving the request
            standing: Standing value to assign (default: STANDINGS_DEFAULT_STANDING)
            notes: Optional notes for the standing entry

        Returns:
            The created StandingsEntry

        Raises:
            ValidationError: If the request is not pending or standing already exists
        """
        if self.state != self.State.PENDING:
            raise ValidationError(f"Cannot approve request in state: {self.state}")

        # Check if standing already exists
        if StandingsEntry.objects.filter(eve_entity=self.eve_entity).exists():
            raise ValidationError(f"Standing already exists for {self.eve_entity.name}")

        # Use default standing if not specified
        if standing is None:
            standing = STANDINGS_DEFAULT_STANDING

        # Create the standing entry
        with transaction.atomic():
            standing_entry = StandingsEntry.objects.create(
                eve_entity=self.eve_entity,
                standing=standing,
                entity_type=self.entity_type,
                added_by=self.requested_by,
                notes=notes,
            )

            # Update request state
            self.state = self.State.APPROVED
            self.actioned_by = approver
            self.action_date = now()
            self.save()

            # Create audit log entry
            AuditLog.objects.create(
                action_type=AuditLog.ActionType.APPROVE_REQUEST,
                eve_entity=self.eve_entity,
                actioned_by=approver,
                requested_by=self.requested_by,
            )

            # Notify requester
            notify(
                self.requested_by,
                f"Standing Request Approved: {self.eve_entity.name}",
                f"Your standing request for {self.eve_entity.name} "
                f"({self.get_entity_type_display()}) has been approved with standing {standing}.",
            )

        logger.info(
            "%s approved standing request for %s by %s",
            approver,
            self.eve_entity.name,
            self.requested_by,
        )

        return standing_entry

    def reject(self, approver: User, reason: str = ""):
        """Reject this request.

        Args:
            approver: User rejecting the request
            reason: Optional reason for rejection
        """
        if self.state != self.State.PENDING:
            raise ValidationError(f"Cannot reject request in state: {self.state}")

        with transaction.atomic():
            # Update request state
            self.state = self.State.REJECTED
            self.actioned_by = approver
            self.action_date = now()
            self.save()

            # Create audit log entry
            AuditLog.objects.create(
                action_type=AuditLog.ActionType.REJECT_REQUEST,
                eve_entity=self.eve_entity,
                actioned_by=approver,
                requested_by=self.requested_by,
            )

            # Notify requester
            message = (
                f"Your standing request for {self.eve_entity.name} "
                f"({self.get_entity_type_display()}) has been rejected."
            )
            if reason:
                message += f"\n\nReason: {reason}"

            notify(
                self.requested_by,
                f"Standing Request Rejected: {self.eve_entity.name}",
                message,
            )

        logger.info(
            "%s rejected standing request for %s by %s",
            approver,
            self.eve_entity.name,
            self.requested_by,
        )


class StandingRevocation(models.Model):
    """Request to remove an entity from the standings database.

    Can be user-initiated or auto-generated by the system.
    """

    class State(models.TextChoices):
        """Revocation state choices."""

        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class Reason(models.TextChoices):
        """Revocation reason choices."""

        USER_REQUEST = "user_request", "User Request"
        LOST_PERMISSION = "lost_permission", "Lost Permission"
        MISSING_TOKEN = "missing_token", "Missing Token"

    class EntityType(models.TextChoices):
        """Entity type choices."""

        CHARACTER = "character", "Character"
        CORPORATION = "corporation", "Corporation"
        ALLIANCE = "alliance", "Alliance"

    eve_entity = models.ForeignKey(
        EveEntity,
        on_delete=models.CASCADE,
        help_text="Entity to be removed from standings",
    )
    entity_type = models.CharField(
        max_length=20,
        choices=EntityType.choices,
        help_text="Type of entity",
    )
    requested_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="standing_revocations",
        help_text="User who requested this revocation (null for auto-revocations)",
    )
    request_date = models.DateTimeField(
        auto_now_add=True,
        help_text="When this revocation was created",
    )
    reason = models.CharField(
        max_length=30,
        choices=Reason.choices,
        help_text="Reason for revocation",
    )
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PENDING,
        help_text="Current state of the revocation",
    )
    actioned_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="standing_revocations_actioned",
        help_text="User who approved or rejected this revocation",
    )
    action_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this revocation was actioned",
    )

    objects = StandingRevocationManager()

    class Meta:
        verbose_name = "Standing Revocation"
        verbose_name_plural = "Standing Revocations"
        ordering = ["-request_date"]

    def __str__(self):
        return (
            f"{self.eve_entity.name} ({self.get_entity_type_display()}) "
            f"- {self.get_state_display()}"
        )

    def approve(self, approver: User):
        """Approve this revocation and remove the StandingsEntry.

        Args:
            approver: User approving the revocation

        Raises:
            ValidationError: If the revocation is not pending
        """
        if self.state != self.State.PENDING:
            raise ValidationError(f"Cannot approve revocation in state: {self.state}")

        with transaction.atomic():
            # Delete the standing entry if it exists
            try:
                standing_entry = StandingsEntry.objects.get(eve_entity=self.eve_entity)
                standing_entry.delete()
                logger.info("Deleted standing entry for %s", self.eve_entity.name)
            except StandingsEntry.DoesNotExist:
                logger.warning(
                    "Standing entry for %s not found during revocation approval",
                    self.eve_entity.name,
                )

            # Update revocation state
            self.state = self.State.APPROVED
            self.actioned_by = approver
            self.action_date = now()
            self.save()

            # Create audit log entry
            AuditLog.objects.create(
                action_type=AuditLog.ActionType.APPROVE_REVOCATION,
                eve_entity=self.eve_entity,
                actioned_by=approver,
                requested_by=self.requested_by if self.requested_by else approver,
            )

            # Notify requester (if user-initiated)
            if self.requested_by:
                notify(
                    self.requested_by,
                    f"Standing Revocation Approved: {self.eve_entity.name}",
                    f"Your revocation request for {self.eve_entity.name} "
                    f"({self.get_entity_type_display()}) has been approved.",
                )

        logger.info(
            "%s approved standing revocation for %s", approver, self.eve_entity.name
        )

    def reject(self, approver: User, reason: str = ""):
        """Reject this revocation (keep the standing).

        Args:
            approver: User rejecting the revocation
            reason: Optional reason for rejection
        """
        if self.state != self.State.PENDING:
            raise ValidationError(f"Cannot reject revocation in state: {self.state}")

        with transaction.atomic():
            # Update revocation state
            self.state = self.State.REJECTED
            self.actioned_by = approver
            self.action_date = now()
            self.save()

            # Create audit log entry
            AuditLog.objects.create(
                action_type=AuditLog.ActionType.REJECT_REVOCATION,
                eve_entity=self.eve_entity,
                actioned_by=approver,
                requested_by=self.requested_by if self.requested_by else approver,
            )

            # Notify requester (if user-initiated)
            if self.requested_by:
                message = (
                    f"Your revocation request for {self.eve_entity.name} "
                    f"({self.get_entity_type_display()}) has been rejected. "
                    "The standing will remain in place."
                )
                if reason:
                    message += f"\n\nReason: {reason}"

                notify(
                    self.requested_by,
                    f"Standing Revocation Rejected: {self.eve_entity.name}",
                    message,
                )

        logger.info(
            "%s rejected standing revocation for %s", approver, self.eve_entity.name
        )


class AuditLog(models.Model):
    """Immutable audit log of all standings actions.

    This model is frozen - records can only be created, never updated or deleted.
    """

    class ActionType(models.TextChoices):
        """Action type choices."""

        APPROVE_REQUEST = "approve_request", "Approve Request"
        REJECT_REQUEST = "reject_request", "Reject Request"
        APPROVE_REVOCATION = "approve_revocation", "Approve Revocation"
        REJECT_REVOCATION = "reject_revocation", "Reject Revocation"

    action_type = models.CharField(
        max_length=30,
        choices=ActionType.choices,
        help_text="Type of action performed",
    )
    eve_entity = models.ForeignKey(
        EveEntity,
        on_delete=models.CASCADE,
        help_text="Entity affected by this action",
    )
    actioned_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="audit_logs_actioned",
        help_text="User who performed the action",
    )
    requested_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="audit_logs_requested",
        help_text="User who made the original request",
    )
    timestamp = models.DateTimeField(
        auto_now_add=True,
        help_text="When this action occurred",
    )

    objects = AuditLogManager()

    class Meta:
        verbose_name = "Audit Log Entry"
        verbose_name_plural = "Audit Log Entries"
        ordering = ["-timestamp"]
        # Note: Django automatically creates view_auditlog permission, so we don't need to define it

    def __str__(self):
        return (
            f"{self.get_action_type_display()}: {self.eve_entity.name} "
            f"by {self.actioned_by} at {self.timestamp}"
        )

    def save(self, *args, **kwargs):
        """Override save to prevent updates."""
        if self.pk is not None:
            raise ValidationError("Audit log entries cannot be updated")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Override delete to prevent deletion."""
        raise ValidationError("Audit log entries cannot be deleted")


# ============================================================================
# Character Sync Model
# ============================================================================


class SyncedCharacter(models.Model):
    """A character that receives automated contact synchronization.

    Simplified from the original version to work with the persistent
    standings database instead of alliance-level contacts.
    """

    character_ownership = models.OneToOneField(
        CharacterOwnership,
        on_delete=models.CASCADE,
        primary_key=True,
        help_text="Character to sync",
    )
    has_label = models.BooleanField(
        default=False,
        help_text=f"Whether this character has the configured label ({STANDINGS_LABEL_NAME}) in-game",
    )
    last_sync_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the last successful sync was completed",
    )
    last_error = models.TextField(
        blank=True,
        help_text="Last error encountered during sync (if any)",
    )

    objects = SyncedCharacterManager()

    class Meta:
        verbose_name = "Synced Character"
        verbose_name_plural = "Synced Characters"
        ordering = ["character_ownership__character__character_name"]

    def __str__(self):
        try:
            character_name = self.character.character_name
        except ObjectDoesNotExist:
            character_name = "?"
        return f"{character_name}"

    @property
    def character(self) -> EveCharacter:
        """Return EveCharacter used to sync."""
        return self.character_ownership.character

    @property
    def character_id(self) -> Optional[int]:
        """Return character ID of the EveCharacter used to sync."""
        return self.character.character_id if self.character else None

    @property
    def is_sync_fresh(self) -> bool:
        """Return True when sync is not stale, else False."""
        if not self.last_sync_at:
            return False

        deadline = now() - dt.timedelta(minutes=STANDINGS_SYNC_TIMEOUT)
        return self.last_sync_at > deadline

    def run_sync(self) -> Optional[bool]:
        """Sync in-game contacts for this character with standings database.

        Returns:
            - False when the sync character was deleted
            - None when no update was needed
            - True when update was done successfully
        """
        # Validate eligibility
        if not self.is_eligible():
            return False

        # Get valid token
        token = self.fetch_token()
        if not token:
            logger.error("%s: Can not sync. No valid token found.", self)
            self.last_error = "No valid token found"
            self.save()
            return False

        try:
            # Fetch current contacts and labels
            current_contacts = self._fetch_current_contacts(token)
            self._update_label_info(current_contacts)

            # Check for label
            if not self.has_label:
                error_msg = f"Label '{STANDINGS_LABEL_NAME}' not found. Please create it in-game."
                logger.warning("%s: %s", self, error_msg)
                self.last_error = error_msg
                self.save()
                return None

            # Build target contacts from standings database
            label_id = current_contacts.get_label_by_name(STANDINGS_LABEL_NAME).id
            new_contacts = self._build_target_contacts(current_contacts, label_id)

            # Update contacts on ESI
            self._update_contacts_on_esi(token, current_contacts, new_contacts)

            # Record successful sync
            self.last_sync_at = now()
            self.last_error = ""
            self.save()

            logger.info("%s: Sync completed successfully", self)
            return True

        except Exception as ex:
            error_msg = f"Sync failed: {ex}"
            logger.exception("%s: %s", self, error_msg)
            self.last_error = error_msg
            self.save()
            return None

    def is_eligible(self) -> bool:
        """Check if this character is still eligible for sync.

        Returns False and deactivates the character if ineligible.
        """
        # Check user permissions
        if not self.character_ownership.user.has_perm(
            "standingsmanager.add_syncedcharacter"
        ):
            logger.info(
                "%s: sync deactivated due to insufficient user permissions", self
            )
            self._deactivate_sync("you no longer have permission for this service")
            return False

        return True

    def fetch_token(self) -> Optional[Token]:
        """Fetch valid token with required scopes.

        Will deactivate this character if any severe issues are encountered.
        """
        try:
            token = self._valid_token()

        except TokenInvalidError:
            self._deactivate_sync("your token is no longer valid")
            logger.info("%s: sync deactivated due to invalid token", self)
            return None

        except TokenExpiredError:
            self._deactivate_sync("your token has expired")
            logger.info("%s: sync deactivated due to expired token", self)
            return None

        if token is None:
            self._deactivate_sync("you do not have a token anymore")
            logger.info("%s: can not find suitable token for synced char", self)
            return None

        return token

    def _valid_token(self) -> Optional[Token]:
        """Get a valid token with required scopes."""
        return (
            Token.objects.filter(
                user=self.character_ownership.user,
                character_id=self.character_ownership.character.character_id,
            )
            .require_scopes(self.get_esi_scopes())
            .require_valid()
            .first()
        )

    def _deactivate_sync(self, message: str):
        """Deactivate character and send a message to the user about the issue."""
        message = (
            "Standings Sync has been deactivated for your "
            f"character {self}, because {message}.\n"
            "Feel free to activate sync for your character again, "
            "once the issue has been resolved."
        )
        notify(
            self.character_ownership.user,
            f"Standings Sync deactivated for {self}",
            message,
        )
        self.delete()

    def _fetch_current_contacts(self, token: Token) -> EsiContactsContainer:
        """Fetch current contacts and labels from ESI."""
        contacts = esi_api.fetch_character_contacts(token)
        labels = esi_api.fetch_character_contact_labels(token)
        current_contacts = EsiContactsContainer.from_esi_contacts(contacts, labels)
        logger.debug("%s: fetched %d contacts", self, len(contacts))
        return current_contacts

    def _update_label_info(self, current_contacts: EsiContactsContainer):
        """Update info about whether the configured label exists."""
        label = current_contacts.get_label_by_name(STANDINGS_LABEL_NAME)
        has_label = label is not None
        if has_label != self.has_label:
            self.has_label = has_label
            self.save()

    def _build_target_contacts(
        self, current_contacts: EsiContactsContainer, label_id: int
    ) -> EsiContactsContainer:
        """Build target contact list from standings database.

        Args:
            current_contacts: Current contacts container (for labels)
            label_id: ID of the configured label

        Returns:
            New contacts container with target state
        """
        # Start with current contacts
        new_contacts = current_contacts.clone()

        # Get all standings from database
        all_standings = StandingsEntry.objects.all()

        # Remove old managed contacts (those with our label)
        contacts_to_remove = [
            contact
            for contact in new_contacts.contacts()
            if label_id in contact.label_ids
        ]
        for contact in contacts_to_remove:
            new_contacts.remove_contact(contact)

        # Add all standings with our label
        for standing_entry in all_standings:
            try:
                esi_contact = EsiContact(
                    contact_id=standing_entry.eve_entity.id,
                    contact_type=standing_entry.eve_entity.category,
                    standing=standing_entry.standing,
                    label_ids=[label_id],
                )
                new_contacts.add_contact(esi_contact)
            except ValueError as ex:
                logger.warning(
                    "%s: Skipping invalid standing entry %s: %s",
                    self,
                    standing_entry.eve_entity.name,
                    ex,
                )

        return new_contacts

    def _update_contacts_on_esi(
        self,
        token: Token,
        current_contacts: EsiContactsContainer,
        new_contacts: EsiContactsContainer,
    ):
        """Update contacts on ESI based on difference between current and new."""
        added, removed, changed = current_contacts.contacts_difference(new_contacts)

        if removed:
            esi_api.delete_character_contacts(token, removed)
            logger.info("%s: Deleted %d contacts", self, len(removed))

        if added:
            esi_api.add_character_contacts(token, added)
            logger.info("%s: Added %d contacts", self, len(added))

        if changed:
            esi_api.update_character_contacts(token, changed)
            logger.info("%s: Updated %d contacts", self, len(changed))

        if not added and not removed and not changed:
            logger.info("%s: Nothing updated. Contacts were already up-to-date.", self)
        else:
            logger.info("%s: Contacts update completed.", self)

    @staticmethod
    def get_esi_scopes() -> list:
        """Return required ESI scopes."""
        return ["esi-characters.read_contacts.v1", "esi-characters.write_contacts.v1"]
