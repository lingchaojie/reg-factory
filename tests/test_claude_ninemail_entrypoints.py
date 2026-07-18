import argparse
import asyncio
import contextlib
import io
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

import register_three_platforms
import run_full_flow
from common.claude_email_accounts import ClaudeEmailAccount
from common.ipmart_proxy import ProxyLease
from webui import scripts


def platform_args(platforms):
    return argparse.Namespace(
        platforms=platforms,
        timeout=600,
        node="auto",
        keep_on_fail=False,
        import_c2a=False,
        codex=False,
        codex_group=None,
        codex_manual_phone=False,
        grok_sub2api=False,
        grok_sub2api_group=None,
    )


class FakeStore:
    def __init__(self, account):
        self.account = account
        self.released = []

    def reserve_one(self):
        return self.account

    def release(self, account):
        self.released.append(account)
        return True


class ClaudeNineMallEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.account = ClaudeEmailAccount(
            "NINEMALL",
            "person@example.com",
            "mail-pass",
            "client-guid",
            "refresh-secret",
        )

    def test_claude_command_passes_token_and_client_id(self):
        command = register_three_platforms.build_command(
            "claude",
            platform_args(["claude"]),
            ("person@example.com", "mail-pass", "refresh-secret", "client-guid"),
        )
        self.assertEqual(command[command.index("--token") + 1], "refresh-secret")
        self.assertEqual(command[command.index("--client-id") + 1], "client-guid")

    def test_pure_claude_from_pool_uses_ninemail_store(self):
        args = platform_args(["claude"])
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "ClaudeEmailAccountStore",
            return_value=FakeStore(self.account),
        ):
            selected = register_three_platforms.next_pool_account(args)
        self.assertEqual(
            selected,
            ("person@example.com", "mail-pass", "refresh-secret", "client-guid"),
        )

    def test_mixed_platform_from_pool_uses_legacy_email_pool(self):
        args = platform_args(["claude", "chatgpt"])
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms, "ClaudeEmailAccountStore"
        ) as store, patch.object(
            register_three_platforms.email_pool,
            "next_email",
            return_value=("legacy@example.com", "pw", "rt", "cid"),
        ) as legacy:
            selected = register_three_platforms.next_pool_account(args)
        self.assertEqual(selected[0], "legacy@example.com")
        store.assert_not_called()
        legacy.assert_called_once_with("tri")

    def test_outlook_pure_claude_from_pool_uses_legacy_email_pool(self):
        args = platform_args(["claude"])
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "OUTLOOK"}), patch.object(
            register_three_platforms, "ClaudeEmailAccountStore"
        ) as store, patch.object(
            register_three_platforms.email_pool,
            "next_email",
            return_value=("legacy@example.com", "pw", "rt", "cid"),
        ) as legacy:
            selected = register_three_platforms.next_pool_account(args)
        self.assertEqual(selected[0], "legacy@example.com")
        store.assert_not_called()
        legacy.assert_called_once_with("tri")

    def test_full_flow_pure_claude_bypasses_stage_email(self):
        args = argparse.Namespace(platforms=["claude"])
        stage = Mock(side_effect=AssertionError("Outlook Stage A ran"))
        selected = run_full_flow.acquire_stage_account(
            args,
            {"EMAIL_PROVIDER": "NINEMALL"},
            stage_email_fn=stage,
            store_factory=lambda **_kwargs: FakeStore(self.account),
        )
        self.assertEqual(
            selected,
            ("person@example.com", "mail-pass", "refresh-secret", "client-guid"),
        )
        stage.assert_not_called()

    def test_full_flow_mixed_platform_keeps_stage_email(self):
        args = argparse.Namespace(platforms=["claude", "chatgpt"])
        expected = ("legacy@example.com", "pw", "rt", "cid")
        stage = Mock(return_value=expected)
        selected = run_full_flow.acquire_stage_account(
            args,
            {"EMAIL_PROVIDER": "NINEMALL"},
            stage_email_fn=stage,
        )
        self.assertEqual(selected, expected)
        stage.assert_called_once_with(args, {"EMAIL_PROVIDER": "NINEMALL"})

    def test_full_flow_outlook_pure_claude_keeps_stage_email(self):
        args = argparse.Namespace(platforms=["claude"])
        expected = ("legacy@example.com", "pw", "rt", "cid")
        stage = Mock(return_value=expected)
        selected = run_full_flow.acquire_stage_account(
            args,
            {"EMAIL_PROVIDER": "OUTLOOK"},
            stage_email_fn=stage,
        )
        self.assertEqual(selected, expected)
        stage.assert_called_once_with(args, {"EMAIL_PROVIDER": "OUTLOOK"})

    def test_full_flow_non_claude_keeps_stage_email(self):
        args = argparse.Namespace(platforms=["chatgpt"])
        expected = ("legacy@example.com", "pw", "rt", "cid")
        stage = Mock(return_value=expected)
        selected = run_full_flow.acquire_stage_account(
            args,
            {"EMAIL_PROVIDER": "NINEMALL"},
            stage_email_fn=stage,
        )
        self.assertEqual(selected, expected)
        stage.assert_called_once_with(args, {"EMAIL_PROVIDER": "NINEMALL"})

    def test_full_flow_releases_ninemail_reservation_on_proxy_recheck_failure(self):
        store = FakeStore(self.account)
        args = argparse.Namespace(
            skip_email=False,
            email="",
            password="",
            token="",
            client_id="",
            platforms=["claude"],
            dry_run=False,
        )
        env = {
            "EMAIL_PROVIDER": "NINEMALL",
            "IPMART_ENABLED": "1",
            "IPMART_PROXY_HOST": "gateway.example",
            "IPMART_PROXY_PORT": "8080",
            "IPMART_PROXY_USERNAME_TEMPLATE": "user-{sid}",
            "IPMART_PROXY_PASSWORD": "proxy-pass",
        }
        lease = ProxyLease(
            "http",
            "gateway.example",
            8080,
            "user-00000042",
            "proxy-pass",
            "00000042",
            "203.0.113.8",
        )
        with patch.object(
            run_full_flow, "ClaudeEmailAccountStore", return_value=store
        ), patch.object(run_full_flow, "stage_platforms") as platforms:
            result = run_full_flow.run_once(
                args,
                env,
                acquire=lambda **_kwargs: lease,
                verify=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    run_full_flow.IPMartProxyError("changed")
                ),
            )
        self.assertEqual(result, (1, "person@example.com"))
        self.assertEqual(store.released, [self.account])
        platforms.assert_not_called()

    def test_pool_reservation_released_when_claude_subprocess_cannot_launch(self):
        store = FakeStore(self.account)
        args = platform_args(["claude"])
        args.parallel = False
        args.broker = ""
        args.grok_timeout = 40
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "ClaudeEmailAccountStore",
            return_value=store,
        ), patch.object(
            register_three_platforms,
            "run_platform",
            side_effect=register_three_platforms.PlatformLaunchError("launch failed"),
        ):
            selected = register_three_platforms.next_pool_account(args)
            with self.assertRaises(register_three_platforms.PlatformLaunchError):
                asyncio.run(
                    register_three_platforms.process_account(selected, args, {})
                )
        self.assertEqual(store.released, [self.account])

    def test_pure_claude_ninemail_does_not_release_outlook_broker(self):
        args = platform_args(["claude"])
        args.parallel = False
        args.broker = "http://127.0.0.1:8765"
        args.grok_timeout = 40
        account = (
            "person@example.com",
            "mail-pass",
            "refresh-secret",
            "client-guid",
        )
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "run_platform",
            return_value=("claude", True, 0, "test.log"),
        ), patch.object(register_three_platforms, "broker_release") as release:
            asyncio.run(register_three_platforms.process_account(account, args, {}))
        release.assert_not_called()

    def test_failed_claude_child_releases_pool_reservation(self):
        store = FakeStore(self.account)
        args = platform_args(["claude"])
        args.parallel = False
        args.broker = ""
        args.grok_timeout = 40
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "ClaudeEmailAccountStore",
            return_value=store,
        ), patch.object(
            register_three_platforms,
            "run_platform",
            return_value=("claude", False, 1, "test.log"),
        ):
            selected = register_three_platforms.next_pool_account(args)
            asyncio.run(register_three_platforms.process_account(selected, args, {}))
        self.assertEqual(store.released, [self.account])

    def test_pure_claude_ninemail_main_propagates_child_failure(self):
        argv = [
            "register_three_platforms.py",
            "--email",
            "person@example.com",
            "--password",
            "mail-pass",
            "--token",
            "refresh-secret",
            "--client-id",
            "client-guid",
            "--platforms",
            "claude",
            "--broker",
            "",
        ]
        failed = [("claude", False, 1, "test.log")]
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms.sys, "argv", argv
        ), patch.object(
            register_three_platforms,
            "process_account",
            new=AsyncMock(return_value=failed),
        ):
            rc = asyncio.run(register_three_platforms.main())
        self.assertEqual(rc, 1)

    def test_stage_platform_command_log_does_not_expose_mailbox_secrets(self):
        args = argparse.Namespace(
            platforms=["claude"],
            node="auto",
            platform_timeout=600,
            broker="",
            keep_on_fail=False,
            import_c2a=False,
            codex=False,
            grok_sub2api=False,
            dry_run=True,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = run_full_flow.stage_platforms(
                args,
                {"EMAIL_PROVIDER": "NINEMALL"},
                "person@example.com",
                "mail-pass",
                "refresh-secret",
                "client-guid",
            )
        self.assertEqual(rc, 0)
        logged = output.getvalue()
        for secret in ("mail-pass", "refresh-secret", "client-guid"):
            self.assertNotIn(secret, logged)

    def test_webui_exposes_client_id_and_ninemail_env(self):
        claude_flags = {
            item["flag"]
            for item in scripts.script_by_id("register_claude")["args"]
        }
        self.assertIn("--client-id", claude_flags)
        full_flow_flags = {
            item["flag"]
            for item in scripts.script_by_id("run_full_flow")["args"]
        }
        self.assertTrue({"--token", "--client-id"}.issubset(full_flow_flags))
        env_keys = set(scripts.env_keys())
        self.assertTrue(
            {
                "EMAIL_PROVIDER",
                "NINEMALL_EMAIL_FILE",
                "NINEMALL_API_BASE",
                "NINEMALL_API_PASSWORD",
                "NINEMALL_HTTP_TIMEOUT",
                "NINEMALL_POLL_INTERVAL",
            }.issubset(env_keys)
        )
        env_items = {
            item["key"]: item
            for group in scripts.ENV_SCHEMA
            for item in group["items"]
        }
        self.assertEqual(env_items["EMAIL_PROVIDER"]["default"], "NINEMALL")
        self.assertEqual(
            env_items["EMAIL_PROVIDER"]["choices"], ["NINEMALL", "OUTLOOK"]
        )
        self.assertEqual(env_items["NINEMALL_EMAIL_FILE"]["default"], "mail.txt")
        self.assertEqual(
            env_items["NINEMALL_API_BASE"]["default"],
            "https://www.appleemail.top",
        )
        self.assertTrue(env_items["NINEMALL_API_PASSWORD"]["secret"])
        self.assertNotIn("default", env_items["NINEMALL_API_PASSWORD"])
        for key, default in (
            ("NINEMALL_HTTP_TIMEOUT", 30),
            ("NINEMALL_POLL_INTERVAL", 5),
        ):
            self.assertEqual(env_items[key]["type"], "int")
            self.assertEqual(env_items[key]["default"], default)


if __name__ == "__main__":
    unittest.main()
