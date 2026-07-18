import argparse
import unittest
from unittest.mock import patch

import register_three_platforms


class PlatformProxyEnvTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "ACCOUNT_PROXY_SOURCE": "ipmart",
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://stale.invalid",
            "HTTPS_PROXY": "http://stale.invalid",
        }

    def test_claude_child_has_no_environment_http_proxy(self):
        env = register_three_platforms.platform_child_env("claude", self.env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["ACCOUNT_PROXY_SOURCE"], "ipmart")

    def test_chatgpt_and_grok_restore_existing_clash_behavior(self):
        for platform in ("chatgpt", "grok"):
            with self.subTest(platform=platform):
                env = register_three_platforms.platform_child_env(platform, self.env)
                self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7897")
                self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7897")
                self.assertNotIn("ACCOUNT_PROXY_SOURCE", env)


class PlatformLaunchEnvTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = {
            "ACCOUNT_PROXY_SOURCE": "ipmart",
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://stale.invalid",
            "HTTPS_PROXY": "http://stale.invalid",
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

    def assert_platform_envs(self, captured):
        self.assertNotIn("HTTP_PROXY", captured["claude"])
        self.assertEqual(captured["claude"]["ACCOUNT_PROXY_SOURCE"], "ipmart")
        for platform in ("chatgpt", "grok"):
            self.assertEqual(
                captured[platform]["HTTP_PROXY"], "http://127.0.0.1:7897"
            )
            self.assertNotIn("ACCOUNT_PROXY_SOURCE", captured[platform])

    async def test_sequential_launches_use_platform_environments(self):
        self.assert_platform_envs(await self.capture_launch_envs(parallel=False))

    async def test_parallel_launches_use_platform_environments(self):
        self.assert_platform_envs(await self.capture_launch_envs(parallel=True))


if __name__ == "__main__":
    unittest.main()
