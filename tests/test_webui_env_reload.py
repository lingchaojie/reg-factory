import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from webui import server


class WebUIEnvReloadTests(unittest.TestCase):
    def _env_file(self, value):
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        tmp.write(f"DYNAMIC_TEST_KEY={value}\n")
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
        return tmp.name

    def test_child_env_uses_latest_dotenv_value_without_restart(self):
        path = self._env_file("new-value")
        with patch.object(server, "ENV_PATH", path):
            with patch.object(server, "BOOT_ENV", {}):
                with patch.dict(os.environ, {"DYNAMIC_TEST_KEY": "stale-value"}):
                    child = server._child_env()
        self.assertEqual(child["DYNAMIC_TEST_KEY"], "new-value")

    def test_explicit_startup_environment_keeps_precedence(self):
        path = self._env_file("dotenv-value")
        with patch.object(server, "ENV_PATH", path):
            with patch.object(server, "BOOT_ENV", {"DYNAMIC_TEST_KEY": "system-value"}):
                with patch.dict(os.environ, {"DYNAMIC_TEST_KEY": "system-value"}):
                    child = server._child_env()
        self.assertEqual(child["DYNAMIC_TEST_KEY"], "system-value")

    def test_startup_route_uses_dotenv_for_webui_process(self):
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        tmp.write("CLASH_PROXY=http://dotenv.example:7897\n")
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))

        with patch.object(server, "ENV_PATH", tmp.name), patch.object(
            server, "BOOT_ENV", {}
        ), patch.dict(os.environ, {}, clear=True), patch(
            "common.network_route.socket.create_connection",
            return_value=Mock(),
        ):
            server._ensure_proxy_env()
            self.assertEqual(
                os.environ["HTTPS_PROXY"], "http://dotenv.example:7897"
            )

    def test_startup_route_keeps_process_proxy_over_dotenv(self):
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        tmp.write("CLASH_PROXY=http://dotenv.example:7897\n")
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
        process_proxy = "http://process.example:7898"

        with patch.object(server, "ENV_PATH", tmp.name), patch.object(
            server, "BOOT_ENV", {"CLASH_PROXY": process_proxy}
        ), patch.dict(
            os.environ, {"CLASH_PROXY": process_proxy}, clear=True
        ), patch(
            "common.network_route.socket.create_connection",
            return_value=Mock(),
        ):
            server._ensure_proxy_env()
            self.assertEqual(os.environ["HTTPS_PROXY"], process_proxy)

    def test_saved_octo_bases_are_visible_to_new_children(self):
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        tmp.write("OCTO_PUBLIC_API_BASE=https://public.example.test\n")
        tmp.write("OCTO_LOCAL_API_BASE=http://local.example.test:58888\n")
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))

        with patch.object(server, "ENV_PATH", tmp.name), patch.object(
            server, "BOOT_ENV", {}
        ), patch.dict(os.environ, {}, clear=True):
            child = server._child_env()
        self.assertEqual(
            child["OCTO_PUBLIC_API_BASE"], "https://public.example.test"
        )
        self.assertEqual(
            child["OCTO_LOCAL_API_BASE"], "http://local.example.test:58888"
        )


if __name__ == "__main__":
    unittest.main()
