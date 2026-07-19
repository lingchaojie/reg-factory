import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from nexacard_otp.browser import NativeChromeManager, chrome_args, direct_browser_env


def make_settings(fingerprint=("chrome.exe", True, "account", "mail@example.com")):
    settings = Mock()
    settings.browser_fingerprint = fingerprint
    settings.chrome_path = Path(fingerprint[0])
    settings.headless = fingerprint[1]
    return settings


class NexaCardBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_transition(self, manager):
        for _ in range(20):
            if manager._transitioning:
                return
            await asyncio.sleep(0)
        self.fail("browser manager did not enter transition state")

    async def test_cancelled_context_transition_releases_waiters_and_preserves_old_context(self):
        old_settings = make_settings()
        new_settings = make_settings(
            ("chrome.exe", False, "account", "mail@example.com")
        )
        old_context = AsyncMock()
        old_context.new_page.side_effect = [AsyncMock(), AsyncMock()]
        playwright = AsyncMock()
        playwright.chromium.launch_persistent_context.return_value = old_context
        manager = NativeChromeManager(
            playwright_factory=AsyncMock(return_value=playwright)
        )
        active = manager.page(old_settings)
        await active.__aenter__()

        transition = asyncio.create_task(manager.page(new_settings).__aenter__())
        await self._wait_for_transition(manager)
        transition.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await transition

        self.assertIs(manager._context, old_context)
        old_context.close.assert_not_awaited()
        await active.__aexit__(None, None, None)
        async with asyncio.timeout(0.2):
            async with manager.page(old_settings):
                pass
            await manager.close()

    async def test_cancelled_close_releases_waiters_and_allows_later_close(self):
        settings = make_settings()
        context = AsyncMock()
        context.new_page.side_effect = [AsyncMock(), AsyncMock()]
        playwright = AsyncMock()
        playwright.chromium.launch_persistent_context.return_value = context
        manager = NativeChromeManager(
            playwright_factory=AsyncMock(return_value=playwright)
        )
        active = manager.page(settings)
        await active.__aenter__()

        closing = asyncio.create_task(manager.close())
        await self._wait_for_transition(manager)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing

        self.assertIs(manager._context, context)
        await active.__aexit__(None, None, None)
        async with asyncio.timeout(0.2):
            async with manager.page(settings):
                pass
            await manager.close()

        context.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    def test_direct_env_removes_only_standard_proxy_keys_case_insensitively(self):
        source = {
            "HTTP_PROXY": "http://proxy",
            "HtTp_PrOxY": "http://proxy",
            "https_proxy": "http://proxy",
            "ALL_PROXY": "socks://proxy",
            "No_PrOxY": "localhost",
            "fTp_PrOxY": "ftp://proxy",
            "PROXY_VENDOR_API_KEY": "keep-this-business-value",
            "NEXACARD_PROXY_METRICS": "keep-this-business-value",
            "KEEP_ME": "yes",
        }

        result = direct_browser_env(source)

        self.assertEqual(
            result,
            {
                "PROXY_VENDOR_API_KEY": "keep-this-business-value",
                "NEXACARD_PROXY_METRICS": "keep-this-business-value",
                "KEEP_ME": "yes",
            },
        )

    def test_direct_env_defaults_to_process_environment_without_mutating_it(self):
        with patch.dict(os.environ, {"hTtP_pRoXy": "http://proxy", "KEEP_ME": "yes"}, clear=True):
            result = direct_browser_env()
            self.assertEqual(os.environ["hTtP_pRoXy"], "http://proxy")
        self.assertEqual(result, {"KEEP_ME": "yes"})

    def test_chrome_args_force_direct_network(self):
        self.assertEqual(
            chrome_args(),
            ["--no-proxy-server", "--proxy-server=direct://", "--proxy-bypass-list=*"],
        )

    async def test_launches_installed_chrome_with_private_profile_headless_and_direct_env(self):
        settings = make_settings()
        context = AsyncMock()
        context.new_page.return_value = AsyncMock()
        playwright = AsyncMock()
        playwright.chromium.launch_persistent_context.return_value = context
        manager = NativeChromeManager(playwright_factory=AsyncMock(return_value=playwright))

        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "chrome-profile"
            with patch("nexacard_otp.browser.CHROME_PROFILE_DIR", profile), patch.dict(
                os.environ, {"hTtP_pRoXy": "http://proxy", "KEEP_ME": "yes"}, clear=True
            ):
                async with manager.page(settings):
                    pass

        kwargs = playwright.chromium.launch_persistent_context.await_args.kwargs
        self.assertEqual(kwargs["user_data_dir"], str(profile))
        self.assertEqual(kwargs["executable_path"], "chrome.exe")
        self.assertTrue(kwargs["headless"])
        self.assertEqual(kwargs["args"], chrome_args())
        self.assertEqual(kwargs["env"], {"KEEP_ME": "yes"})
        self.assertNotIn("proxy", kwargs)

    async def test_same_fingerprint_reuses_context_and_each_request_gets_a_page(self):
        settings = make_settings()
        context = AsyncMock()
        context.new_page.side_effect = [AsyncMock(), AsyncMock()]
        playwright = AsyncMock()
        playwright.chromium.launch_persistent_context.return_value = context
        manager = NativeChromeManager(playwright_factory=AsyncMock(return_value=playwright))

        async with manager.page(settings):
            pass
        async with manager.page(settings):
            pass

        self.assertEqual(playwright.chromium.launch_persistent_context.await_count, 1)
        self.assertEqual(context.new_page.await_count, 2)

    async def test_changed_fingerprint_closes_and_recreates_context(self):
        first = AsyncMock()
        first.new_page.return_value = AsyncMock()
        second = AsyncMock()
        second.new_page.return_value = AsyncMock()
        playwright = AsyncMock()
        playwright.chromium.launch_persistent_context.side_effect = [first, second]
        manager = NativeChromeManager(playwright_factory=AsyncMock(return_value=playwright))

        async with manager.page(make_settings()):
            pass
        async with manager.page(make_settings(("chrome.exe", False, "account", "mail@example.com"))):
            pass

        self.assertEqual(playwright.chromium.launch_persistent_context.await_count, 2)
        first.close.assert_awaited_once()

    async def test_page_is_closed_when_request_raises(self):
        page = AsyncMock()
        context = AsyncMock()
        context.new_page.return_value = page
        playwright = AsyncMock()
        playwright.chromium.launch_persistent_context.return_value = context
        manager = NativeChromeManager(playwright_factory=AsyncMock(return_value=playwright))

        with self.assertRaisesRegex(RuntimeError, "lookup failed"):
            async with manager.page(make_settings()):
                raise RuntimeError("lookup failed")

        page.close.assert_awaited_once()

    async def test_concurrent_first_use_launches_once_and_hands_out_two_pages(self):
        launch_started = asyncio.Event()
        allow_launch = asyncio.Event()
        context = AsyncMock()
        context.new_page.side_effect = [AsyncMock(), AsyncMock()]
        playwright = AsyncMock()

        async def launch(**_kwargs):
            launch_started.set()
            await allow_launch.wait()
            return context

        playwright.chromium.launch_persistent_context.side_effect = launch
        factory = AsyncMock(return_value=playwright)
        manager = NativeChromeManager(playwright_factory=factory)

        async def request_page():
            async with manager.page(make_settings()):
                pass

        first = asyncio.create_task(request_page())
        await launch_started.wait()
        second = asyncio.create_task(request_page())
        await asyncio.sleep(0)
        allow_launch.set()
        await asyncio.gather(first, second)

        self.assertEqual(factory.await_count, 1)
        self.assertEqual(playwright.chromium.launch_persistent_context.await_count, 1)
        self.assertEqual(context.new_page.await_count, 2)

    async def test_close_is_idempotent_and_partial_launch_failure_is_cleaned_up(self):
        playwright = AsyncMock()
        playwright.chromium.launch_persistent_context.side_effect = RuntimeError("launch failed")
        manager = NativeChromeManager(playwright_factory=AsyncMock(return_value=playwright))

        with self.assertRaisesRegex(RuntimeError, "launch failed"):
            async with manager.page(make_settings()):
                pass
        await manager.close()
        await manager.close()

        self.assertIsNone(manager._context)
        self.assertIsNone(manager._playwright)
        playwright.stop.assert_awaited_once()

    async def test_close_stops_playwright_and_resets_state_when_context_close_fails(self):
        context = AsyncMock()
        context.new_page.return_value = AsyncMock()
        context.close.side_effect = RuntimeError("context close failed")
        playwright = AsyncMock()
        playwright.chromium.launch_persistent_context.return_value = context
        manager = NativeChromeManager(playwright_factory=AsyncMock(return_value=playwright))

        async with manager.page(make_settings()):
            pass
        with self.assertRaisesRegex(RuntimeError, "context close failed"):
            await manager.close()
        await manager.close()

        self.assertIsNone(manager._context)
        self.assertIsNone(manager._playwright)
        playwright.stop.assert_awaited_once()
