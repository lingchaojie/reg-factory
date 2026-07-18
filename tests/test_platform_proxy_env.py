import argparse
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import register_three_platforms


ACCOUNT_PROXY_KEYS = (
    "ACCOUNT_PROXY_SOURCE",
    "ACCOUNT_PROXY_TYPE",
    "ACCOUNT_PROXY_HOST",
    "ACCOUNT_PROXY_PORT",
    "ACCOUNT_PROXY_USERNAME",
    "ACCOUNT_PROXY_PASSWORD",
    "ACCOUNT_PROXY_SID",
    "ACCOUNT_PROXY_EXIT_IP",
)
HTTP_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


class PlatformProxyEnvTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "ACCOUNT_PROXY_SOURCE": "ipmart",
            "ACCOUNT_PROXY_TYPE": "http",
            "ACCOUNT_PROXY_HOST": "gateway.example",
            "ACCOUNT_PROXY_PORT": "8080",
            "ACCOUNT_PROXY_USERNAME": "account-res-US-sid-00000042",
            "ACCOUNT_PROXY_PASSWORD": "test-secret",
            "ACCOUNT_PROXY_SID": "00000042",
            "ACCOUNT_PROXY_EXIT_IP": "203.0.113.8",
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://stale.invalid",
            "HTTPS_PROXY": "http://stale.invalid",
            "http_proxy": "http://stale.invalid",
            "https_proxy": "http://stale.invalid",
        }

    def test_claude_child_has_no_environment_http_proxy(self):
        env = register_three_platforms.platform_child_env("claude", self.env)
        for key in HTTP_PROXY_KEYS:
            self.assertNotIn(key, env)
        for key in ACCOUNT_PROXY_KEYS:
            self.assertEqual(env[key], self.env[key])

    def test_chatgpt_and_grok_preserve_exact_original_http_routes(self):
        for platform in ("chatgpt", "grok"):
            with self.subTest(platform=platform):
                env = register_three_platforms.platform_child_env(platform, self.env)
                for key in HTTP_PROXY_KEYS:
                    self.assertEqual(env[key], "http://stale.invalid")
                for key in ACCOUNT_PROXY_KEYS:
                    self.assertNotIn(key, env)

    def test_chatgpt_and_grok_fall_back_to_clash_without_original_route(self):
        base_env = {
            key: value for key, value in self.env.items()
            if key not in HTTP_PROXY_KEYS
        }
        for platform in ("chatgpt", "grok"):
            with self.subTest(platform=platform):
                env = register_three_platforms.platform_child_env(
                    platform, base_env
                )
                for key in HTTP_PROXY_KEYS:
                    self.assertEqual(env[key], "http://127.0.0.1:7897")
                for key in ACCOUNT_PROXY_KEYS:
                    self.assertNotIn(key, env)

    def test_chatgpt_and_grok_use_reachable_clash_without_ipmart_lease(self):
        base_env = {
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://upper-http.example",
            "HTTPS_PROXY": "http://upper-https.example",
            "http_proxy": "http://lower-http.example",
            "https_proxy": "http://lower-https.example",
        }
        for platform in ("chatgpt", "grok"):
            with self.subTest(platform=platform):
                with patch(
                    "common.network_route.socket.create_connection",
                    return_value=Mock(),
                ):
                    env = register_three_platforms.platform_child_env(
                        platform, base_env
                    )
                for key in HTTP_PROXY_KEYS:
                    self.assertEqual(env[key], base_env["CLASH_PROXY"])
                self.assertIsNot(env, base_env)

    def test_chatgpt_and_grok_strip_dead_clash_without_ipmart_lease(self):
        base_env = {
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "http_proxy": "http://127.0.0.1:7897",
            "https_proxy": "http://127.0.0.1:7897",
        }
        for platform in ("chatgpt", "grok"):
            with self.subTest(platform=platform), patch(
                "common.network_route.socket.create_connection",
                side_effect=ConnectionRefusedError,
            ):
                env = register_three_platforms.platform_child_env(
                    platform, base_env
                )
            for key in HTTP_PROXY_KEYS:
                self.assertNotIn(key, env)

    def test_direct_entry_loads_dotenv_before_chatgpt_route(self):
        args = argparse.Namespace(broker="", grok_timeout=40)
        connector = Mock(
            side_effect=[Mock(), ConnectionRefusedError("listener stopped")]
        )
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
                f.write("CLASH_PROXY=http://dotenv.example:7897\n")
            with patch.object(
                register_three_platforms, "ROOT", tmp
            ), patch.dict(os.environ, {}, clear=True), patch(
                "common.network_route.socket.create_connection",
                connector,
            ):
                base_env = register_three_platforms.child_env_for(args)
                chatgpt_env = register_three_platforms.platform_child_env(
                    "chatgpt", base_env, ["chatgpt"]
                )
                grok_env = register_three_platforms.platform_child_env(
                    "grok", base_env, ["grok"]
                )
        self.assertEqual(connector.call_count, 1)
        for child in (chatgpt_env, grok_env):
            self.assertEqual(
                child["HTTPS_PROXY"], "http://dotenv.example:7897"
            )


class PlatformLaunchEnvTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = {
            "ACCOUNT_PROXY_SOURCE": "ipmart",
            "ACCOUNT_PROXY_TYPE": "http",
            "ACCOUNT_PROXY_HOST": "gateway.example",
            "ACCOUNT_PROXY_PORT": "8080",
            "ACCOUNT_PROXY_USERNAME": "account-res-US-sid-00000042",
            "ACCOUNT_PROXY_PASSWORD": "test-secret",
            "ACCOUNT_PROXY_SID": "00000042",
            "ACCOUNT_PROXY_EXIT_IP": "203.0.113.8",
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://stale.invalid",
            "HTTPS_PROXY": "http://stale.invalid",
            "http_proxy": "http://stale.invalid",
            "https_proxy": "http://stale.invalid",
        }

    async def capture_launch_envs(self, parallel):
        args = argparse.Namespace(
            platforms=["claude", "chatgpt", "grok"],
            parallel=parallel,
            timeout=600,
            node="auto",
            keep_on_fail=False,
            import_c2a=False,
            codex=False,
            codex_group=None,
            codex_manual_phone=False,
            grok_sub2api=False,
            grok_sub2api_group=None,
            broker="",
        )
        captured = {}

        async def fake_run(platform, _cmd, _run_id, child_env):
            captured[platform] = child_env
            return platform, True, 0, "test.log"

        with patch.object(
            register_three_platforms, "run_platform", side_effect=fake_run
        ):
            await register_three_platforms.process_account(
                ("a@outlook.com", "Pass1!", "", ""), args, self.env
            )
        return captured

    async def capture_launch_order(self, platforms, parallel, env):
        args = argparse.Namespace(
            platforms=platforms,
            parallel=parallel,
            timeout=600,
            node="auto",
            keep_on_fail=False,
            import_c2a=False,
            codex=False,
            codex_group=None,
            codex_manual_phone=False,
            grok_sub2api=False,
            grok_sub2api_group=None,
            broker="",
        )
        order = []
        captured = {}

        async def fake_run(platform, _cmd, _run_id, child_env):
            order.append(platform)
            captured[platform] = child_env
            return platform, True, 0, "test.log"

        with patch.object(
            register_three_platforms, "run_platform", side_effect=fake_run
        ):
            await register_three_platforms.process_account(
                ("a@outlook.com", "Pass1!", "", ""), args, env
            )
        return order, captured

    def assert_platform_envs(self, captured):
        for key in HTTP_PROXY_KEYS:
            self.assertNotIn(key, captured["claude"])
        for key in ACCOUNT_PROXY_KEYS:
            self.assertEqual(captured["claude"][key], self.env[key])
        for platform in ("chatgpt", "grok"):
            for key in HTTP_PROXY_KEYS:
                self.assertEqual(
                    captured[platform][key], "http://stale.invalid"
                )
            for key in ACCOUNT_PROXY_KEYS:
                self.assertNotIn(key, captured[platform])

    async def test_sequential_launches_use_platform_environments(self):
        self.assert_platform_envs(await self.capture_launch_envs(parallel=False))

    async def test_parallel_launches_use_platform_environments(self):
        self.assert_platform_envs(await self.capture_launch_envs(parallel=True))

    async def test_reversed_sequential_order_runs_claude_first_with_lease(self):
        order, captured = await self.capture_launch_order(
            ["chatgpt", "grok", "claude"], False, self.env
        )
        self.assertEqual(order, ["claude", "chatgpt", "grok"])
        self.assert_platform_envs(captured)

    async def test_reversed_parallel_order_prioritizes_claude_and_keeps_envs(self):
        order, captured = await self.capture_launch_order(
            ["chatgpt", "grok", "claude"], True, self.env
        )
        self.assertEqual(order, ["claude", "chatgpt", "grok"])
        self.assert_platform_envs(captured)

    async def test_no_lease_sequential_order_stays_exactly_as_requested(self):
        env = {
            key: value for key, value in self.env.items()
            if key not in ACCOUNT_PROXY_KEYS
        }
        requested = ["chatgpt", "grok", "claude"]
        with patch(
            "common.network_route.socket.create_connection",
            return_value=Mock(),
        ):
            order, _captured = await self.capture_launch_order(
                requested, False, env
            )
        self.assertEqual(order, requested)


if __name__ == "__main__":
    unittest.main()
