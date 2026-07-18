import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from common.network_route import RESOLVED_ROUTE_ENV_KEY
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
            self.assertNotIn(RESOLVED_ROUTE_ENV_KEY, os.environ)

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

    def test_separate_tasks_refresh_direct_to_clash(self):
        proxy = "http://127.0.0.1:7897"
        connector = Mock(
            side_effect=[ConnectionRefusedError("listener down"), Mock()]
        )
        with patch.object(server, "ENV_PATH", self._env_file("unused")), patch.object(
            server, "BOOT_ENV", {}
        ), patch.dict(
            os.environ,
            {"CLASH_PROXY": proxy, RESOLVED_ROUTE_ENV_KEY: "direct"},
            clear=True,
        ), patch(
            "common.network_route.socket.create_connection", connector
        ):
            first_task = server._child_env()
            second_task = server._child_env()

        self.assertEqual(connector.call_count, 2)
        self.assertEqual(first_task[RESOLVED_ROUTE_ENV_KEY], "direct")
        self.assertEqual(second_task[RESOLVED_ROUTE_ENV_KEY], "clash")
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertNotIn(key, first_task)
            self.assertEqual(second_task[key], proxy)

    def test_separate_tasks_refresh_clash_to_direct(self):
        proxy = "http://127.0.0.1:7897"
        connector = Mock(
            side_effect=[Mock(), ConnectionRefusedError("listener down")]
        )
        with patch.object(server, "ENV_PATH", self._env_file("unused")), patch.object(
            server, "BOOT_ENV", {}
        ), patch.dict(
            os.environ,
            {"CLASH_PROXY": proxy, RESOLVED_ROUTE_ENV_KEY: "clash"},
            clear=True,
        ), patch(
            "common.network_route.socket.create_connection", connector
        ):
            first_task = server._child_env()
            second_task = server._child_env()

        self.assertEqual(connector.call_count, 2)
        self.assertEqual(first_task[RESOLVED_ROUTE_ENV_KEY], "clash")
        self.assertEqual(second_task[RESOLVED_ROUTE_ENV_KEY], "direct")
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(first_task[key], proxy)
            self.assertNotIn(key, second_task)

    def test_saved_octo_bases_are_visible_to_new_children(self):
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        tmp.write(
            "OCTO_PUBLIC_API_BASE="
            "https://public.example.test/api/v2/automation\n"
        )
        tmp.write("OCTO_LOCAL_API_BASE=http://local.example.test:58888\n")
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))

        with patch.object(server, "ENV_PATH", tmp.name), patch.object(
            server, "BOOT_ENV", {}
        ), patch.dict(os.environ, {}, clear=True):
            child = server._child_env()
        self.assertEqual(
            child["OCTO_PUBLIC_API_BASE"],
            "https://public.example.test/api/v2/automation",
        )
        self.assertEqual(
            child["OCTO_LOCAL_API_BASE"], "http://local.example.test:58888"
        )


if __name__ == "__main__":
    unittest.main()
