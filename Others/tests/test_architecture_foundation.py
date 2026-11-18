import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from event_based.bootstrap import build as build_event_based
from layered.bootstrap import build as build_layered
from shared.codec import decode, load_json
from shared.commands import (
    AdoptCampaignUpdate,
    CreateCampaign,
    Login,
    PublishCampaign,
    RegisterAccount,
    SaveDraft,
    StartSession,
)
from shared.domain.knowledge import KnowledgePolicy
from shared.errors import ApplicationError, ErrorCode
from shared.gateway import OperationStatus
from shared.infrastructure.sqlite import SQLiteStore
from shared.models.ai import PlayerView
from shared.models.common import RequestContext, SecretInput
from shared.models.gameplay import AcceptedTurn
from shared.models.views import UpdatePreviewView
from shared.models.world import CampaignDraft
from shared.queries import Query, QueryKind


class ArchitectureFoundationTests(unittest.TestCase):
    def submit(self, app, context, command, request_id, expected=OperationStatus.COMPLETED):
        receipt = app.submit(context, command, request_id)
        deadline = monotonic() + 5

        while receipt.status in (
            OperationStatus.PENDING,
            OperationStatus.RUNNING,
        ):
            if monotonic() >= deadline:
                self.fail(f"Operation timed out: {request_id}")
            sleep(0.01)
            receipt = app.poll(context, receipt.operation_id)

        self.assertEqual(receipt.status, expected, receipt.message)

        return receipt.result

    def check_workflow(self, build):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = build(root)
            context = RequestContext("browser-a", None, "architecture-test")
            password = SecretInput("demo password")

            try:
                account = self.submit(app, context, RegisterAccount("Alice", password), "register")
                login = self.submit(app, context, Login("alice", password), "login")
                self.assertEqual(login.account, account)

                draft = self.submit(app, context, CreateCampaign("Story", "Summary"), "create")
                self.assertEqual(draft.owner_id, account.id)

                content = replace(draft.content, title="Edited story")
                saved = self.submit(app, context, SaveDraft(draft.id, 0, content), "save")
                self.assertEqual(saved.revision, 1)
                self.assertEqual(saved.content, content)
                store = SQLiteStore(root / "application.sqlite3")
                self.assertEqual(store.catalog(), ())

                # Incomplete drafts can be saved, but not published.
                self.submit(
                    app,
                    context,
                    PublishCampaign(draft.id, saved.revision),
                    "publish-incomplete",
                    OperationStatus.REJECTED,
                )
                self.assertEqual(store.catalog(), ())

                fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "thornmere.json"
                fixture = decode(
                    load_json(fixture_path.read_text(encoding="utf-8"))["draft"],
                    CampaignDraft,
                )
                saved = self.submit(
                    app,
                    context,
                    SaveDraft(draft.id, saved.revision, fixture.content),
                    "save-ready",
                )

                # An outdated editor revision must not publish.
                self.submit(
                    app,
                    context,
                    PublishCampaign(draft.id, saved.revision - 1),
                    "publish-stale",
                    OperationStatus.REJECTED,
                )
                self.assertEqual(store.catalog(), ())

                first = self.submit(
                    app,
                    context,
                    PublishCampaign(draft.id, saved.revision),
                    "publish-first",
                )
                self.assertEqual(first.version, 1)
                self.assertEqual(store.catalog(), (first,))

                saved = self.submit(
                    app,
                    context,
                    SaveDraft(
                        draft.id,
                        saved.revision,
                        replace(fixture.content, title="Updated campaign"),
                    ),
                    "save-update",
                )

                # Draft edits do not change the published catalog.
                self.assertEqual(store.catalog(), (first,))

                latest = self.submit(
                    app,
                    context,
                    PublishCampaign(draft.id, saved.revision),
                    "publish-second",
                )
                self.assertEqual(latest.version, 2)
                self.assertEqual(latest.title, "Updated campaign")
                self.assertEqual(store.catalog(), (latest,))
                self.assertEqual(
                    store.published(first.published_version_id).content,
                    fixture.content,
                )
                guest = RequestContext("guest-browser", None, "query-test")
                self.assertEqual(app.query(guest, Query(QueryKind.CATALOG)), (latest,))

                drafts = app.query(context, Query(QueryKind.MY_CAMPAIGNS))
                self.assertEqual(len(drafts), 1)
                self.assertEqual(drafts[0].id, draft.id)
                self.assertEqual(drafts[0].draft_revision, saved.revision)
                self.assertEqual(
                    drafts[0].published_version_id,
                    latest.published_version_id,
                )

                editor_query = Query(QueryKind.EDITOR_DRAFT, draft.id)
                self.assertEqual(app.query(context, editor_query), saved)

                # Anonymous visitors can browse, but cannot open drafts.
                for query in (Query(QueryKind.MY_CAMPAIGNS), editor_query):
                    with self.assertRaises(ApplicationError) as caught:
                        app.query(guest, query)
                    self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)

                # Another account cannot read Alice's draft.
                other = RequestContext("browser-b", None, "query-test")
                self.submit(app, other, RegisterAccount("Bob", password), "bob-register")
                self.submit(app, other, Login("Bob", password), "bob-login")
                self.assertEqual(app.query(other, Query(QueryKind.MY_CAMPAIGNS)), ())
                with self.assertRaises(ApplicationError) as caught:
                    app.query(other, editor_query)
                self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

                session = self.check_sessions(app, context, other, store, first)
            finally:
                app.close()

            reopened = SQLiteStore(root / "application.sqlite3")
            self.assertEqual(reopened.load_session(account.id, session.id), session)
            self.assertEqual(reopened.load_draft(account.id, draft.id), saved)
            self.assertEqual(reopened.catalog(), (latest,))
            self.assertEqual(
                reopened.published(first.published_version_id).content,
                fixture.content,
            )

            app = build(root)
            try:
                resumed = app.query(context, Query(QueryKind.RESUME_SESSION, session.id))
                self.assertEqual(resumed, KnowledgePolicy().player_view(session, (), ""))
            finally:
                app.close()

    def test_layered_workflow(self):
        self.check_workflow(build_layered)

    def test_event_based_workflow(self):
        self.check_workflow(build_event_based)

    def check_sessions(self, app, context, other, store, publication):
        for pc_id in ("cedric", "missing-character"):
            self.submit(
                app,
                context,
                StartSession(publication.published_version_id, pc_id),
                f"invalid-pc-{pc_id}",
                OperationStatus.REJECTED,
            )

        command = StartSession(publication.published_version_id, "rowan")
        view = self.submit(app, context, command, "start-first")
        second = self.submit(app, other, command, "start-second")
        self.assertIsInstance(view, PlayerView)
        self.assertNotEqual(view.session_id, second.session_id)
        self.assertEqual(view.revision, 0)

        facts = {fact.id: fact for fact in view.facts}
        self.assertTrue(
            {"opening", "background", "rowan-history", "wedding-chalice"} <= facts.keys()
        )
        self.assertFalse(
            {
                "cedric-private-secret",
                "tomas-private-report",
                "chalice-clue",
                "hidden-dagger",
                "antechamber-description",
                "rowan-mara-trust",
            }
            & facts.keys()
        )
        self.assertNotIn("SPOILER-CANARY", repr(view))
        self.assertIn("Steady", facts["rowan-composure"].text)

        owner = store.resolve_login(context.client_session_id)
        other_owner = store.resolve_login(other.client_session_id)
        state = store.load_session(owner.id, view.session_id)
        other_state = store.load_session(other_owner.id, second.session_id)

        self.assertEqual(state.base_version_id, publication.published_version_id)
        self.assertEqual(state.world, other_state.world)
        self.assertEqual(state.disclosed_ids, ())
        self.assertEqual(
            state.flag_values,
            tuple((flag.id, flag.initial) for flag in state.world.flags),
        )
        with self.assertRaises(ApplicationError) as caught:
            store.load_session(other_owner.id, state.id)
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

        # Moving rooms changes perception, but does not reveal hidden items.
        policy = KnowledgePolicy()
        moved = replace(
            state,
            world=replace(
                state.world,
                characters=tuple(
                    replace(character, location_id="antechamber")
                    if character.id == "rowan"
                    else character
                    for character in state.world.characters
                ),
            ),
        )
        moved_ids = {fact.id for fact in policy.player_view(moved, (), "").facts}
        self.assertIn("antechamber-description", moved_ids)
        self.assertNotIn("great-hall-description", moved_ids)
        self.assertNotIn("hidden-dagger", moved_ids)

        # This in-memory preview must not change the stored session.
        self.assertEqual(store.load_session(owner.id, state.id), state)

        foreign_turn = AcceptedTurn(
            "turn",
            second.session_id,
            "operation",
            1,
            "private input",
            "private reply",
        )
        with self.assertRaises(ApplicationError) as caught:
            policy.player_view(state, (foreign_turn,), "")
        self.assertEqual(caught.exception.code, ErrorCode.INVALID)

        summaries = app.query(context, Query(QueryKind.MY_SESSIONS))
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary.id, state.id)
        self.assertEqual(summary.campaign_title, publication.title)
        self.assertEqual(summary.base_version_id, state.base_version_id)
        self.assertEqual(summary.revision, 0)
        self.assertTrue(summary.update_available)
        self.assertIsNotNone(summary.latest_version_id)
        preview = app.query(
            context,
            Query(QueryKind.UPDATE_PREVIEW, state.id, summary.latest_version_id),
        )
        self.assertIsInstance(preview, UpdatePreviewView)
        self.assertTrue(preview.can_adopt)
        self.submit(
            app,
            context,
            AdoptCampaignUpdate(state.id, summary.latest_version_id, state.revision),
            "adopt-compatible-update",
        )
        state = store.load_session(owner.id, state.id)
        self.assertEqual(state.base_version_id, summary.latest_version_id)
        adopted_summary = next(
            item for item in app.query(context, Query(QueryKind.MY_SESSIONS)) if item.id == state.id
        )
        self.assertFalse(adopted_summary.update_available)

        other_summaries = app.query(other, Query(QueryKind.MY_SESSIONS))
        self.assertEqual([item.id for item in other_summaries], [second.session_id])

        resume = Query(QueryKind.RESUME_SESSION, state.id)
        self.assertEqual(app.query(context, resume), view)
        self.assertEqual(view.accepted_history, ())

        guest = RequestContext("guest-browser", None, "resume-test")
        for caller, query, expected in (
            (other, resume, ErrorCode.FORBIDDEN),
            (guest, resume, ErrorCode.UNAUTHENTICATED),
            (guest, Query(QueryKind.MY_SESSIONS), ErrorCode.UNAUTHENTICATED),
            (context, Query(QueryKind.RESUME_SESSION), ErrorCode.INVALID),
            (
                context,
                Query(QueryKind.RESUME_SESSION, "missing-session"),
                ErrorCode.FORBIDDEN,
            ),
        ):
            with self.assertRaises(ApplicationError) as caught:
                app.query(caller, query)
            self.assertEqual(caught.exception.code, expected)

        # A session on the latest publication needs no update.
        latest = store.catalog()[0]
        current = self.submit(
            app,
            context,
            StartSession(latest.published_version_id, "rowan"),
            "start-current",
        )
        current_summary = next(
            item
            for item in app.query(context, Query(QueryKind.MY_SESSIONS))
            if item.id == current.session_id
        )
        self.assertFalse(current_summary.update_available)

        return state
