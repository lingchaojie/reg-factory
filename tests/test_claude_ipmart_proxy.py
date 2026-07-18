import unittest
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from unittest.mock import AsyncMock

import register
from common.claude_email_accounts import ClaudeEmailAccountStore
from common.ipmart_proxy import ProxyLease


def make_lease():
    return ProxyLease(
        "http", "gateway.example", 8080,
        "account-res-US-sid-00000042", "proxy-secret", "00000042", "203.0.113.8",
    )


class ClaudeIPMartProxyTests(unittest.TestCase):
    def test_create_profile_uses_inherited_credentialed_lease(self):
        bb = Mock()
        bb.create_browser.return_value = "profile-1"
        profile_id = register.create_claude_profile(bb, "claude-1", make_lease())
        self.assertEqual(profile_id, "profile-1")
        self.assertEqual(bb.create_browser.call_args.kwargs, {
            "name": "claude-1",
            "proxyMethod": 2,
            "proxyType": "http",
            "host": "gateway.example",
            "port": "8080",
            "proxyUserName": "account-res-US-sid-00000042",
            "proxyPassword": "proxy-secret",
        })

    def test_create_profile_preserves_default_without_lease(self):
        bb = Mock()
        register.create_claude_profile(bb, "claude-1", None)
        bb.create_browser.assert_called_once_with(name="claude-1")

    def test_inherited_lease_suppresses_clash_selection(self):
        with patch.object(register, "_pick_claude_node") as pick, patch.object(
            register.proxy_switch, "set_node"
        ) as set_node:
            register.configure_claude_proxy("auto", make_lease())
        pick.assert_not_called()
        set_node.assert_not_called()
        self.assertIsNone(register.CLAUDE_PROXY_NODE)

    def test_enabled_ipmart_suppresses_clash_before_direct_acquisition(self):
        with patch.object(register, "_pick_claude_node") as pick:
            register.configure_claude_proxy(
                "auto", account_lease=None, ipmart_enabled=True
            )
        pick.assert_not_called()
        self.assertIsNone(register.CLAUDE_PROXY_NODE)

    def test_ipmart_strips_process_http_proxy_before_acquisition(self):
        env = {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "CLASH_PROXY": "http://127.0.0.1:7897",
        }
        register.prepare_claude_network(
            env, account_lease=make_lease(), ipmart_enabled=False
        )
        self.assertEqual(env, {"CLASH_PROXY": "http://127.0.0.1:7897"})

    def test_enabled_ipmart_strips_http_proxy_for_direct_acquisition(self):
        env = {"HTTP_PROXY": "http://127.0.0.1:7897"}
        register.prepare_claude_network(
            env, account_lease=None, ipmart_enabled=True
        )
        self.assertEqual(env, {})

    def test_disabled_ipmart_without_clash_listener_strips_http_proxy(self):
        env = {
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://127.0.0.1:7897",
        }
        with patch(
            "common.network_route.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ):
            route = register.prepare_claude_network(
                env, account_lease=None, ipmart_enabled=False
            )
        self.assertEqual(route.mode, "direct")
        self.assertNotIn("HTTP_PROXY", env)

    def test_profile_failure_hides_credentialed_client_text(self):
        bb = Mock()
        leaked = (
            "http://account-res-US-sid-00000042:proxy-secret@"
            "gateway.example:8080 account-res-US-sid-{sid}"
        )
        bb.create_browser.side_effect = RuntimeError(leaked)

        with self.assertRaises(RuntimeError) as caught:
            register.create_claude_profile(bb, "claude-1", make_lease())

        rendered = str(caught.exception)
        self.assertEqual(
            rendered,
            "BitBrowser profile creation failed with IPMart account proxy",
        )
        self.assertEqual(
            getattr(caught.exception, "category", None), "configuration"
        )
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(caught.exception.__dict__, {"category": "configuration"})
        for secret in (
            "account-res-US-sid-00000042",
            "proxy-secret",
            "account-res-US-sid-{sid}",
            leaked,
        ):
            self.assertNotIn(secret, rendered)

    def test_profile_failure_without_lease_keeps_useful_error(self):
        bb = Mock()
        bb.create_browser.side_effect = RuntimeError("window quota exceeded")
        with self.assertRaisesRegex(RuntimeError, "window quota exceeded"):
            register.create_claude_profile(bb, "claude-1", None)


class ClaudeProfileRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def _run_recovery(self, raw_message):
        bb = Mock()
        bb.create_browser.side_effect = [RuntimeError(raw_message), "profile-1"]
        registration = AsyncMock(return_value=None)
        error = None
        with patch.object(
            sys,
            "argv",
            [
                "register.py",
                "--email",
                "a@outlook.com",
                "--password",
                "Pass1!",
                "--token",
                "rt-a",
                "--client-id",
                "client-a",
                "--node",
                "none",
            ],
        ), patch.object(
            register, "lease_from_env", return_value=make_lease()
        ), patch.object(
            register,
            "settings_from_env",
            return_value=SimpleNamespace(enabled=True),
        ), patch.object(
            register, "prepare_claude_network"
        ), patch.object(
            register, "configure_claude_proxy"
        ), patch.object(
            register, "BitBrowser", return_value=bb
        ), patch.object(
            register, "register", registration
        ), patch.object(
            register.asyncio, "sleep", new=AsyncMock()
        ), patch("builtins.print") as printer:
            try:
                await register.main()
            except Exception as exc:
                error = exc
        self.assertIsNone(error, f"credentialed recovery escaped: {error}")
        return bb, registration, printer

    async def test_credentialed_quota_failure_cleans_up_and_retries_secretly(self):
        leaked = (
            "maximum quota exceeded for "
            "http://account-res-US-sid-00000042:proxy-secret@"
            "gateway.example:8080"
        )
        bb, registration, printer = await self._run_recovery(leaked)

        self.assertEqual(bb.create_browser.call_count, 2)
        bb.cleanup_browsers.assert_called_once_with(keep=0)
        registration.assert_awaited_once()
        self._assert_output_is_secret_free(printer, leaked)

    async def test_credentialed_transient_failure_retries_secretly(self):
        leaked = (
            "TLS socket disconnected via "
            "http://account-res-US-sid-00000042:proxy-secret@"
            "gateway.example:8080"
        )
        bb, registration, printer = await self._run_recovery(leaked)

        self.assertEqual(bb.create_browser.call_count, 2)
        bb.cleanup_browsers.assert_not_called()
        registration.assert_awaited_once()
        self._assert_output_is_secret_free(printer, leaked)

    def _assert_output_is_secret_free(self, printer, leaked):
        rendered = " ".join(str(call) for call in printer.call_args_list)
        for secret in (
            "account-res-US-sid-00000042",
            "proxy-secret",
            leaked,
        ):
            self.assertNotIn(secret, rendered)


class StandaloneClaudeLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_concurrent_account_passes_its_own_lease_to_mailbox_flow(self):
        leases = [
            make_lease(),
            ProxyLease(
                "http",
                "gateway.example",
                8080,
                "account-res-US-sid-00000043",
                "other-secret",
                "00000043",
                "203.0.113.9",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            emails_path = os.path.join(tmp, "emails.txt")
            with open(emails_path, "w", encoding="utf-8") as stream:
                stream.write("a@outlook.com----Pass1!----rt-a\n")
                stream.write("b@outlook.com----Pass2!----rt-b\n")

            original_prepare = register.prepare_email_accounts

            def prepare_in_temp(args):
                return original_prepare(
                    args,
                    provider="OUTLOOK",
                    store_factory=lambda **kwargs: ClaudeEmailAccountStore(
                        root_dir=tmp, **kwargs
                    ),
                )

            registration = AsyncMock(return_value=None)
            with patch.object(
                sys,
                "argv",
                [
                    "register.py",
                    "--count",
                    "2",
                    "--concurrency",
                    "2",
                    "--emails",
                    emails_path,
                    "--node",
                    "none",
                ],
            ), patch.object(
                register, "EMAIL_PROVIDER", "OUTLOOK"
            ), patch.object(
                register,
                "prepare_email_accounts",
                side_effect=prepare_in_temp,
            ), patch.object(
                register, "lease_from_env", return_value=None
            ), patch.object(
                register,
                "settings_from_env",
                return_value=SimpleNamespace(enabled=True),
            ), patch.object(
                register, "prepare_claude_network"
            ), patch.object(
                register, "configure_claude_proxy"
            ), patch.object(
                register, "BitBrowser", return_value=Mock()
            ), patch.object(
                register, "acquire_proxy", side_effect=leases
            ), patch.object(
                register,
                "create_claude_profile",
                side_effect=["profile-a", "profile-b"],
            ), patch.object(
                register, "register", registration
            ), patch.object(
                register.asyncio, "sleep", new=AsyncMock()
            ):
                await register.main()

        calls_by_email = {
            call.kwargs["account"].email: call
            for call in registration.await_args_list
        }
        self.assertIs(calls_by_email["a@outlook.com"].kwargs["account_lease"], leases[0])
        self.assertIs(calls_by_email["b@outlook.com"].kwargs["account_lease"], leases[1])


if __name__ == "__main__":
    unittest.main()
