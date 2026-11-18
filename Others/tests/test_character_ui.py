import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from event_based.bootstrap import build as build_events
from layered.bootstrap import build as build_layered
from shared.codec import decode, load_json
from shared.commands import CreateCampaign, Login, PublishCampaign, RegisterAccount, SaveDraft
from shared.domain.knowledge import KnowledgePolicy
from shared.domain.world import WorldService
from shared.gateway import OperationStatus
from shared.models.common import RequestContext, SecretInput
from shared.models.gameplay import SessionSnapshot
from shared.models.world import CampaignContent
from shared.queries import Query, QueryKind


def character_page():
    from pathlib import Path

    import streamlit as st

    from shared.codec import decode, load_json
    from shared.models.world import CampaignContent, EditorBuffer
    from shared.ui.editor import BUFFER_KEY, _characters_tab

    if BUFFER_KEY not in st.session_state:
        content = decode(
            load_json(Path("fixtures/thornmere-video.json").read_text())["draft"]["content"],
            CampaignContent,
        )
        st.session_state[BUFFER_KEY] = EditorBuffer("demo", 0, 0, content)
    _characters_tab(st.session_state[BUFFER_KEY])


class CharacterUITests(unittest.TestCase):
    def test_video_campaign_saves_and_publishes_through_both_gateways(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/thornmere-video.json"
        content = decode(load_json(source.read_text())["draft"]["content"], CampaignContent)
        for build in (build_layered, build_events):
            with self.subTest(architecture=build.__module__), TemporaryDirectory() as directory:
                app = build(Path(directory))
                context = RequestContext("video-test", None, "video-test")

                def submit(command, request):
                    receipt = app.submit(context, command, request)
                    deadline = monotonic() + 5
                    while receipt.status in (OperationStatus.PENDING, OperationStatus.RUNNING):
                        self.assertLess(monotonic(), deadline)
                        sleep(0.01)
                        receipt = app.poll(context, receipt.operation_id)
                    self.assertEqual(receipt.status, OperationStatus.COMPLETED, receipt.message)
                    return receipt.result

                try:
                    password = SecretInput("test password")
                    submit(RegisterAccount("Author", password), "register")
                    submit(Login("Author", password), "login")
                    draft = submit(CreateCampaign("Video", "Summary"), "create")
                    saved = submit(SaveDraft(draft.id, 0, content), "save")
                    self.assertEqual(
                        app.query(context, Query(QueryKind.EDITOR_DRAFT, draft.id)).content, content
                    )
                    published = submit(PublishCampaign(draft.id, saved.revision), "publish")
                    self.assertEqual(published.title, content.title)
                finally:
                    app.close()

    def test_select_preview_update_and_add_preserve_existing_records(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_function(character_page).run(timeout=20)
        self.assertFalse(app.exception)
        original = app.session_state["ui.editor.buffer"]
        for character in original.content.world.characters:
            app = app.selectbox(key="editor_character_demo").select(character.id).run(timeout=20)
            self.assertFalse(app.exception, str(app.exception))
            self.assertEqual(
                next(x.value for x in app.text_input if x.label == "Character name"), character.name
            )
            for label, field in (
                ("Appearance", "appearance"),
                ("Personality", "personality"),
                ("Personal history", "personal_history"),
                ("Expression style", "expression_style"),
            ):
                self.assertEqual(
                    next(x.value for x in app.text_area if x.label == label),
                    getattr(character, field).text,
                )

        # Cedric's private fields must not become public or disappear when editing text.
        selected = original.content.world.characters[-1]
        next(x for x in app.text_input if x.label == "Character name").set_value(
            "Cedric Fen edited"
        )
        next(x for x in app.text_area if x.label == "Appearance").set_value(
            "An updated coat description."
        )
        app = next(b for b in app.button if b.label == "Update character").click().run(timeout=20)
        self.assertFalse(app.exception, str(app.exception))
        buffer = app.session_state["ui.editor.buffer"]
        expected = replace(
            selected,
            name="Cedric Fen edited",
            appearance=replace(selected.appearance, text="An updated coat description."),
        )
        self.assertEqual(buffer.content.world.characters[-1], expected)
        self.assertEqual(
            buffer.content.world.characters[:-1], original.content.world.characters[:-1]
        )
        self.assertEqual((buffer.persisted_revision, buffer.local_revision), (0, 1))
        self.assertEqual(app.selectbox(key="editor_character_demo").value, selected.id)

        app = app.selectbox(key="editor_character_demo").select(None).run(timeout=20)
        self.assertEqual(next(x.value for x in app.text_input if x.label == "Character name"), "")
        next(x for x in app.text_input if x.label == "Character name").set_value("New guest")
        app = next(b for b in app.button if b.label == "Add character").click().run(timeout=20)
        self.assertFalse(app.exception, str(app.exception))
        after = app.session_state["ui.editor.buffer"]
        self.assertEqual(len(after.content.world.characters), 5)
        self.assertEqual(after.content.world.characters[:4], buffer.content.world.characters)
        self.assertEqual(after.local_revision, 2)

    def test_video_fixture_is_ready_and_private_knowledge_stays_private(self):
        root = Path(__file__).resolve().parents[1] / "fixtures"
        base = decode(
            load_json((root / "thornmere.json").read_text())["draft"]["content"], CampaignContent
        )
        content = decode(
            load_json((root / "thornmere-video.json").read_text())["draft"]["content"],
            CampaignContent,
        )
        self.assertEqual(WorldService(None).readiness(content), ())
        self.assertEqual(content.world.items, base.world.items)
        self.assertEqual(content.world.flags, base.world.flags)
        self.assertEqual(content.world.milestones, base.world.milestones)
        for old, new in zip(base.world.characters, content.world.characters, strict=True):
            self.assertEqual(
                (old.id, old.playable, old.location_id, old.stats),
                (new.id, new.playable, new.location_id, new.stats),
            )
            for field in ("appearance", "personality", "personal_history", "expression_style"):
                self.assertEqual(
                    replace(getattr(new, field), text=getattr(old, field).text), getattr(old, field)
                )
        state = SessionSnapshot(
            "demo",
            "owner",
            "campaign",
            "version",
            0,
            "rowan",
            content.world,
            (),
            tuple((f.id, f.initial) for f in content.world.flags),
            (content.background, content.opening),
        )
        view = KnowledgePolicy().player_view(state, (), "")
        self.assertNotIn("SPOILER-CANARY-CEDRIC", str(view))
        self.assertNotIn("hidden-dagger", {fact.id for fact in view.facts})
