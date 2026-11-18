import importlib.util
import unittest
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
HAS_STREAMLIT = importlib.util.find_spec("streamlit") is not None


@unittest.skipUnless(HAS_STREAMLIT, "Install requirements.txt for Streamlit AppTest")
class UIIntegrationTests(unittest.TestCase):
    def settle(self, app, timeout=5):
        deadline = monotonic() + timeout
        while True:
            refresh = next(
                (button for button in app.button if button.label == "Refresh status"), None
            )
            if refresh is None:
                return app
            if monotonic() >= deadline:
                self.fail("UI operation did not complete")
            sleep(0.02)
            app = refresh.click().run(timeout=20)
            self.assertEqual(len(app.exception), 0, str(app.exception))

    def test_all_pages_render_in_both_entrypoints(self):
        from streamlit.testing.v1 import AppTest

        from shared.ui.shell import PAGES

        for architecture in ("layered", "event_based"):
            app = AppTest.from_file(str(ROOT / architecture / "streamlit_app.py")).run(timeout=20)
            self.assertEqual(len(app.exception), 0, str(app.exception))
            for page in PAGES:
                with self.subTest(architecture=architecture, page=page):
                    app = app.radio[0].set_value(page).run(timeout=20)
                    self.assertEqual(len(app.exception), 0, str(app.exception))

    def test_account_workflow_in_both_architectures(self):
        from streamlit.testing.v1 import AppTest

        for package, label in (("layered", "Layered"), ("event_based", "Event-Based")):
            with self.subTest(architecture=label):
                username = f"ui-{package}-{uuid4().hex[:10]}"
                password = "demo password"
                app = AppTest.from_file(str(ROOT / package / "streamlit_app.py")).run(timeout=20)
                self.assertTrue(any(c.value == f"{label} architecture" for c in app.caption))
                self.assertFalse(any(widget.label == "Architecture" for widget in app.selectbox))
                self.assertNotIn("Account", app.radio[0].options)

                app.text_input("register_username").set_value(username)
                app.text_input("register_password").set_value(password)
                app = (
                    app.button(key="FormSubmitter:register_form-Create account")
                    .click()
                    .run(timeout=20)
                )
                app = self.settle(app)
                self.assertTrue(any("created" in str(value.value) for value in app.success))

                app.text_input("login_username").set_value(username)
                app.text_input("login_password").set_value(password)
                app = app.button(key="FormSubmitter:login_form-Sign in").click().run(timeout=20)
                app = self.settle(app)
                account_menu = app.get("menu_button")
                self.assertEqual([menu.label for menu in account_menu], [username])
                self.assertFalse(any(button.label == "Sign in" for button in app.button))

                app = app.radio[0].set_value("API Cards").run(timeout=20)
                self.assertEqual(len(app.exception), 0, str(app.exception))
                preset = next(value for value in app.selectbox if value.label == "Provider preset")
                self.assertEqual(preset.value, "OpenAI")
                endpoint = next(
                    value for value in app.text_input if value.label == "Full provider endpoint"
                )
                self.assertEqual(endpoint.value, "https://api.openai.com/v1/chat/completions")

                account_menu = app.get("menu_button")
                app = account_menu[0].set_value("Sign out").run(timeout=20)
                app = self.settle(app)
                self.assertTrue(any(button.label == "Sign in" for button in app.button))

    def test_layered_user_can_create_campaign_from_editor(self):
        from streamlit.testing.v1 import AppTest

        username = f"ui-editor-{uuid4().hex[:10]}"
        password = "demo password"
        app = AppTest.from_file(str(ROOT / "layered" / "streamlit_app.py")).run(timeout=20)
        app.text_input("register_username").set_value(username)
        app.text_input("register_password").set_value(password)
        app = app.button(key="FormSubmitter:register_form-Create account").click().run(timeout=20)
        app = self.settle(app)
        app.text_input("login_username").set_value(username)
        app.text_input("login_password").set_value(password)
        app = app.button(key="FormSubmitter:login_form-Sign in").click().run(timeout=20)
        app = self.settle(app)

        app = app.radio[0].set_value("My Campaigns / Editor").run(timeout=20)
        title = f"UI Campaign {uuid4().hex[:6]}"
        campaign_title = next(value for value in app.text_input if value.label == "Campaign title")
        campaign_title.set_value(title)
        next(value for value in app.text_area if value.label == "Initial summary").set_value(
            "Created through Streamlit AppTest"
        )
        app = (
            next(button for button in app.button if button.label == "Create draft")
            .click()
            .run(timeout=20)
        )
        app = self.settle(app)
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertTrue(
            any(
                value.label == "Campaign title" and value.value == title for value in app.text_input
            )
        )
        self.assertTrue(any("Saved revision" in str(value.value) for value in app.caption))

        edited_title = title + " Revised"
        setup_title = [value for value in app.text_input if value.label == "Campaign title"][-1]
        setup_title.set_value(edited_title)
        app = (
            next(button for button in app.button if button.label == "Apply setup changes")
            .click()
            .run(timeout=20)
        )
        self.assertTrue(any("unsaved local changes" in str(value.value) for value in app.warning))
        app = (
            next(button for button in app.button if button.label == "Save draft")
            .click()
            .run(timeout=20)
        )
        app = self.settle(app)
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertTrue(
            any("Saved revision 1 · local revision 0" in str(value.value) for value in app.caption)
        )


if __name__ == "__main__":
    unittest.main()
