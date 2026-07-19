import unittest
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from webui import scripts, server


ROOT = Path(__file__).resolve().parents[1]


class _JsonRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class NexaCardWebUITests(unittest.TestCase):
    def test_env_get_masks_every_schema_secret_including_nexacard_password(self):
        secret_keys = {
            item["key"]
            for group in scripts.ENV_SCHEMA
            for item in group["items"]
            if item.get("secret")
        }
        values = {key: f"private-{index}" for index, key in enumerate(secret_keys)}
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "".join(f"{key}={value}\n" for key, value in values.items()),
                encoding="utf-8",
            )
            with patch.object(server, "ENV_PATH", str(env_path)), patch.object(
                server, "ENV_EXAMPLE", str(Path(directory) / "missing.example")
            ):
                payload = server.api_env_get()

        items = {
            item["key"]: item
            for group in payload["groups"]
            for item in group["items"]
        }
        self.assertIn("NEXACARD_PASSWORD", secret_keys)
        for key in secret_keys:
            with self.subTest(key=key):
                self.assertEqual(items[key]["value"], "********")
                self.assertNotIn(values[key], repr(payload))

    def test_env_post_sentinel_preserves_every_stored_schema_secret(self):
        secret_keys = {
            item["key"]
            for group in scripts.ENV_SCHEMA
            for item in group["items"]
            if item.get("secret")
        }
        values = {key: f"private-{index}" for index, key in enumerate(secret_keys)}
        values["NEXACARD_ACCOUNT"] = "old-account"
        request_values = {key: "********" for key in secret_keys}
        request_values["NEXACARD_ACCOUNT"] = "new-account"
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "".join(f"{key}={value}\n" for key, value in values.items()),
                encoding="utf-8",
            )
            with patch.object(server, "ENV_PATH", str(env_path)), patch.object(
                server, "ENV_EXAMPLE", str(Path(directory) / "missing.example")
            ), patch.object(server, "_apply_saved_env"):
                asyncio.run(
                    server.api_env_set(_JsonRequest({"env": request_values}))
                )
            saved = server._parse_env_file(str(env_path))

        for key in secret_keys:
            with self.subTest(key=key):
                self.assertEqual(saved[key], values[key])
        self.assertEqual(saved["NEXACARD_ACCOUNT"], "new-account")

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
