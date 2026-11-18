import sqlite3
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from shared.errors import ApplicationError, ErrorCode
from shared.infrastructure.sqlite import SQLiteStore
from shared.models.ai import ProviderProfile


class ProfileStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SQLiteStore(Path(temporary.name) / "profiles.sqlite3")
        self.store.initialize()
        self.owner = self.store.create_account("Alice", "test-hash").id
        self.other = self.store.create_account("Bob", "test-hash").id
        self.first = self.card("first", self.owner, "ref-first")
        self.second = self.card("second", self.owner, "ref-second")
        self.empty = self.card("empty", self.owner, None)
        self.bob = self.card("bob", self.other, "ref-bob")

    def card(self, identifier, owner, reference):
        profile = ProviderProfile(
            identifier,
            owner,
            identifier,
            "https://example.test/chat",
            "test-model",
            "chat-completions-compatible",
            0,
            False,
        )
        return self.store.save_profile(owner, profile, None, reference)

    def reject(self, code, call):
        with self.assertRaises(ApplicationError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_save_list_and_reopen(self):
        self.assertEqual(self.first.configuration_revision, 1)
        self.assertTrue(self.first.credential_present)
        self.assertEqual(self.store.list_profiles(self.other), (self.bob,))
        self.assertNotIn("credential_reference", asdict(self.first))
        self.reject(ErrorCode.INVALID, lambda: self.store.active_profile(self.owner))
        self.reject(
            ErrorCode.CONFLICT,
            lambda: self.store.save_profile(self.owner, self.first, None, "ref-first"),
        )

        self.store.activate_profile(self.owner, "first", 1)
        saved = self.store.save_profile(
            self.owner, replace(self.first, name="Renamed"), 1, "new-reference"
        )
        self.assertEqual(saved.configuration_revision, 2)
        self.reject(
            ErrorCode.CONFLICT,
            lambda: self.store.save_profile(self.owner, self.first, 1, "old-reference"),
        )
        self.reject(
            ErrorCode.CONFLICT, lambda: self.store.credential_reference(self.owner, "first", 1)
        )

        reopened = SQLiteStore(self.store.database_path)
        self.assertEqual(reopened.active_profile(self.owner), saved)
        self.assertEqual(
            reopened.credential_reference(self.owner, "first", 2),
            "new-reference",
        )
        cleared = self.store.save_profile(self.owner, saved, 2, None)
        self.assertFalse(cleared.credential_present)
        self.reject(ErrorCode.INVALID, lambda: self.store.active_profile(self.owner))

    def test_owner_isolation(self):
        for call in (
            lambda: self.store.activate_profile(self.other, "first", 1),
            lambda: self.store.credential_reference(self.other, "first", 1),
            lambda: self.store.delete_profile(self.other, "first", 1, None),
            lambda: self.store.save_profile(
                self.other, replace(self.first, owner_id=self.other), 1, "ref"
            ),
        ):
            self.reject(ErrorCode.FORBIDDEN, call)
        self.assertIn(self.first, self.store.list_profiles(self.owner))

    def test_switch_and_delete_inactive_card(self):
        self.store.activate_profile(self.other, "bob", 1)
        self.store.activate_profile(self.owner, "first", 1)
        self.store.activate_profile(self.owner, "second", 1)
        self.store.activate_profile(self.owner, "second", 1)
        self.store.delete_profile(self.owner, "first", 1, None)

        self.assertEqual(self.store.active_profile(self.owner), self.second)
        self.assertEqual(self.store.active_profile(self.other), self.bob)

    def test_delete_active_with_replacement_or_disable(self):
        self.store.activate_profile(self.owner, "first", 1)
        self.store.delete_profile(self.owner, "first", 1, "second")
        self.assertEqual(self.store.active_profile(self.owner), self.second)
        self.assertNotIn(self.first, self.store.list_profiles(self.owner))

        self.store.delete_profile(self.owner, "second", 1, None)
        self.reject(ErrorCode.INVALID, lambda: self.store.active_profile(self.owner))

    def test_invalid_deletion_preserves_active_card(self):
        self.store.activate_profile(self.owner, "first", 1)
        cases = (
            (0, None, ErrorCode.CONFLICT),
            (1, "first", ErrorCode.INVALID),
            (1, "empty", ErrorCode.INVALID),
            (1, "missing", ErrorCode.FORBIDDEN),
            (1, "bob", ErrorCode.FORBIDDEN),
        )
        for revision, replacement, code in cases:
            self.reject(
                code, lambda: self.store.delete_profile(self.owner, "first", revision, replacement)
            )
            self.assertEqual(self.store.active_profile(self.owner), self.first)

    def test_failed_replacement_rolls_back_deletion(self):
        self.store.activate_profile(self.owner, "first", 1)
        with closing(sqlite3.connect(self.store.database_path)) as db:
            db.execute("""
                CREATE TRIGGER fail_replacement
                BEFORE UPDATE OF is_active ON provider_profiles
                WHEN NEW.id = 'second' AND NEW.is_active = 1
                BEGIN SELECT RAISE(ABORT, 'test failure'); END
            """)
            db.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_profile(self.owner, "first", 1, "second")

        reopened = SQLiteStore(self.store.database_path)
        self.assertEqual(reopened.active_profile(self.owner), self.first)
        self.assertIn(self.second, reopened.list_profiles(self.owner))
