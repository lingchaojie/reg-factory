import argparse
import asyncio
import io
import threading
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import register_claude_api
from common import proxy_switch
from common.claude_email_accounts import ClaudeEmailAccount
from common.claude_platform_mailbox import ClaudePlatformVerification
from common.ipmart_proxy import ProxyLease
from tests.test_claude_api_registration import (
    DelayedPlatformPage,
    FakePlatformPage,
)


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
                browser_proxy_fields={
                    "proxyMethod": 2,
                    "proxyType": "http",
                    "host": "127.0.0.1",
                    "port": "7897",
                },
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

    async def test_clash_proxy_fields_reach_profile_creation_without_ipmart(self):
        proxy_fields = {
            "proxyMethod": 2,
            "proxyType": "http",
            "host": "127.0.0.1",
            "port": "8899",
        }
        with self.playwright_patch(), patch.object(
            register_claude_api,
            "run_claude_platform_flow",
            new=AsyncMock(return_value="cookies/session.json"),
        ):
            await register_claude_api.register_one(
                self.bb,
                self.account,
                self.store,
                timeout=90,
                browser_proxy_fields=proxy_fields,
            )

        self.bb.create_browser.assert_called_once_with(
            name=unittest.mock.ANY,
            **proxy_fields,
        )

    async def test_none_proxy_keeps_profile_direct(self):
        with self.playwright_patch(), patch.object(
            register_claude_api,
            "run_claude_platform_flow",
            new=AsyncMock(return_value="cookies/session.json"),
        ):
            await register_claude_api.register_one(
                self.bb,
                self.account,
                self.store,
                timeout=90,
                browser_proxy_fields={},
            )

        self.bb.create_browser.assert_called_once_with(
            name=unittest.mock.ANY
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
            bb,
            mailbox,
            store,
            77,
            account_lease=inherited,
            browser_proxy_fields={},
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


class ClaudeApiClashProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mailbox = account()
        self.store = Mock()
        self.store.reserve_many.return_value = [self.mailbox]
        self.bb = Mock()

    def base_patches(self, argv, *, inherited=None, enabled=False):
        return (
            patch.object(register_claude_api.sys, "argv", argv),
            patch.object(register_claude_api, "EMAIL_PROVIDER", "NINEMALL"),
            patch.object(
                register_claude_api,
                "ClaudeEmailAccountStore",
                return_value=self.store,
            ),
            patch.object(
                register_claude_api, "lease_from_env", return_value=inherited
            ),
            patch.object(
                register_claude_api,
                "settings_from_env",
                return_value=SimpleNamespace(enabled=enabled),
            ),
            patch.object(register_claude_api, "BitBrowser", return_value=self.bb),
        )

    async def test_explicit_node_switches_and_supplies_local_proxy_profile_fields(self):
        argv = [
            "register_claude_api.py",
            "--node",
            "tokyo-01",
            "--proxy-port",
            "8899",
        ]
        argv_patch, provider, store, inherited, settings, browser = self.base_patches(
            argv
        )
        with argv_patch, provider, store, inherited, settings, browser, patch.object(
            proxy_switch, "set_node", return_value=True
        ) as set_node, patch.object(
            proxy_switch, "find_working_node"
        ) as probe, patch.object(
            register_claude_api,
            "register_one",
            new=AsyncMock(return_value="cookies/session.json"),
        ) as register:
            status = await register_claude_api.main()

        self.assertEqual(status, 0)
        set_node.assert_called_once_with("tokyo-01")
        probe.assert_not_called()
        register.assert_awaited_once_with(
            self.bb,
            self.mailbox,
            self.store,
            480,
            account_lease=None,
            browser_proxy_fields={
                "proxyMethod": 2,
                "proxyType": "http",
                "host": "127.0.0.1",
                "port": "8899",
            },
        )

    async def test_auto_node_probes_existing_candidates_and_supplies_proxy(self):
        argv = ["register_claude_api.py", "--node", "auto"]
        argv_patch, provider, store, inherited, settings, browser = self.base_patches(
            argv
        )
        candidates = ["tokyo-01", "osaka-02"]
        with argv_patch, provider, store, inherited, settings, browser, patch.object(
            proxy_switch,
            "concrete_nodes",
            return_value=candidates,
        ), patch.object(
            proxy_switch,
            "find_working_node",
            return_value="osaka-02",
        ) as probe, patch.object(
            register_claude_api,
            "register_one",
            new=AsyncMock(return_value="cookies/session.json"),
        ) as register:
            status = await register_claude_api.main()

        self.assertEqual(status, 0)
        probe.assert_called_once_with(
            test_url="https://platform.claude.com/",
            challenge_markers=(
                "app-unavailable-in-region",
                "unavailable in your",
                "just a moment",
                "performing security",
            ),
            candidates=candidates,
            verbose=False,
        )
        self.assertEqual(
            register.await_args.kwargs["browser_proxy_fields"]["port"], "7897"
        )

    async def test_none_node_does_not_switch_or_add_profile_proxy(self):
        argv = ["register_claude_api.py", "--node", "none"]
        argv_patch, provider, store, inherited, settings, browser = self.base_patches(
            argv
        )
        with argv_patch, provider, store, inherited, settings, browser, patch.object(
            proxy_switch, "set_node"
        ) as set_node, patch.object(
            proxy_switch, "find_working_node"
        ) as probe, patch.object(
            register_claude_api,
            "register_one",
            new=AsyncMock(return_value="cookies/session.json"),
        ) as register:
            await register_claude_api.main()

        set_node.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(register.await_args.kwargs["browser_proxy_fields"], {})

    async def test_ipmart_lease_wins_and_skips_clash(self):
        inherited_lease = lease()
        argv = ["register_claude_api.py", "--node", "tokyo-01"]
        argv_patch, provider, store, inherited, settings, browser = self.base_patches(
            argv, inherited=inherited_lease
        )
        with argv_patch, provider, store, inherited, settings, browser, patch.object(
            proxy_switch, "set_node"
        ) as set_node, patch.object(
            proxy_switch, "find_working_node"
        ) as probe, patch.object(
            register_claude_api,
            "register_one",
            new=AsyncMock(return_value="cookies/session.json"),
        ) as register:
            await register_claude_api.main()

        set_node.assert_not_called()
        probe.assert_not_called()
        self.assertIs(register.await_args.kwargs["account_lease"], inherited_lease)
        self.assertEqual(register.await_args.kwargs["browser_proxy_fields"], {})

    async def test_invalid_proxy_port_fails_before_reservation_or_browser_launch(self):
        with patch.object(
            register_claude_api.sys,
            "argv",
            ["register_claude_api.py", "--proxy-port", "70000"],
        ), patch.object(
            register_claude_api, "ClaudeEmailAccountStore"
        ) as store, patch.object(
            register_claude_api, "BitBrowser"
        ) as browser, self.assertRaises(SystemExit):
            await register_claude_api.main()

        store.assert_not_called()
        browser.assert_not_called()


class ClaudeApiDeadlineAndCleanupTests(ClaudeApiRegistrationLifecycleTests):
    async def drain_background_tasks(self):
        drain = getattr(register_claude_api, "_drain_retained_tasks", None)
        if drain is not None:
            await drain(0.1)
            self.assertFalse(register_claude_api._RETAINED_BACKGROUND_TASKS)

    async def test_cancellation_resistant_flow_is_detached_at_fixed_bound(self):
        started = asyncio.Event()
        release = asyncio.Event()
        cancellations = 0

        async def resist_cancellation(*_args, **_kwargs):
            nonlocal cancellations
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancellations += 1
            return "late-success.json"

        with self.playwright_patch(), patch.object(
            register_claude_api,
            "run_claude_platform_flow",
            side_effect=resist_cancellation,
        ), patch.object(
            register_claude_api, "EMERGENCY_TIMEOUT_CUSHION", 0.005
        ), patch.object(
            register_claude_api,
            "TASK_CANCEL_GRACE",
            0.005,
            create=True,
        ):
            started_at = asyncio.get_running_loop().time()
            registration = asyncio.create_task(
                register_claude_api.register_one(
                    self.bb, self.account, self.store, timeout=0.005
                )
            )
            await started.wait()
            done, _pending = await asyncio.wait(
                {registration}, timeout=0.08
            )
            elapsed = asyncio.get_running_loop().time() - started_at
            try:
                self.assertIn(registration, done)
                self.assertIsNone(registration.result())
                self.assertLess(elapsed, 0.06)
                self.assertGreaterEqual(cancellations, 2)
                self.store.mark_error.assert_called_once_with(
                    self.account, "timeout"
                )
            finally:
                release.set()
                if not registration.done():
                    registration.cancel()
                await asyncio.gather(registration, return_exceptions=True)
                await self.drain_background_tasks()

    async def test_stuck_code_is_classified_before_emergency_timeout(self):
        page = FakePlatformPage(code_submit_target_state="code")
        self.context.pages = [page]
        now = 0.0

        async def advance(delay):
            nonlocal now
            now += delay

        with self.playwright_patch(), patch.object(
            register_claude_api,
            "fetch_platform_verification",
            new=AsyncMock(return_value=ClaudePlatformVerification(code="482731")),
        ), patch.object(
            register_claude_api, "_clock", side_effect=lambda: now
        ), patch.object(
            register_claude_api.asyncio, "sleep", side_effect=advance
        ), patch.object(
            register_claude_api,
            "EMERGENCY_TIMEOUT_CUSHION",
            0.05,
            create=True,
        ):
            result = await register_claude_api.register_one(
                self.bb, self.account, self.store, timeout=0.02
            )

        self.assertIsNone(result)
        self.store.mark_error.assert_called_once_with(
            self.account, "verification_rejected"
        )

    async def test_delayed_success_near_deadline_remains_success(self):
        page = DelayedPlatformPage(
            code_submit_target_state="pending",
            transition_after=2,
            transition_to="authenticated",
        )
        self.context.pages = [page]
        now = 0.0

        async def advance(delay):
            nonlocal now
            now += delay

        with self.playwright_patch(), patch.object(
            register_claude_api,
            "fetch_platform_verification",
            new=AsyncMock(return_value=ClaudePlatformVerification(code="482731")),
        ), patch.object(
            register_claude_api,
            "save_claude_platform_session",
            new=AsyncMock(return_value="cookies/session.json"),
        ), patch.object(
            register_claude_api, "_clock", side_effect=lambda: now
        ), patch.object(
            register_claude_api.asyncio, "sleep", side_effect=advance
        ), patch.object(
            register_claude_api,
            "EMERGENCY_TIMEOUT_CUSHION",
            0.05,
            create=True,
        ):
            result = await register_claude_api.register_one(
                self.bb, self.account, self.store, timeout=0.08
            )

        self.assertEqual(result, "cookies/session.json")
        self.store.mark_used.assert_called_once_with(self.account)

    async def test_hung_mailbox_is_classified_as_mail_timeout(self):
        async def hang(*_args, **_kwargs):
            await asyncio.Future()

        self.context.pages = [FakePlatformPage()]
        with self.playwright_patch(), patch.object(
            register_claude_api,
            "fetch_platform_verification",
            side_effect=hang,
        ), patch.object(
            register_claude_api,
            "EMERGENCY_TIMEOUT_CUSHION",
            0.05,
            create=True,
        ):
            result = await register_claude_api.register_one(
                self.bb, self.account, self.store, timeout=0.02
            )

        self.assertIsNone(result)
        self.store.mark_error.assert_called_once_with(self.account, "mail_timeout")

    async def test_hard_hang_is_stopped_by_emergency_cushion(self):
        async def hang(*_args, **_kwargs):
            await asyncio.Future()

        with self.playwright_patch(), patch.object(
            register_claude_api, "run_claude_platform_flow", side_effect=hang
        ), patch.object(
            register_claude_api,
            "EMERGENCY_TIMEOUT_CUSHION",
            0.01,
            create=True,
        ):
            result = await asyncio.wait_for(
                register_claude_api.register_one(
                    self.bb, self.account, self.store, timeout=0.01
                ),
                timeout=0.1,
            )

        self.assertIsNone(result)
        self.store.mark_error.assert_called_once_with(self.account, "timeout")

    def make_cleanup_failures(self):
        self.browser.close.side_effect = RuntimeError("browser close secret")
        self.bb.close_browser.side_effect = RuntimeError("profile close secret")
        self.bb.delete_browser.side_effect = RuntimeError("profile delete secret")

    def assert_all_cleanup_attempted(self):
        self.browser.close.assert_awaited_once_with()
        self.bb.close_browser.assert_called_once_with("profile-a")
        self.bb.delete_browser.assert_called_once_with("profile-a")

    async def test_cleanup_errors_do_not_change_success(self):
        self.make_cleanup_failures()
        output = io.StringIO()
        with self.playwright_patch(), patch.object(
            register_claude_api,
            "run_claude_platform_flow",
            new=AsyncMock(return_value="cookies/session.json"),
        ), redirect_stdout(output):
            result = await register_claude_api.register_one(
                self.bb, self.account, self.store, timeout=90
            )

        self.assertEqual(result, "cookies/session.json")
        self.store.mark_used.assert_called_once_with(self.account)
        self.assert_all_cleanup_attempted()
        self.assertNotIn("secret", output.getvalue())

    async def test_cleanup_errors_do_not_change_stable_failure(self):
        self.make_cleanup_failures()
        with self.playwright_patch(), patch.object(
            register_claude_api,
            "run_claude_platform_flow",
            new=AsyncMock(
                side_effect=register_claude_api.ClaudeApiRegistrationError(
                    "verification_rejected"
                )
            ),
        ):
            result = await register_claude_api.register_one(
                self.bb, self.account, self.store, timeout=90
            )

        self.assertIsNone(result)
        self.store.mark_error.assert_called_once_with(
            self.account, "verification_rejected"
        )
        self.assert_all_cleanup_attempted()

    async def test_cleanup_errors_do_not_replace_cancellation(self):
        self.make_cleanup_failures()
        started = asyncio.Event()

        async def hang(*_args, **_kwargs):
            started.set()
            await asyncio.Future()

        with self.playwright_patch(), patch.object(
            register_claude_api, "run_claude_platform_flow", side_effect=hang
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
        self.assert_all_cleanup_attempted()

    async def test_hung_browser_close_is_bounded_and_later_cleanup_runs(self):
        close_started = asyncio.Event()
        close_release = asyncio.Event()
        close_cancellations = 0

        async def hung_close():
            nonlocal close_cancellations
            close_started.set()
            while not close_release.is_set():
                try:
                    await close_release.wait()
                except asyncio.CancelledError:
                    close_cancellations += 1

        self.browser.close.side_effect = hung_close
        output = io.StringIO()
        with self.playwright_patch(), patch.object(
            register_claude_api,
            "run_claude_platform_flow",
            new=AsyncMock(return_value="cookies/session.json"),
        ), patch.object(
            register_claude_api,
            "CLEANUP_OPERATION_TIMEOUT",
            0.005,
            create=True,
        ), patch.object(
            register_claude_api,
            "TASK_CANCEL_GRACE",
            0.005,
            create=True,
        ), redirect_stdout(output):
            started_at = asyncio.get_running_loop().time()
            registration = asyncio.create_task(
                register_claude_api.register_one(
                    self.bb, self.account, self.store, timeout=1
                )
            )
            await close_started.wait()
            done, _pending = await asyncio.wait(
                {registration}, timeout=0.08
            )
            elapsed = asyncio.get_running_loop().time() - started_at
            try:
                self.assertIn(registration, done)
                self.assertEqual(registration.result(), "cookies/session.json")
                self.assertLess(elapsed, 0.06)
                self.assertGreaterEqual(close_cancellations, 2)
                self.bb.close_browser.assert_called_once_with("profile-a")
                self.bb.delete_browser.assert_called_once_with("profile-a")
                self.store.mark_used.assert_called_once_with(self.account)
                self.assertIn("browser_cleanup_failed", output.getvalue())
                self.assertNotIn("mail-pass", output.getvalue())
            finally:
                close_release.set()
                if not registration.done():
                    registration.cancel()
                await asyncio.gather(registration, return_exceptions=True)
                await self.drain_background_tasks()

    async def test_hung_profile_close_is_bounded_and_delete_preserves_failure(self):
        close_release = threading.Event()
        timer = threading.Timer(0.15, close_release.set)
        timer.start()

        def hung_profile_close(_profile_id):
            close_release.wait()

        self.bb.close_browser.side_effect = hung_profile_close
        output = io.StringIO()
        try:
            with self.playwright_patch(), patch.object(
                register_claude_api,
                "run_claude_platform_flow",
                new=AsyncMock(
                    side_effect=register_claude_api.ClaudeApiRegistrationError(
                        "verification_rejected"
                    )
                ),
            ), patch.object(
                register_claude_api,
                "CLEANUP_OPERATION_TIMEOUT",
                0.005,
                create=True,
            ), patch.object(
                register_claude_api,
                "TASK_CANCEL_GRACE",
                0.005,
                create=True,
            ), redirect_stdout(output):
                started_at = time.monotonic()
                result = await register_claude_api.register_one(
                    self.bb, self.account, self.store, timeout=1
                )
                elapsed = time.monotonic() - started_at

            self.assertIsNone(result)
            self.assertLess(elapsed, 0.08)
            self.bb.delete_browser.assert_called_once_with("profile-a")
            self.store.mark_error.assert_called_once_with(
                self.account, "verification_rejected"
            )
            self.assertIn("profile_close_failed", output.getvalue())
            self.assertNotIn("mail-pass", output.getvalue())
        finally:
            close_release.set()
            timer.cancel()
            timer.join(timeout=0.3)
            await self.drain_background_tasks()

    async def test_hung_profile_delete_is_bounded_and_preserves_cancellation(self):
        delete_release = threading.Event()
        timer = threading.Timer(0.15, delete_release.set)
        timer.start()
        flow_started = asyncio.Event()

        def hung_profile_delete(_profile_id):
            delete_release.wait()

        async def blocked_flow(*_args, **_kwargs):
            flow_started.set()
            await asyncio.Future()

        self.bb.delete_browser.side_effect = hung_profile_delete
        output = io.StringIO()
        try:
            with self.playwright_patch(), patch.object(
                register_claude_api,
                "run_claude_platform_flow",
                side_effect=blocked_flow,
            ), patch.object(
                register_claude_api,
                "CLEANUP_OPERATION_TIMEOUT",
                0.005,
                create=True,
            ), patch.object(
                register_claude_api,
                "TASK_CANCEL_GRACE",
                0.005,
                create=True,
            ), redirect_stdout(output):
                registration = asyncio.create_task(
                    register_claude_api.register_one(
                        self.bb, self.account, self.store, timeout=1
                    )
                )
                await flow_started.wait()
                started_at = time.monotonic()
                registration.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await registration
                elapsed = time.monotonic() - started_at

            self.assertLess(elapsed, 0.08)
            self.store.release.assert_called_once_with(self.account)
            self.assertIn("profile_delete_failed", output.getvalue())
            self.assertNotIn("mail-pass", output.getvalue())
        finally:
            delete_release.set()
            timer.cancel()
            timer.join(timeout=0.3)
            await self.drain_background_tasks()


class ClaudeApiMainBatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.accounts = [
            ClaudeEmailAccount(
                "NINEMALL",
                f"person{index}@example.com",
                f"mail-pass-{index}",
                f"client-{index}",
                f"refresh-{index}",
            )
            for index in range(3)
        ]
        self.store = Mock()
        self.store.reserve_many.return_value = self.accounts

    def main_stack(self, argv):
        return (
            patch.object(register_claude_api.sys, "argv", argv),
            patch.object(register_claude_api, "EMAIL_PROVIDER", "NINEMALL"),
            patch.object(
                register_claude_api,
                "ClaudeEmailAccountStore",
                return_value=self.store,
            ),
            patch.object(register_claude_api, "lease_from_env", return_value=None),
            patch.object(
                register_claude_api,
                "settings_from_env",
                return_value=SimpleNamespace(enabled=False),
            ),
            patch.object(register_claude_api, "BitBrowser", return_value=Mock()),
        )

    async def test_main_enforces_multi_account_concurrency(self):
        active = 0
        maximum = 0
        first_pair = asyncio.Event()
        release = asyncio.Event()

        async def registration(*_args, **_kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                first_pair.set()
            await release.wait()
            active -= 1
            return "cookies/session.json"

        stack = self.main_stack([
            "register_claude_api.py", "--count", "3", "--concurrency", "2"
        ])
        with stack[0], stack[1], stack[2], stack[3], stack[4], stack[5], patch.object(
            register_claude_api, "register_one", side_effect=registration
        ):
            task = asyncio.create_task(register_claude_api.main())
            await asyncio.wait_for(first_pair.wait(), timeout=0.2)
            self.assertEqual(maximum, 2)
            release.set()
            status = await task

        self.assertEqual(status, 0)
        self.assertEqual(maximum, 2)

    async def test_ctrl_c_releases_each_queued_reservation_once(self):
        started = asyncio.Event()

        async def registration(_bb, selected, store, *_args, **_kwargs):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                store.release(selected)
                raise

        stack = self.main_stack([
            "register_claude_api.py", "--count", "3", "--concurrency", "1"
        ])
        with stack[0], stack[1], stack[2], stack[3], stack[4], stack[5], patch.object(
            register_claude_api, "register_one", side_effect=registration
        ):
            task = asyncio.create_task(register_claude_api.main())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        for selected in self.accounts:
            self.assertEqual(
                self.store.release.call_args_list.count(call(selected)), 1
            )

    async def test_ipmart_acquires_and_propagates_one_lease_per_account(self):
        leases = [
            lease(),
            ProxyLease(
                "http", "gateway.example", 8080,
                "account-res-US-sid-00000043", "proxy-secret-2",
                "00000043", "203.0.113.9",
            ),
            ProxyLease(
                "http", "gateway.example", 8080,
                "account-res-US-sid-00000044", "proxy-secret-3",
                "00000044", "203.0.113.10",
            ),
        ]
        stack = self.main_stack([
            "register_claude_api.py", "--count", "3", "--concurrency", "3"
        ])
        output = io.StringIO()
        with stack[0], stack[1], stack[2], stack[3], patch.object(
            register_claude_api,
            "settings_from_env",
            return_value=SimpleNamespace(enabled=True),
        ), stack[5], patch.object(
            register_claude_api, "acquire_proxy", side_effect=leases
        ) as acquire, patch.object(
            register_claude_api,
            "register_one",
            new=AsyncMock(return_value="cookies/session.json"),
        ) as register, redirect_stdout(output):
            status = await register_claude_api.main()

        self.assertEqual(status, 0)
        self.assertEqual(acquire.call_count, 3)
        self.assertEqual(
            {id(item.kwargs["account_lease"]) for item in register.await_args_list},
            {id(item) for item in leases},
        )
        for secret in ("proxy-secret", "proxy-secret-2", "proxy-secret-3"):
            self.assertNotIn(secret, output.getvalue())

    async def test_final_marker_matches_mixed_results(self):
        stack = self.main_stack([
            "register_claude_api.py", "--count", "3", "--concurrency", "2"
        ])
        output = io.StringIO()
        with stack[0], stack[1], stack[2], stack[3], stack[4], stack[5], patch.object(
            register_claude_api,
            "register_one",
            new=AsyncMock(side_effect=["one.json", None, "three.json"]),
        ), redirect_stdout(output):
            status = await register_claude_api.main()

        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue().splitlines()[-1], "success: 2/3")


if __name__ == "__main__":
    unittest.main()
