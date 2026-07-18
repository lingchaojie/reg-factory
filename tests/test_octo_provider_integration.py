import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import adspower
import bitbrowser
import config
import octobrowser
import outlook_reg_loop
import register_outlook_standalone
import unlock_outlook
from webui import scripts, server


class _JsonRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class OctoProviderIntegrationTests(unittest.TestCase):
    def setUp(self):
        unlock_outlook._BROWSER_CLIENT = None

    def tearDown(self):
        unlock_outlook._BROWSER_CLIENT = None

    def test_factory_selects_octo(self):
        with patch.dict(os.environ, {"FINGERPRINT_BROWSER": "octo"}):
            browser = bitbrowser.BitBrowser()
        self.assertIsInstance(browser, octobrowser.OctoBrowser)

    def test_factory_preserves_bitbrowser_default(self):
        with patch.dict(os.environ, {"FINGERPRINT_BROWSER": "bitbrowser"}):
            browser = bitbrowser.BitBrowser()
        self.assertIs(type(browser), bitbrowser.BitBrowser)

    def test_factory_preserves_adspower(self):
        sentinel = object()
        with patch.dict(os.environ, {"FINGERPRINT_BROWSER": "adspower"}), patch.object(
            adspower, "AdsPower", return_value=sentinel
        ) as factory:
            browser = bitbrowser.BitBrowser(api_base="http://ads.test")
        self.assertIs(browser, sentinel)
        factory.assert_called_once_with(api_base="http://ads.test")

    def test_canonical_octo_base_settings_reach_adapter(self):
        env = {
            "OCTO_API_TOKEN": "canonical-token",
            "OCTO_PUBLIC_API_BASE": (
                "https://public.example.test/api/v2/automation/"
            ),
            "OCTO_LOCAL_API_BASE": "http://local.example.test:58888/",
        }
        try:
            with patch.dict(os.environ, env):
                importlib.reload(config)
                importlib.reload(octobrowser)
                browser = octobrowser.OctoBrowser()
                self.assertEqual(browser.api_token, "canonical-token")
                self.assertEqual(
                    browser.public_api,
                    "https://public.example.test/api/v2/automation",
                )
                self.assertEqual(
                    browser.local_api, "http://local.example.test:58888"
                )
        finally:
            importlib.reload(config)
            importlib.reload(octobrowser)

    def test_legacy_octo_base_names_remain_readable(self):
        env = {
            "OCTO_PUBLIC_API": "https://legacy-public.example.test/",
            "OCTO_LOCAL_API": "http://legacy-local.example.test:58888/",
        }
        try:
            with patch.dict(os.environ, env, clear=True):
                importlib.reload(config)
                importlib.reload(octobrowser)
                browser = octobrowser.OctoBrowser(api_token="token")
                self.assertEqual(
                    browser.public_api,
                    "https://legacy-public.example.test/api/v2/automation",
                )
                self.assertEqual(
                    browser.local_api,
                    "http://legacy-local.example.test:58888",
                )
        finally:
            importlib.reload(config)
            importlib.reload(octobrowser)

    def test_outlook_loop_uses_provider_adapter_for_octo(self):
        with patch.object(
            outlook_reg_loop, "_fingerprint_provider", return_value="octo"
        ), patch("bitbrowser.BitBrowser") as factory, patch.object(
            outlook_reg_loop,
            "_bb_call",
            return_value={"success": True, "data": {"id": "legacy"}},
        ):
            factory.return_value.create_browser.return_value = "p1"
            result = outlook_reg_loop._bb_create_for_outlook_reg("outlook")
        self.assertEqual(result, "p1")
        factory.return_value.create_browser.assert_called_once()

    def test_standalone_factory_selects_shared_provider_for_octo(self):
        with patch.object(
            register_outlook_standalone,
            "_fingerprint_provider",
            return_value="octo",
        ), patch("bitbrowser.BitBrowser") as factory:
            client = register_outlook_standalone.BitBrowserClient()
        self.assertIs(client, factory.return_value)
        factory.assert_called_once_with()

    def test_standalone_preserves_custom_adspower_api_base(self):
        with patch.object(
            register_outlook_standalone,
            "_fingerprint_provider",
            return_value="adspower",
        ), patch("bitbrowser.BitBrowser") as factory:
            client = register_outlook_standalone.BitBrowserClient(
                "http://ads.example.test"
            )
        self.assertIs(client, factory.return_value)
        factory.assert_called_once_with(api_base="http://ads.example.test")

    def test_unlock_uses_shared_provider_for_octo(self):
        response = Mock()
        response.json.return_value = {
            "success": True,
            "data": {"id": "legacy"},
        }
        with patch.object(
            unlock_outlook, "_fingerprint_provider", return_value="octo"
        ), patch("bitbrowser.BitBrowser") as factory, patch.object(
            unlock_outlook.requests, "post", return_value=response
        ):
            factory.return_value._post.return_value = {
                "success": True,
                "data": {"id": "p1"},
            }
            result = unlock_outlook._bb_post(
                "/browser/update", {"name": "unlock"}
            )
        self.assertEqual(result["data"]["id"], "p1")
        factory.return_value._post.assert_called_once_with(
            "/browser/update", {"name": "unlock"}
        )

    def test_webui_metadata_exposes_canonical_octo_settings(self):
        group = next(
            group
            for group in scripts.ENV_SCHEMA
            if any(
                item.get("key") == "FINGERPRINT_BROWSER"
                for item in group["items"]
            )
        )
        items = {item["key"]: item for item in group["items"]}
        self.assertIn("octo", items["FINGERPRINT_BROWSER"]["choices"])
        self.assertIn("OCTO_API_TOKEN", items)
        self.assertIn("OCTO_PUBLIC_API_BASE", items)
        self.assertIn("OCTO_LOCAL_API_BASE", items)
        self.assertTrue(items["OCTO_API_TOKEN"]["secret"])
        self.assertEqual(
            items["OCTO_PUBLIC_API_BASE"]["default"],
            "https://app.octobrowser.net/api/v2/automation",
        )
        self.assertEqual(
            items["OCTO_LOCAL_API_BASE"]["default"],
            "http://127.0.0.1:58888",
        )
        self.assertNotIn("OCTO_PUBLIC_API", items)
        self.assertNotIn("OCTO_LOCAL_API", items)

    def test_status_health_uses_octo_local_api_base(self):
        with patch.object(
            server, "_fingerprint_provider", return_value="octo"
        ), patch.object(
            server,
            "_read_config_val",
            return_value="http://127.0.0.1:58888",
        ) as read, patch.object(
            server, "_direct_get", return_value=(200, "{}")
        ) as get:
            ok, message = server._test_bitbrowser()
        self.assertTrue(ok)
        self.assertIn("Octo Browser", message)
        read.assert_any_call("OCTO_LOCAL_API_BASE", "")
        self.assertIn("127.0.0.1:58888/api/update", get.call_args.args[0])

    def test_status_health_falls_back_to_legacy_octo_local_api(self):
        def config_value(key, default=""):
            values = {
                "OCTO_LOCAL_API_BASE": "",
                "OCTO_LOCAL_API": "http://legacy-octo.test:58888",
            }
            return values.get(key, default)

        with patch.object(
            server, "_fingerprint_provider", return_value="octo"
        ), patch.object(
            server, "_read_config_val", side_effect=config_value
        ), patch.object(
            server, "_direct_get", return_value=(200, "{}")
        ) as get:
            ok, _message = server._test_bitbrowser()
        self.assertTrue(ok)
        self.assertIn(
            "legacy-octo.test:58888/api/update", get.call_args.args[0]
        )

    def test_api_status_reports_octo_provider_and_base(self):
        def config_value(key, default=""):
            values = {
                "OCTO_LOCAL_API_BASE": "http://octo.local:58888",
                "CLASH_API": "http://clash.local:9097",
            }
            return values.get(key, default)

        with patch.object(
            server, "_fingerprint_provider", return_value="octobrowser"
        ), patch.object(
            server, "_read_config_val", side_effect=config_value
        ), patch.object(server, "_http_alive", return_value=True):
            status = server.api_status()
        self.assertEqual(status["browser_provider"], "octo")
        self.assertTrue(status["bitbrowser"])

    def test_browser_provider_fallback_drives_real_webui_health_and_label(self):
        env = {
            "BROWSER_PROVIDER": "octo",
            "OCTO_LOCAL_API_BASE": "http://fallback-octo.test:58888",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            server, "ENV_PATH", os.path.join(tmp, "missing.env")
        ), patch.dict(os.environ, env, clear=True), patch.object(
            server, "_direct_get", return_value=(200, "{}")
        ) as direct_get, patch.object(
            server, "_http_alive", return_value=True
        ):
            ok, message = server._test_bitbrowser()
            status = server.api_status()

        self.assertTrue(ok)
        self.assertIn("Octo Browser", message)
        self.assertEqual(
            direct_get.call_args.args[0],
            "http://fallback-octo.test:58888/api/update",
        )
        self.assertEqual(status["browser_provider"], "octo")

    def test_frontend_maps_octo_status_label(self):
        source = Path(server.WEBUI, "static", "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("octo: 'Octo Browser'", source)

    def test_webui_masks_saved_octo_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            Path(env_path).write_text(
                "OCTO_API_TOKEN=top-secret-token\n", encoding="utf-8"
            )
            with patch.object(server, "ENV_PATH", env_path), patch.object(
                server, "ENV_EXAMPLE", os.path.join(tmp, "missing.example")
            ):
                payload = server.api_env_get()
        items = {
            item["key"]: item
            for group in payload["groups"]
            for item in group["items"]
        }
        self.assertIn("OCTO_API_TOKEN", items)
        item = items["OCTO_API_TOKEN"]
        self.assertEqual(item["value"], "********")
        self.assertNotIn("top-secret-token", repr(payload))

    def test_webui_displays_legacy_octo_bases_under_canonical_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            Path(env_path).write_text(
                "OCTO_PUBLIC_API=https://legacy-public.example.test\n"
                "OCTO_LOCAL_API=http://legacy-local.example.test:58888\n",
                encoding="utf-8",
            )
            with patch.object(server, "ENV_PATH", env_path), patch.object(
                server, "ENV_EXAMPLE", os.path.join(tmp, "missing.example")
            ):
                payload = server.api_env_get()
        items = {
            item["key"]: item
            for group in payload["groups"]
            for item in group["items"]
        }
        self.assertEqual(
            items["OCTO_PUBLIC_API_BASE"]["value"],
            "https://legacy-public.example.test",
        )
        self.assertEqual(
            items["OCTO_LOCAL_API_BASE"]["value"],
            "http://legacy-local.example.test:58888",
        )

    def test_saving_masked_octo_token_preserves_existing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            Path(env_path).write_text(
                "OCTO_API_TOKEN=top-secret-token\n"
                "OCTO_LOCAL_API_BASE=http://127.0.0.1:58888\n",
                encoding="utf-8",
            )
            request = _JsonRequest({
                "env": {
                    "OCTO_API_TOKEN": "********",
                    "OCTO_LOCAL_API_BASE": "http://127.0.0.1:59999",
                }
            })
            with patch.object(server, "ENV_PATH", env_path), patch.object(
                server, "ENV_EXAMPLE", os.path.join(tmp, "missing.example")
            ), patch.object(server, "_apply_saved_env"):
                asyncio.run(server.api_env_set(request))
            saved = Path(env_path).read_text(encoding="utf-8")
        self.assertIn("OCTO_API_TOKEN=top-secret-token", saved)
        self.assertIn(
            "OCTO_LOCAL_API_BASE=http://127.0.0.1:59999", saved
        )

    def test_env_writer_closes_input_before_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            Path(env_path).write_text("VALUE=old\n", encoding="utf-8")
            read_handles = []
            real_open = open

            def tracking_open(*args, **kwargs):
                handle = real_open(*args, **kwargs)
                mode = kwargs.get(
                    "mode", args[1] if len(args) > 1 else "r"
                )
                if os.fspath(args[0]) == env_path and "r" in mode:
                    read_handles.append(handle)
                return handle

            try:
                with patch("builtins.open", side_effect=tracking_open), patch.object(
                    server.os, "replace"
                ) as replace:
                    server._write_env_file(env_path, {"VALUE": "new"})
                self.assertTrue(read_handles)
                self.assertTrue(all(handle.closed for handle in read_handles))
                replace.assert_called_once()
            finally:
                for handle in read_handles:
                    handle.close()


if __name__ == "__main__":
    unittest.main()
