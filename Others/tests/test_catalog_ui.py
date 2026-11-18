import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch

from event_based.bootstrap import build as build_events
from layered.bootstrap import build as build_layered
from shared.codec import decode, load_json
from shared.commands import CreateCampaign, Login, PublishCampaign, RegisterAccount, SaveDraft
from shared.gateway import OperationStatus
from shared.models.common import RequestContext, SecretInput
from shared.models.world import CampaignDraft
from shared.queries import Query, QueryKind


def catalog_app(label):
    import streamlit as st

    from shared.ui.app import run

    st.session_state.setdefault("_client_session_id", "catalog-test")
    run(label, lambda: None)  # The test supplies the real, temporary gateway through _host.


class CatalogUITests(unittest.TestCase):
    def submit(self, gateway, context, command, request_id):
        receipt = gateway.submit(context, command, request_id)
        deadline = monotonic() + 5
        while receipt.status in {OperationStatus.PENDING, OperationStatus.RUNNING}:
            self.assertLess(monotonic(), deadline, "Setup operation timed out")
            sleep(0.01)
            receipt = gateway.poll(context, receipt.operation_id)
        self.assertEqual(receipt.status, OperationStatus.COMPLETED, receipt.message)
        return receipt.result

    def test_start_navigates_to_new_session_once_in_both_architectures(self):
        from streamlit.testing.v1 import AppTest

        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "thornmere.json"
        content = decode(load_json(fixture.read_text())["draft"], CampaignDraft).content
        for label, build in (("Layered", build_layered), ("Event-Based", build_events)):
            with self.subTest(architecture=label), TemporaryDirectory() as directory:
                gateway = build(Path(directory))
                try:
                    context = RequestContext("catalog-test", None, "catalog-test")
                    password = SecretInput("demo password")
                    self.submit(gateway, context, RegisterAccount("Alice", password), "register")
                    self.submit(gateway, context, Login("Alice", password), "login")
                    draft = self.submit(
                        gateway, context, CreateCampaign("Story", "Summary"), "create"
                    )
                    saved = self.submit(gateway, context, SaveDraft(draft.id, 0, content), "save")
                    self.submit(
                        gateway, context, PublishCampaign(draft.id, saved.revision), "publish"
                    )

                    with patch("shared.ui.app._host", return_value=gateway):
                        app = AppTest.from_function(catalog_app, args=(label,)).run(timeout=20)
                        previous_ids = set()
                        for count in (1, 2):
                            app = (
                                app.radio(key="page").set_value("Browse Campaigns").run(timeout=20)
                            )
                            self.assertFalse(app.exception, str(app.exception))
                            app = (
                                next(b for b in app.button if b.label == "Start playthrough")
                                .click()
                                .run(timeout=20)
                            )
                            deadline = monotonic() + 5
                            while app.radio(key="page").value != "My Sessions / Play":
                                self.assertFalse(app.exception, str(app.exception))
                                self.assertLess(
                                    monotonic(), deadline, "Session navigation timed out"
                                )
                                refresh = next(b for b in app.button if b.label == "Refresh status")
                                sleep(0.01)
                                app = refresh.click().run(timeout=20)
                            self.assertFalse(app.exception, str(app.exception))
                            sessions = gateway.query(context, Query(QueryKind.MY_SESSIONS))
                            ids = {s.id for s in sessions}
                            self.assertEqual(len(ids), count)
                            self.assertEqual(len(ids - previous_ids), 1)
                            self.assertEqual(
                                app.selectbox(key="ui.play.session_id").value,
                                (ids - previous_ids).pop(),
                            )
                            self.assertTrue(
                                any("Session revision 0" in x.value for x in app.caption)
                            )
                            self.assertNotIn("ui.next_page", app.session_state)
                            app = app.run(timeout=20)
                            self.assertFalse(app.exception, str(app.exception))
                            self.assertEqual(
                                len(gateway.query(context, Query(QueryKind.MY_SESSIONS))), count
                            )
                            previous_ids = ids
                finally:
                    gateway.close()
