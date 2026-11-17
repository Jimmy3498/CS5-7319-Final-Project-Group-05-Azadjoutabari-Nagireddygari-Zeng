import unittest
from tempfile import TemporaryDirectory
from time import monotonic, sleep


def profile_page(root, architecture):
    import importlib
    from pathlib import Path
    from time import monotonic, sleep
    from unittest.mock import patch

    import streamlit as st
    from test_profile_service import FakeVault

    from shared.commands import Login, RegisterAccount
    from shared.gateway import OperationStatus
    from shared.models.common import RequestContext, SecretInput
    from shared.ui.common import UIContext
    from shared.ui.profiles import profiles

    if "test_gateway" not in st.session_state:
        build = importlib.import_module(f"{architecture}.bootstrap").build
        with patch("shared.wiring.LocalCredentialStore", return_value=FakeVault()):
            app = build(Path(root))
        context = RequestContext("profile-ui", None, "profile-ui")
        for request_id, command in (
            ("register", RegisterAccount("Alice", SecretInput("demo password"))),
            ("login", Login("Alice", SecretInput("demo password"))),
        ):
            receipt = app.submit(context, command, request_id)
            deadline = monotonic() + 5
            while receipt.status in {OperationStatus.PENDING, OperationStatus.RUNNING}:
                if monotonic() >= deadline:
                    raise AssertionError("Account setup timed out")
                sleep(0.01)
                receipt = app.poll(context, receipt.operation_id)
            assert receipt.status is OperationStatus.COMPLETED, receipt.message
        st.session_state["test_gateway"] = app
        st.session_state["test_context"] = context
    profiles(UIContext(st.session_state["test_gateway"], st.session_state["test_context"]))


class ProfileUITests(unittest.TestCase):
    def settle(self, app):
        deadline = monotonic() + 5
        while True:
            self.assertFalse(app.exception, str(app.exception))
            refresh = next((b for b in app.button if b.label == "Refresh status"), None)
            if refresh is None:
                return app
            self.assertLess(monotonic(), deadline, "Profile operation timed out")
            sleep(0.01)
            app = refresh.click().run(timeout=20)

    def test_save_edit_delete_and_create_again_in_both_architectures(self):
        from streamlit.testing.v1 import AppTest

        for architecture in ("layered", "event_based"):
            with self.subTest(architecture=architecture), TemporaryDirectory() as directory:
                app = AppTest.from_function(profile_page, args=(directory, architecture))
                app = app.run(timeout=20)
                try:
                    self.assertFalse(app.exception, str(app.exception))
                    for title in ("First card", "Second card"):
                        next(x for x in app.text_input if x.label == "Card name").set_value(title)
                        app = (
                            next(b for b in app.button if b.label == "Save card")
                            .click()
                            .run(timeout=20)
                        )
                        app = self.settle(app)
                        selected = app.selectbox(key="ui.profiles.selected").value
                        self.assertIsNotNone(
                            selected,
                            title
                            + str(
                                [
                                    (x.type, x.value)
                                    for group in (app.error, app.warning, app.info, app.success)
                                    for x in group
                                ]
                            ),
                        )
                        self.assertEqual(
                            next(x.value for x in app.text_input if x.label == "Card name"), title
                        )

                        next(x for x in app.text_input if x.label == "Card name").set_value(
                            title + " edited"
                        )
                        app = (
                            next(b for b in app.button if b.label == "Save card")
                            .click()
                            .run(timeout=20)
                        )
                        app = self.settle(app)
                        self.assertEqual(app.selectbox(key="ui.profiles.selected").value, selected)
                        self.assertEqual(
                            next(x.value for x in app.text_input if x.label == "Card name"),
                            title + " edited",
                        )

                        app = (
                            next(b for b in app.button if b.label == "Delete card")
                            .click()
                            .run(timeout=20)
                        )
                        app = self.settle(app)
                        self.assertIsNone(app.selectbox(key="ui.profiles.selected").value)
                        app = app.run(timeout=20)
                        self.assertFalse(app.exception, str(app.exception))
                        self.assertEqual(
                            app.selectbox(key="ui.profiles.selected").options, ["Create a new card"]
                        )
                finally:
                    if "test_gateway" in app.session_state:
                        app.session_state["test_gateway"].close()
