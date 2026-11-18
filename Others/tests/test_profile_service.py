import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from event_based.bootstrap import build as build_event
from layered.bootstrap import build as build_layered
from shared.commands import (
    ActivateProviderCard,
    DeleteProviderCard,
    Login,
    RegisterAccount,
    SaveProviderCard,
)
from shared.domain.profiles import ProfileService
from shared.errors import ApplicationError, ErrorCode
from shared.infrastructure.sqlite import SQLiteStore
from shared.models.common import RequestContext, SecretInput
from shared.operation_status import OperationStatus
from shared.queries import Query, QueryKind


class FakeVault:
    def __init__(self):
        self.values = {}

    def write(self, owner_id, reference, value):
        self.values[owner_id, reference] = value

    def read(self, owner_id, reference):
        if (owner_id, reference) not in self.values:
            raise ApplicationError(ErrorCode.INVALID, "API key not found")
        return self.values[owner_id, reference]

    def remove(self, owner_id, reference):
        self.values.pop((owner_id, reference), None)


class ProfileServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = SQLiteStore(self.root / "profiles.sqlite3")
        self.store.initialize()
        self.owner = self.store.create_account("Alice", "test-hash").id
        self.other = self.store.create_account("Bob", "test-hash").id
        self.vault = FakeVault()
        self.service = ProfileService(self.store, self.vault)
        self.command = SaveProviderCard(
            None,
            None,
            "Demo",
            "https://example.test/v1/chat/completions",
            "model",
            "chat-completions-compatible",
            SecretInput("FAKE-KEY"),
        )

    def reject(self, code, call):
        with self.assertRaises(ApplicationError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_save_preserve_and_rebind_key(self):
        first = self.service.save(self.owner, self.command)
        reference = self.store.credential_reference(self.owner, first.id, 1)
        edit = replace(
            self.command,
            profile_id=first.id,
            expected_revision=1,
            name="Renamed",
            credential=None,
        )
        self.service.save(self.owner, edit)
        self.assertEqual(self.store.credential_reference(self.owner, first.id, 2), reference)
        self.reject(ErrorCode.CONFLICT, lambda: self.service.save(self.owner, edit))
        changed = replace(edit, expected_revision=2, endpoint="https://other.test/chat")
        self.reject(ErrorCode.INVALID, lambda: self.service.save(self.owner, changed))
        saved = self.service.save(self.owner, replace(changed, credential=SecretInput("NEW-KEY")))
        self.assertEqual(saved.configuration_revision, 3)
        self.assertNotIn((self.owner, reference), self.vault.values)
        self.assertNotIn("NEW-KEY", str(asdict(saved)))
        self.reject(ErrorCode.INVALID, lambda: self.service.capture(self.owner))

    def test_ownership_validation_and_missing_key(self):
        card = self.service.save(self.owner, self.command)
        self.reject(
            ErrorCode.FORBIDDEN,
            lambda: self.service.activate(self.other, ActivateProviderCard(card.id, 1)),
        )
        self.reject(
            ErrorCode.FORBIDDEN,
            lambda: self.service.delete(self.other, DeleteProviderCard(card.id, 1)),
        )
        self.reject(
            ErrorCode.INVALID,
            lambda: self.service.save(
                self.owner, replace(self.command, endpoint="http://example.test/chat")
            ),
        )
        self.vault.values.clear()
        self.reject(
            ErrorCode.INVALID,
            lambda: self.service.activate(self.owner, ActivateProviderCard(card.id, 1)),
        )
        self.reject(ErrorCode.INVALID, lambda: self.service.capture(self.owner))

    def test_delete_replace_and_preserve_existing_capture(self):
        first = self.service.save(self.owner, self.command)
        second = self.service.save(
            self.owner, replace(self.command, credential=SecretInput("SECOND-KEY"))
        )
        self.service.activate(self.owner, ActivateProviderCard(first.id, 1))
        captured = self.service.capture(self.owner)
        reference = self.store.credential_reference(self.owner, first.id, 1)
        self.service.delete(self.owner, DeleteProviderCard(first.id, 1, second.id))
        self.assertEqual(self.service.capture(self.owner).profile, second)
        self.assertEqual(captured.credential.value, "FAKE-KEY")
        self.assertNotIn((self.owner, reference), self.vault.values)
        self.service.delete(self.owner, DeleteProviderCard(second.id, 1))
        self.reject(ErrorCode.INVALID, lambda: self.service.capture(self.owner))

    def test_failed_database_save_removes_staged_key(self):
        with patch.object(
            self.store,
            "save_profile",
            side_effect=ApplicationError(ErrorCode.CONFLICT, "Changed"),
        ):
            self.reject(
                ErrorCode.CONFLICT,
                lambda: self.service.save(self.owner, self.command),
            )
        self.assertEqual(self.vault.values, {})
        self.assertEqual(self.store.list_profiles(self.owner), ())

    def test_retirement_failure_does_not_reverse_saved_metadata(self):
        first = self.service.save(self.owner, self.command)
        edit = replace(
            self.command,
            profile_id=first.id,
            expected_revision=1,
            credential=SecretInput("NEW-KEY"),
        )
        with (
            patch.object(
                self.vault,
                "remove",
                side_effect=ApplicationError(ErrorCode.STORAGE, "Vault unavailable"),
            ),
            self.assertLogs("shared.domain.profiles", level="WARNING"),
        ):
            saved = self.service.save(self.owner, edit)
        self.assertEqual(saved.configuration_revision, 2)
        self.assertEqual(self.store.list_profiles(self.owner), (saved,))
        reference = self.store.credential_reference(self.owner, first.id, 2)
        self.assertEqual(self.vault.read(self.owner, reference).value, "NEW-KEY")

    def test_card_queries_in_both_architectures(self):
        for name, build in (("layered", build_layered), ("event", build_event)):
            with (
                self.subTest(architecture=name),
                patch("shared.wiring.LocalCredentialStore", return_value=FakeVault()),
            ):
                app = (
                    build(self.root / name)
                    if name == "layered"
                    else build(self.root / name, start_worker=False)
                )
                try:
                    alice = RequestContext("alice-browser", None, "test")
                    bob = RequestContext("bob-browser", None, "test")
                    guest = RequestContext("guest-browser", None, "test")

                    def submit(context, command, request_id):
                        receipt = app.submit(context, command, request_id)
                        if name == "event":
                            self.assertEqual(receipt.status, OperationStatus.PENDING)
                            app.bus.dispatch_one()
                            receipt = app.poll(context, receipt.operation_id)
                        self.assertEqual(receipt.status, OperationStatus.COMPLETED, receipt.message)
                        return receipt.result

                    for context, username in ((alice, "Alice"), (bob, "Bob")):
                        password = SecretInput("demo password")
                        submit(context, RegisterAccount(username, password), f"register-{username}")
                        submit(context, Login(username, password), f"login-{username}")

                    card = submit(alice, self.command, "save-card")
                    query = Query(QueryKind.PROVIDER_CARDS)
                    self.assertEqual(app.query(alice, query), (card,))
                    self.assertEqual(app.query(bob, query), ())
                    self.reject(ErrorCode.UNAUTHENTICATED, lambda: app.query(guest, query))
                    fields = asdict(app.query(alice, query)[0])
                    self.assertNotIn("credential_reference", fields)
                    self.assertNotIn("FAKE-KEY", str(fields))
                    submit(alice, ActivateProviderCard(card.id, 1), "activate-card")
                    submit(alice, DeleteProviderCard(card.id, 1), "delete-card")
                    self.assertEqual(app.query(alice, query), ())
                finally:
                    app.close()
