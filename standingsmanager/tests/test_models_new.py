"""Unit tests for new models - AA Standings Manager."""

import datetime as dt
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils.timezone import now

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from app_utils.testdata_factories import UserMainFactory

from ..models import (
    AuditLog,
    StandingRequest,
    StandingRevocation,
    StandingsEntry,
    SyncedCharacter,
)
from .factories import (
    EveEntityAllianceFactory,
    EveEntityCharacterFactory,
    EveEntityCorporationFactory,
)


class StandingsEntryTestCase(TestCase):
    """Test cases for StandingsEntry model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = UserMainFactory()
        cls.character_entity = EveEntityCharacterFactory()
        cls.corporation_entity = EveEntityCorporationFactory()
        cls.alliance_entity = EveEntityAllianceFactory()

    def test_create_character_standing(self):
        """Test creating a character standing entry."""
        standing = StandingsEntry.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingsEntry.EntityType.CHARACTER,
            standing=5.0,
            added_by=self.user,
            notes="Test character",
        )

        self.assertEqual(standing.eve_entity, self.character_entity)
        self.assertEqual(standing.entity_type, StandingsEntry.EntityType.CHARACTER)
        self.assertEqual(standing.standing, 5.0)
        self.assertEqual(standing.added_by, self.user)
        self.assertEqual(standing.notes, "Test character")
        self.assertIsNotNone(standing.added_date)

    def test_unique_constraint(self):
        """Test that only one standing can exist per entity."""
        StandingsEntry.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingsEntry.EntityType.CHARACTER,
            standing=5.0,
            added_by=self.user,
        )

        # Try to create duplicate
        with self.assertRaises(Exception):  # IntegrityError or similar
            StandingsEntry.objects.create(
                eve_entity=self.character_entity,
                entity_type=StandingsEntry.EntityType.CHARACTER,
                standing=10.0,
                added_by=self.user,
            )

    def test_standing_value_validation(self):
        """Test that standing value is validated between -10 and +10."""
        # Valid standing
        standing = StandingsEntry(
            eve_entity=self.character_entity,
            entity_type=StandingsEntry.EntityType.CHARACTER,
            standing=5.0,
            added_by=self.user,
        )
        standing.full_clean()  # Should not raise

        # Invalid standing (too high)
        standing_high = StandingsEntry(
            eve_entity=self.corporation_entity,
            entity_type=StandingsEntry.EntityType.CORPORATION,
            standing=15.0,
            added_by=self.user,
        )
        with self.assertRaises(ValidationError):
            standing_high.full_clean()

        # Invalid standing (too low)
        standing_low = StandingsEntry(
            eve_entity=self.alliance_entity,
            entity_type=StandingsEntry.EntityType.ALLIANCE,
            standing=-15.0,
            added_by=self.user,
        )
        with self.assertRaises(ValidationError):
            standing_low.full_clean()

    def test_manager_for_entity_type(self):
        """Test filtering standings by entity type."""
        # Create standings of different types
        StandingsEntry.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingsEntry.EntityType.CHARACTER,
            standing=5.0,
            added_by=self.user,
        )
        StandingsEntry.objects.create(
            eve_entity=self.corporation_entity,
            entity_type=StandingsEntry.EntityType.CORPORATION,
            standing=7.0,
            added_by=self.user,
        )

        # Test filtering
        characters = StandingsEntry.objects.all_characters()
        self.assertEqual(characters.count(), 1)
        self.assertEqual(
            characters.first().entity_type, StandingsEntry.EntityType.CHARACTER
        )

        corporations = StandingsEntry.objects.all_corporations()
        self.assertEqual(corporations.count(), 1)
        self.assertEqual(
            corporations.first().entity_type, StandingsEntry.EntityType.CORPORATION
        )

    def test_str_representation(self):
        """Test string representation of StandingsEntry."""
        standing = StandingsEntry.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingsEntry.EntityType.CHARACTER,
            standing=5.0,
            added_by=self.user,
        )

        str_repr = str(standing)
        self.assertIn(self.character_entity.name, str_repr)
        self.assertIn("5.0", str_repr)


class StandingRequestTestCase(TestCase):
    """Test cases for StandingRequest model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = UserMainFactory()
        cls.approver = UserMainFactory()
        cls.character_entity = EveEntityCharacterFactory()

    def test_create_pending_request(self):
        """Test creating a pending standing request."""
        request = StandingRequest.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingRequest.EntityType.CHARACTER,
            requested_by=self.user,
        )

        self.assertEqual(request.state, StandingRequest.State.PENDING)
        self.assertEqual(request.requested_by, self.user)
        self.assertIsNone(request.actioned_by)
        self.assertIsNone(request.action_date)
        self.assertIsNotNone(request.request_date)

    def test_unique_together_constraint(self):
        """Test that only one pending request can exist per entity."""
        StandingRequest.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingRequest.EntityType.CHARACTER,
            requested_by=self.user,
        )

        # Try to create duplicate pending request
        with self.assertRaises(Exception):  # IntegrityError
            StandingRequest.objects.create(
                eve_entity=self.character_entity,
                entity_type=StandingRequest.EntityType.CHARACTER,
                requested_by=self.user,
            )

    def test_approve_request(self):
        """Test approving a standing request."""
        request = StandingRequest.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingRequest.EntityType.CHARACTER,
            requested_by=self.user,
        )

        # Approve the request
        with patch("standingsmanager.models.notify"):
            standing_entry = request.approve(self.approver, standing=7.0)

        # Check request state
        request.refresh_from_db()
        self.assertEqual(request.state, StandingRequest.State.APPROVED)
        self.assertEqual(request.actioned_by, self.approver)
        self.assertIsNotNone(request.action_date)

        # Check standing was created
        self.assertIsNotNone(standing_entry)
        self.assertEqual(standing_entry.eve_entity, self.character_entity)
        self.assertEqual(standing_entry.standing, 7.0)
        self.assertEqual(standing_entry.added_by, self.user)

        # Check audit log was created
        audit_logs = AuditLog.objects.filter(
            action_type=AuditLog.ActionType.APPROVE_REQUEST,
            eve_entity=self.character_entity,
        )
        self.assertEqual(audit_logs.count(), 1)

    def test_reject_request(self):
        """Test rejecting a standing request."""
        request = StandingRequest.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingRequest.EntityType.CHARACTER,
            requested_by=self.user,
        )

        # Reject the request
        with patch("standingsmanager.models.notify"):
            request.reject(self.approver, reason="Test rejection")

        # Check request state
        request.refresh_from_db()
        self.assertEqual(request.state, StandingRequest.State.REJECTED)
        self.assertEqual(request.actioned_by, self.approver)
        self.assertIsNotNone(request.action_date)

        # Check no standing was created
        self.assertFalse(
            StandingsEntry.objects.filter(eve_entity=self.character_entity).exists()
        )

        # Check audit log was created
        audit_logs = AuditLog.objects.filter(
            action_type=AuditLog.ActionType.REJECT_REQUEST,
            eve_entity=self.character_entity,
        )
        self.assertEqual(audit_logs.count(), 1)

    def test_manager_filtering(self):
        """Test manager methods for filtering requests."""
        pending = StandingRequest.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingRequest.EntityType.CHARACTER,
            requested_by=self.user,
        )

        approved_entity = EveEntityCharacterFactory()
        approved = StandingRequest.objects.create(
            eve_entity=approved_entity,
            entity_type=StandingRequest.EntityType.CHARACTER,
            requested_by=self.user,
            state=StandingRequest.State.APPROVED,
        )

        # Test pending filter
        pending_requests = StandingRequest.objects.pending()
        self.assertEqual(pending_requests.count(), 1)
        self.assertEqual(pending_requests.first(), pending)

        # Test approved filter
        approved_requests = StandingRequest.objects.approved()
        self.assertEqual(approved_requests.count(), 1)
        self.assertEqual(approved_requests.first(), approved)

        # Test for_user filter
        user_requests = StandingRequest.objects.for_user(self.user)
        self.assertEqual(user_requests.count(), 2)


class StandingRevocationTestCase(TestCase):
    """Test cases for StandingRevocation model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = UserMainFactory()
        cls.approver = UserMainFactory()
        cls.character_entity = EveEntityCharacterFactory()

    def setUp(self):
        """Create a standing entry for each test."""
        self.standing = StandingsEntry.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingsEntry.EntityType.CHARACTER,
            standing=5.0,
            added_by=self.user,
        )

    def test_create_revocation(self):
        """Test creating a revocation request."""
        revocation = StandingRevocation.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingRevocation.EntityType.CHARACTER,
            requested_by=self.user,
            reason=StandingRevocation.Reason.USER_REQUEST,
        )

        self.assertEqual(revocation.state, StandingRevocation.State.PENDING)
        self.assertEqual(revocation.requested_by, self.user)
        self.assertEqual(revocation.reason, StandingRevocation.Reason.USER_REQUEST)
        self.assertIsNone(revocation.actioned_by)

    def test_approve_revocation(self):
        """Test approving a revocation request."""
        revocation = StandingRevocation.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingRevocation.EntityType.CHARACTER,
            requested_by=self.user,
            reason=StandingRevocation.Reason.USER_REQUEST,
        )

        # Approve the revocation
        with patch("standingsmanager.models.notify"):
            revocation.approve(self.approver)

        # Check revocation state
        revocation.refresh_from_db()
        self.assertEqual(revocation.state, StandingRevocation.State.APPROVED)
        self.assertEqual(revocation.actioned_by, self.approver)
        self.assertIsNotNone(revocation.action_date)

        # Check standing was deleted
        self.assertFalse(
            StandingsEntry.objects.filter(eve_entity=self.character_entity).exists()
        )

        # Check audit log was created
        audit_logs = AuditLog.objects.filter(
            action_type=AuditLog.ActionType.APPROVE_REVOCATION,
            eve_entity=self.character_entity,
        )
        self.assertEqual(audit_logs.count(), 1)

    def test_reject_revocation(self):
        """Test rejecting a revocation request."""
        revocation = StandingRevocation.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingRevocation.EntityType.CHARACTER,
            requested_by=self.user,
            reason=StandingRevocation.Reason.USER_REQUEST,
        )

        # Reject the revocation
        with patch("standingsmanager.models.notify"):
            revocation.reject(self.approver, reason="Test rejection")

        # Check revocation state
        revocation.refresh_from_db()
        self.assertEqual(revocation.state, StandingRevocation.State.REJECTED)
        self.assertEqual(revocation.actioned_by, self.approver)

        # Check standing still exists
        self.assertTrue(
            StandingsEntry.objects.filter(eve_entity=self.character_entity).exists()
        )

        # Check audit log was created
        audit_logs = AuditLog.objects.filter(
            action_type=AuditLog.ActionType.REJECT_REVOCATION,
            eve_entity=self.character_entity,
        )
        self.assertEqual(audit_logs.count(), 1)

    def test_auto_revocation(self):
        """Test creating an auto-revocation (system-initiated)."""
        revocation = StandingRevocation.objects.create(
            eve_entity=self.character_entity,
            entity_type=StandingRevocation.EntityType.CHARACTER,
            requested_by=None,  # No user for auto-revocation
            reason=StandingRevocation.Reason.LOST_PERMISSION,
        )

        self.assertIsNone(revocation.requested_by)
        self.assertEqual(revocation.reason, StandingRevocation.Reason.LOST_PERMISSION)


class AuditLogTestCase(TestCase):
    """Test cases for AuditLog model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = UserMainFactory()
        cls.approver = UserMainFactory()
        cls.character_entity = EveEntityCharacterFactory()

    def test_create_audit_log(self):
        """Test creating an audit log entry."""
        audit = AuditLog.objects.create(
            action_type=AuditLog.ActionType.APPROVE_REQUEST,
            eve_entity=self.character_entity,
            actioned_by=self.approver,
            requested_by=self.user,
        )

        self.assertEqual(audit.action_type, AuditLog.ActionType.APPROVE_REQUEST)
        self.assertEqual(audit.eve_entity, self.character_entity)
        self.assertEqual(audit.actioned_by, self.approver)
        self.assertEqual(audit.requested_by, self.user)
        self.assertIsNotNone(audit.timestamp)

    def test_immutability_update(self):
        """Test that audit log entries cannot be updated."""
        audit = AuditLog.objects.create(
            action_type=AuditLog.ActionType.APPROVE_REQUEST,
            eve_entity=self.character_entity,
            actioned_by=self.approver,
            requested_by=self.user,
        )

        # Try to update
        audit.action_type = AuditLog.ActionType.REJECT_REQUEST
        with self.assertRaises(ValidationError):
            audit.save()

    def test_immutability_delete(self):
        """Test that audit log entries cannot be deleted."""
        audit = AuditLog.objects.create(
            action_type=AuditLog.ActionType.APPROVE_REQUEST,
            eve_entity=self.character_entity,
            actioned_by=self.approver,
            requested_by=self.user,
        )

        # Try to delete
        with self.assertRaises(ValidationError):
            audit.delete()

    def test_manager_prevents_modifications(self):
        """Test that manager prevents bulk updates/deletes."""
        AuditLog.objects.create(
            action_type=AuditLog.ActionType.APPROVE_REQUEST,
            eve_entity=self.character_entity,
            actioned_by=self.approver,
            requested_by=self.user,
        )

        # Try bulk update
        with self.assertRaises(ValidationError):
            AuditLog.objects.update(action_type=AuditLog.ActionType.REJECT_REQUEST)

        # Try bulk delete
        with self.assertRaises(ValidationError):
            AuditLog.objects.delete()


class SyncedCharacterTestCase(TestCase):
    """Test cases for SyncedCharacter model."""

    def setUp(self):
        """Create user and character ownership for each test."""
        self.user = UserMainFactory()
        self.character = EveCharacter.objects.create(
            character_id=12345,
            character_name="Test Character",
            corporation_id=1000,
            corporation_name="Test Corp",
        )
        self.ownership = CharacterOwnership.objects.create(
            user=self.user,
            character=self.character,
            owner_hash="test_hash",
        )

    def test_create_synced_character(self):
        """Test creating a synced character."""
        synced = SyncedCharacter.objects.create(
            character_ownership=self.ownership,
        )

        self.assertEqual(synced.character_ownership, self.ownership)
        self.assertFalse(synced.has_label)
        self.assertIsNone(synced.last_sync_at)
        self.assertEqual(synced.last_error, "")

    def test_is_sync_fresh(self):
        """Test the is_sync_fresh property."""
        synced = SyncedCharacter.objects.create(
            character_ownership=self.ownership,
        )

        # Never synced
        self.assertFalse(synced.is_sync_fresh)

        # Recently synced
        synced.last_sync_at = now()
        synced.save()
        self.assertTrue(synced.is_sync_fresh)

        # Stale sync
        synced.last_sync_at = now() - dt.timedelta(hours=4)
        synced.save()
        self.assertFalse(synced.is_sync_fresh)

    def test_is_eligible_with_permission(self):
        """Test eligibility check with proper permissions."""
        # Grant permission
        from django.contrib.auth.models import Permission

        permission = Permission.objects.get(codename="add_syncedcharacter")
        self.user.user_permissions.add(permission)

        synced = SyncedCharacter.objects.create(
            character_ownership=self.ownership,
        )

        # Should be eligible
        self.assertTrue(synced.is_eligible())

    def test_is_eligible_without_permission(self):
        """Test eligibility check without permissions."""
        synced = SyncedCharacter.objects.create(
            character_ownership=self.ownership,
        )

        # Should not be eligible and should be deleted
        with patch("standingsmanager.models.notify"):
            is_eligible = synced.is_eligible()

        self.assertFalse(is_eligible)
        # Character should be deleted
        self.assertFalse(SyncedCharacter.objects.filter(pk=synced.pk).exists())
