import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from shared.commands import CreateCampaign, Login, RegisterAccount, SaveDraft
from shared.domain.accounts import AccountService
from shared.domain.world import WorldService
from shared.errors import ApplicationError, ErrorCode
from shared.infrastructure.security import ScryptPasswordHasher
from shared.infrastructure.sqlite import SQLiteStore
from shared.models.common import RequestContext, SecretInput


class FoundationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "application.sqlite3"

        self.store = SQLiteStore(self.path)
        self.store.initialize()
        self.accounts = AccountService(self.store, ScryptPasswordHasher())
        self.world = WorldService(self.store)

        self.context = RequestContext("browser-a", None, "test")
        self.password = SecretInput("demo password")
        self.account = self.accounts.register(self.context, RegisterAccount("Alice", self.password))

    def test_workflow_survives_reopening(self):
        login = self.accounts.login(self.context, Login("alice", self.password))
        self.assertEqual(login.account, self.account)
        self.assertEqual(self.accounts.require_user(self.context), self.account)

        draft = self.world.create(self.account.id, CreateCampaign("Story", "Summary"))
        content = replace(draft.content, title="Edited story")
        saved = self.world.save(self.account.id, SaveDraft(draft.id, 0, content))
        self.assertEqual(saved.revision, 1)

        reopened = SQLiteStore(self.path)
        reopened.initialize()
        self.assertEqual(reopened.load_draft(self.account.id, draft.id), saved)
        self.assertEqual(
            reopened.drafts_for_owner(self.account.id)[0].title,
            "Edited story",
        )

        accounts = AccountService(reopened, ScryptPasswordHasher())
        self.assertEqual(accounts.require_user(self.context), self.account)
        accounts.logout(self.context)

        with self.assertRaises(ApplicationError) as caught:
            accounts.require_user(self.context)
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)

    def test_duplicate_username_and_wrong_password(self):
        with self.assertRaises(ApplicationError) as caught:
            self.accounts.register(self.context, RegisterAccount(" ALICE ", self.password))
        self.assertEqual(caught.exception.code, ErrorCode.CONFLICT)

        with self.assertRaises(ApplicationError) as caught:
            self.accounts.login(self.context, Login("Alice", SecretInput("wrong")))
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
        self.assertIsNone(self.store.resolve_login(self.context.client_session_id))

    def test_rejected_saves_preserve_the_draft(self):
        draft = self.world.create(self.account.id, CreateCampaign("Story", "Summary"))
        saved = self.world.save(
            self.account.id,
            SaveDraft(
                draft.id,
                0,
                replace(draft.content, title="Latest title"),
            ),
        )

        with self.assertRaises(ApplicationError) as caught:
            self.world.save(self.account.id, SaveDraft(draft.id, 0, draft.content))
        self.assertEqual(caught.exception.code, ErrorCode.CONFLICT)

        with self.assertRaises(ApplicationError) as caught:
            self.world.save("another-account", SaveDraft(draft.id, 1, draft.content))
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

        self.assertEqual(self.store.load_draft(self.account.id, draft.id), saved)
        self.assertEqual(self.store.drafts_for_owner("another-account"), ())
