import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from webui import scripts, server


ROOT = Path(__file__).resolve().parents[1]


class NexaCardWebUITests(unittest.TestCase):
    def test_schema_exposes_secret_credentials_and_polling_defaults(self):
        group = next(group for group in scripts.ENV_SCHEMA if group["group"] == "NexaCard OTP")
        items = {item["key"]: item for item in group["items"]}

        self.assertTrue(items["NEXACARD_ACCOUNT"]["required"])
        self.assertTrue(items["NEXACARD_PASSWORD"]["required"])
        self.assertTrue(items["NEXACARD_PASSWORD"]["secret"])
        self.assertTrue(items["NEXACARD_VERIFICATION_EMAIL"]["gmail_oauth"])
        self.assertEqual(items["NEXACARD_HEADLESS"]["default"], "true")
        self.assertEqual(items["NEXACARD_PAGE_TIMEZONE"]["default"], "Asia/Shanghai")
        self.assertEqual(items["NEXACARD_OTP_POLL_INTERVAL_SECONDS"]["default"], 3)
        self.assertEqual(items["NEXACARD_OTP_MAX_ATTEMPTS"]["default"], 100)
        self.assertEqual(items["NEXACARD_SERVICE_HOST"]["default"], "127.0.0.1")
        self.assertEqual(items["NEXACARD_SERVICE_PORT"]["default"], 8811)

    def test_service_is_available_in_script_launcher(self):
        entry = next(item for item in scripts.SCRIPTS if item["id"] == "nexacard_otp_service")
        self.assertEqual(entry["file"], "nexacard_otp_service.py")
        self.assertEqual(entry["args"], [])

    def test_env_api_preserves_gmail_oauth_and_numeric_item_metadata(self):
        response = TestClient(server.app).get("/api/env")
        self.assertEqual(response.status_code, 200)
        group = next(group for group in response.json()["groups"] if group["group"] == "NexaCard OTP")
        items = {item["key"]: item for item in group["items"]}

        self.assertIs(items["NEXACARD_VERIFICATION_EMAIL"]["gmail_oauth"], True)
        self.assertIs(items["NEXACARD_ACCOUNT"]["gmail_oauth"], False)
        self.assertIs(items["NEXACARD_PASSWORD"]["gmail_oauth"], False)
        self.assertEqual(items["NEXACARD_OTP_POLL_INTERVAL_SECONDS"]["type"], "number")
        self.assertEqual(items["NEXACARD_OTP_MAX_ATTEMPTS"]["type"], "int")

    def test_connectivity_check_uses_configured_local_service(self):
        client = TestClient(server.app)
        with patch.object(
            server,
            "_read_config_val",
            side_effect=lambda key, default="": "127.0.0.1" if key.endswith("HOST") else "8811",
        ), patch.object(server, "_http_alive", return_value=True) as alive:
            response = client.post("/api/test/nexacard", json={"env": {}})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("127.0.0.1:8811/health", response.json()["msg"])
        alive.assert_called_once_with("http://127.0.0.1:8811/health")

    def test_static_assets_use_safe_oauth_rendering_and_popup_flow(self):
        script = (ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        style = (ROOT / "webui" / "static" / "style.css").read_text(encoding="utf-8")

        for control in ("Google 鉴权", "重新鉴权", "检测状态"):
            self.assertIn(control, script)
        self.assertIn("oauth-actions", style)
        self.assertIn("textContent", script)
        self.assertIn("window.open('about:blank'", script)
        self.assertIn("popup.close()", script)
        self.assertIn("if(!email)", script)
        self.assertIn("NexaCardWebUi.loadOauthStatus", script)
        self.assertNotIn("authorization_url, '_blank'", script)
        self.assertNotIn("innerHTML = `\n    <button type=\"button\" data-oauth-action", script)

    def test_oauth_status_controls_are_created_once_per_rendered_email_row(self):
        script = (ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("NexaCardWebUi.shouldRenderGoogleOauthActions(it)", script)
        self.assertIn("row.querySelector('.oauth-actions')", script)
        self.assertIn("addEventListener('click'", script)


if __name__ == "__main__":
    unittest.main()
