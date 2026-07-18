import argparse
import asyncio
import io
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import register_claude_api
from common.claude_email_accounts import ClaudeEmailAccount
from common.ipmart_proxy import ProxyLease


def account(provider="NINEMALL"):
    return ClaudeEmailAccount(
        provider,
        "person@example.com",
        "mail-pass",
        "client-guid",
        "refresh-secret",
    )


def lease():
    return ProxyLease(
        "http",
        "gateway.example",
        8080,
        "account-res-US-sid-00000042",
        "proxy-secret",
        "00000042",
        "203.0.113.8",
    )


class _PlaywrightManager:
    def __init__(self, playwright):
        self.playwright = playwright

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, *_args):
        return None


class ClaudeApiProviderDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_ninemail_calls_only_appleemail_dual_artifact_polling(self):
        expected = object()
        client = Mock()
        client.poll_claude_platform_verification.return_value = expected
        context = Mock()
        context.new_page = AsyncMock(
            side_effect=AssertionError("OUTLOOK browser fallback used")
        )

        with patch.object(
            register_claude_api,
            "get_claude_platform_verification_by_token",
        ) as graph, patch.object(
            register_claude_api,
            "fetch_claude_platform_from_broker",
            new=AsyncMock(),
        ) as broker, patch.object(
            register_claude_api,
            "get_claude_platform_verification_outlook_pw",
            new=AsyncMock(),
        ) as browser:
            result = await register_claude_api.fetch_platform_verification(
                context,
                account(),
                37,
                1234.5,
                account_lease="lease-object",
                ninemail_client=client,
            )

        self.assertIs(result, expected)
        poll = client.poll_claude_platform_verification.call_args
        self.assertEqual(poll.args, (account(), 37, 1234.5))
        self.assertIsInstance(poll.kwargs["cancel_event"], threading.Event)
        graph.assert_not_called()
        broker.assert_not_awaited()
        browser.assert_not_awaited()
        context.new_page.assert_not_awaited()

    async def test_ninemail_empty_result_never_falls_back_to_outlook(self):
        client = Mock()
        client.poll_claude_platform_verification.return_value = None
        context = Mock()
        context.new_page = AsyncMock(
            side_effect=AssertionError("OUTLOOK browser fallback used")
        )

        with patch.object(
            register_claude_api,
            "get_claude_platform_verification_by_token",
        ) as graph, patch.object(
            register_claude_api,
            "fetch_claude_platform_from_broker",
            new=AsyncMock(),
        ) as broker:
            result = await register_claude_api.fetch_platform_verification(
                context, account(), 30, 1000.0, ninemail_client=client
            )

        self.assertIsNone(result)
        graph.assert_not_called()
        broker.assert_not_awaited()
        context.new_page.assert_not_awaited()

    async def test_outlook_uses_graph_then_broker_then_browser_and_closes_page(self):
        events = []
        expected = object()
        page = Mock()
        page.close = AsyncMock(side_effect=lambda: events.append("close"))
        context = Mock()
        context.new_page = AsyncMock(
            side_effect=lambda: (events.append("new_page"), page)[1]
        )

        def graph(*_args):
            events.append("graph")
            return None

        async def broker(*_args):
            events.append("broker")
            return None

        async def browser(*_args, **_kwargs):
            events.append("browser")
            return expected

        with patch.dict(
            register_claude_api.os.environ,
            {"MAILBOX_BROKER": "http://broker.test"},
            clear=True,
        ), patch.object(
            register_claude_api,
            "get_claude_platform_verification_by_token",
            side_effect=graph,
        ) as graph_call, patch.object(
            register_claude_api,
            "fetch_claude_platform_from_broker",
            side_effect=broker,
        ), patch.object(
            register_claude_api,
            "get_claude_platform_verification_outlook_pw",
            side_effect=browser,
        ):
            result = await register_claude_api.fetch_platform_verification(
                context,
                account("OUTLOOK"),
                45,
                1234.5,
                account_lease="lease-object",
            )

        self.assertIs(result, expected)
        self.assertEqual(events, ["graph", "broker", "new_page", "browser", "close"])
        graph_call.assert_called_once_with(
            "person@example.com",
            "refresh-secret",
            "client-guid",
            45,
            5,
            1234.5,
            "lease-object",
        )

    async def test_ninemail_cancellation_signals_and_awaits_worker(self):
        class CancellableClient:
            def __init__(self):
                self.started = threading.Event()
                self.cancel_seen = threading.Event()
                self.stopped = threading.Event()

            def poll_claude_platform_verification(
                self, _account, _max_wait, _received_after, *, cancel_event=None
            ):
                self.started.set()
                cancel_event.wait(1)
                self.cancel_seen.set()
                self.stopped.set()
                return None

        client = CancellableClient()
        task = asyncio.create_task(
            register_claude_api.fetch_platform_verification(
                Mock(), account(), 60, 1000.0, ninemail_client=client
            )
        )
        await asyncio.to_thread(client.started.wait)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(client.cancel_seen.is_set())
        self.assertTrue(client.stopped.is_set())


class ClaudeApiRegistrationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account = account()
        self.store = Mock()
        self.bb = Mock()
        self.bb.create_browser.return_value = "profile-a"
        self.bb.open_browser.return_value = {"ws": "ws://browser"}
        self.page = Mock()
        self.context = Mock(pages=[self.page])
        self.browser = Mock(contexts=[self.context])
        self.browser.close = AsyncMock()
        chromium = Mock()
        chromium.connect_over_cdp = AsyncMock(return_value=self.browser)
        self.playwright = SimpleNamespace(chromium=chromium)

    def playwright_patch(self):
        return patch.object(
            register_claude_api,
            "async_playwright",
            return_value=_PlaywrightManager(self.playwright),
        )

    async def test_success_passes_proxy_to_profile_marks_api_ledger_and_cleans_up(self):
        inherited = lease()
        with self.playwright_patch(), patch.object(
            register_claude_api,
            "run_claude_platform_flow",
            new=AsyncMock(return_value="cookies/session.json"),
        ):
            result = await register_claude_api.register_one(
                self.bb,
                self.account,
                self.store,
                timeout=90,
                account_lease=inherited,
            )

        self.assertEqual(result, "cookies/session.json")
        self.bb.create_browser.assert_called_once_with(
            name=unittest.mock.ANY,
            proxyMethod=2,
            proxyType="http",
            host="gateway.example",
            port="8080",
            proxyUserName="account-res-US-sid-00000042",
            proxyPassword="proxy-secret",
        )
        self.store.mark_used.assert_called_once_with(self.account)
        self.store.mark_error.assert_not_called()
        self.store.release.assert_not_called()
        self.browser.close.assert_awaited_once_with()
        self.assertEqual(
            self.bb.method_calls[-2:],
            [call.close_browser("profile-a"), call.delete_browser("profile-a")],
        )

    async def test_stable_flow_failure_marks_safe_code_and_cleans_up(self):
        failure = register_claude_api.ClaudeApiRegistrationError(
            "verification_rejected"
        )
        with self.playwright_patch(), patch.object(
            register_claude_api,
            "run_claude_platform_flow",
            new=AsyncMock(side_effect=failure),
        ):
            result = await register_claude_api.register_one(
                self.bb, self.account, self.store, timeout=90
            )

        self.assertIsNone(result)
        self.store.mark_error.assert_called_once_with(
            self.account, "verification_rejected"
        )
        self.store.release.assert_not_called()
        self.browser.close.assert_awaited_once_with()
        self.bb.close_browser.assert_called_once_with("profile-a")
        self.bb.delete_browser.assert_called_once_with("profile-a")

    async def test_profile_launch_failure_releases_reservation(self):
        self.bb.create_browser.side_effect = RuntimeError(
            "secret profile creation failure"
        )

        with self.assertRaisesRegex(RuntimeError, "secret profile creation failure"):
            await register_claude_api.register_one(
                self.bb, self.account, self.store, timeout=90
            )

        self.store.release.assert_called_once_with(self.account)
        self.store.mark_error.assert_not_called()
        self.bb.close_browser.assert_not_called()
        self.bb.delete_browser.assert_not_called()

    async def test_post_profile_unexpected_failure_is_terminal_and_cleans_profile(self):
        self.bb.open_browser.side_effect = RuntimeError("credential-bearing failure")

        with self.assertRaisesRegex(RuntimeError, "credential-bearing failure"):
            await register_claude_api.register_one(
                self.bb, self.account, self.store, timeout=90
            )

        self.store.mark_error.assert_called_once_with(
            self.account, "registration_error"
        )
        self.store.release.assert_not_called()
        self.bb.close_browser.assert_called_once_with("profile-a")
        self.bb.delete_browser.assert_called_once_with("profile-a")

    async def test_cancellation_releases_and_cleans_profile(self):
        started = asyncio.Event()

        async def block(*_args, **_kwargs):
            started.set()
            await asyncio.Future()

        with self.playwright_patch(), patch.object(
            register_claude_api,
            "run_claude_platform_flow",
            side_effect=block,
        ):
            task = asyncio.create_task(
                register_claude_api.register_one(
                    self.bb, self.account, self.store, timeout=90
                )
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.store.release.assert_called_once_with(self.account)
        self.browser.close.assert_awaited_once_with()
        self.bb.close_browser.assert_called_once_with("profile-a")
        self.bb.delete_browser.assert_called_once_with("profile-a")


class ClaudeApiCliTests(unittest.IsolatedAsyncioTestCase):
    def test_ninemail_failure_output_redacts_credentials(self):
        output = io.StringIO()
        mailbox = account()
        with redirect_stdout(output):
            register_claude_api.log_flow_error(
                "registration_error", account=mailbox
            )
        text = output.getvalue()
        self.assertNotIn("mail-pass", text)
        self.assertNotIn("client-guid", text)
        self.assertNotIn("refresh-secret", text)

    async def test_main_uses_claude_api_store_inherited_lease_and_final_marker(self):
        mailbox = account()
        inherited = lease()
        store = Mock()
        store.reserve_many.return_value = [mailbox]
        bb = Mock()
        output = io.StringIO()
        argv = [
            "register_claude_api.py",
            "--count",
            "1",
            "--concurrency",
            "1",
            "--timeout",
            "77",
            "--emails",
            "custom-mail.txt",
            "--node",
            "none",
            "--proxy-port",
            "7897",
        ]

        with patch.object(
            register_claude_api.sys, "argv", argv
        ), patch.object(
            register_claude_api, "EMAIL_PROVIDER", "NINEMALL"
        ), patch.object(
            register_claude_api,
            "ClaudeEmailAccountStore",
            return_value=store,
        ) as store_type, patch.object(
            register_claude_api, "lease_from_env", return_value=inherited
        ), patch.object(
            register_claude_api,
            "settings_from_env",
            return_value=SimpleNamespace(enabled=False),
        ), patch.object(
            register_claude_api, "BitBrowser", return_value=bb
        ), patch.object(
            register_claude_api,
            "register_one",
            new=AsyncMock(return_value="cookies/session.json"),
        ) as register, redirect_stdout(output):
            status = await register_claude_api.main()

        self.assertEqual(status, 0)
        store_type.assert_called_once_with(
            provider="NINEMALL",
            source_file="custom-mail.txt",
            purpose="claude_api",
        )
        register.assert_awaited_once_with(
            bb, mailbox, store, 77, account_lease=inherited
        )
        self.assertEqual(output.getvalue().splitlines()[-1], "success: 1/1")

    async def test_main_failure_returns_nonzero_releases_and_redacts_exception(self):
        mailbox = account()
        store = Mock()
        store.reserve_many.return_value = [mailbox]
        leaked = "mail-pass client-guid refresh-secret"
        output = io.StringIO()

        with patch.object(
            register_claude_api.sys,
            "argv",
            ["register_claude_api.py", "--email", mailbox.email,
             "--token", mailbox.refresh_token, "--client-id", mailbox.client_id],
        ), patch.object(
            register_claude_api, "EMAIL_PROVIDER", "NINEMALL"
        ), patch.object(
            register_claude_api, "ClaudeEmailAccountStore", return_value=store
        ), patch.object(
            register_claude_api, "lease_from_env", return_value=None
        ), patch.object(
            register_claude_api,
            "settings_from_env",
            return_value=SimpleNamespace(enabled=False),
        ), patch.object(
            register_claude_api, "BitBrowser", side_effect=RuntimeError(leaked)
        ), redirect_stdout(output):
            status = await register_claude_api.main()

        self.assertEqual(status, 1)
        store.release.assert_called_once()
        rendered = output.getvalue()
        self.assertEqual(rendered.splitlines()[-1], "success: 0/1")
        for secret in ("mail-pass", "client-guid", "refresh-secret"):
            self.assertNotIn(secret, rendered)

    async def test_explicit_ninemail_requires_token_and_client_id(self):
        with patch.object(
            register_claude_api.sys,
            "argv",
            ["register_claude_api.py", "--email", "person@example.com"],
        ), self.assertRaisesRegex(
            SystemExit, "requires --token and --client-id"
        ):
            await register_claude_api.main()


if __name__ == "__main__":
    unittest.main()
