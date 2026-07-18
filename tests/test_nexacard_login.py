import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from google.auth.exceptions import RefreshError
from playwright.async_api import Error as PlaywrightError

from nexacard_otp.errors import (
    GmailAuthorizationRequired,
    GmailTemporarilyUnavailable,
    NexaCardLoginFailed,
)
from nexacard_otp.login import BASE_URL, NexaCardLogin


LOGIN_URL = "https://www.nexacardvcc.com/login"
AUTHENTICATED_URL = "https://www.nexacardvcc.com/nova-v-card-b/verify-code"
HASH_LOGIN_URL = f"{BASE_URL}/#/login"
PROTECTED_PROBE_URL = f"{BASE_URL}/#/nova-v-card-b/verify-code"
USERNAME = 'input[placeholder="请输入用户名"]'
PASSWORD = 'input[placeholder="请输入密码"]'
EMAIL = 'input[placeholder="请输入邮箱"]'
EMAIL_CODE = 'input[placeholder="请输入邮箱验证码"]'


def make_settings():
    return Mock(account="account-123", password="password-456", verification_email="owner@example.com")


def logged_out_page():
    page = Mock()
    page.url = LOGIN_URL
    page.goto = AsyncMock()
    page.wait_for_url = AsyncMock()
    locators = {}
    for selector in (USERNAME, PASSWORD, EMAIL, EMAIL_CODE, ".el-radio", "button.get-code-btn", "button.submit-btn"):
        locator = Mock()
        locator.count = AsyncMock(return_value=1)
        locator.fill = AsyncMock()
        locator.click = AsyncMock()
        locator.nth = Mock(return_value=locator)
        locators[selector] = locator
    page.locator.side_effect = locators.__getitem__
    return page, locators


class NexaCardLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_hash_login_url_is_logged_out_without_querying_login_fields(self):
        page = Mock()
        page.url = HASH_LOGIN_URL
        page.locator = Mock()

        logged_out = await NexaCardLogin(asyncio.Lock(), AsyncMock())._is_logged_out(page)

        self.assertTrue(logged_out)
        page.locator.assert_not_called()

    async def test_authenticated_page_does_not_touch_login_or_gmail(self):
        page = Mock()
        page.url = AUTHENTICATED_URL
        page.goto = AsyncMock()
        page.locator = Mock()
        reader = AsyncMock()

        recovered = await NexaCardLogin(asyncio.Lock(), reader).ensure_authenticated(page, Mock())

        self.assertFalse(recovered)
        page.goto.assert_not_awaited()
        page.locator.assert_not_called()
        reader.wait_for_login_code.assert_not_called()

    async def test_logged_out_page_uses_native_controls_and_submits_fresh_email_code(self):
        page, locators = logged_out_page()
        reader = AsyncMock()
        reader.wait_for_login_code.return_value = "123456789"
        sent_at = datetime(2026, 7, 19, 5, 0, tzinfo=timezone.utc)

        with patch("nexacard_otp.login.datetime") as clock:
            clock.now.return_value = sent_at
            recovered = await NexaCardLogin(asyncio.Lock(), reader).ensure_authenticated(
                page, make_settings()
            )

        self.assertTrue(recovered)
        self.assertEqual(page.goto.await_count, 2)
        page.goto.assert_any_await(
            PROTECTED_PROBE_URL, wait_until="domcontentloaded", timeout=30_000
        )
        page.goto.assert_any_await(
            HASH_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000
        )
        locators[USERNAME].fill.assert_awaited_once_with("account-123", timeout=30_000)
        locators[PASSWORD].fill.assert_awaited_once_with("password-456", timeout=30_000)
        locators[".el-radio"].nth.assert_called_once_with(1)
        locators[".el-radio"].click.assert_awaited_once_with(timeout=30_000)
        locators[EMAIL].fill.assert_awaited_once_with("owner@example.com", timeout=30_000)
        reader.wait_for_login_code.assert_awaited_once_with(sent_at)
        locators[EMAIL_CODE].fill.assert_awaited_once_with("123456789", timeout=30_000)
        page.wait_for_url.assert_awaited_once()
        selectors = [call.args[0] for call in page.locator.call_args_list]
        self.assertEqual(
            selectors,
            [USERNAME, PASSWORD, ".el-radio", EMAIL, "button.get-code-btn", EMAIL_CODE, "button.submit-btn"],
        )

    async def test_hash_login_requires_protected_route_after_submit(self):
        page, locators = logged_out_page()
        page.url = HASH_LOGIN_URL
        reader = AsyncMock()
        reader.wait_for_login_code.return_value = "123456789"

        async def wait_for_url(predicate, **_kwargs):
            self.assertFalse(predicate(HASH_LOGIN_URL))
            page.url = PROTECTED_PROBE_URL
            self.assertTrue(predicate(page.url))

        page.wait_for_url.side_effect = wait_for_url

        recovered = await NexaCardLogin(asyncio.Lock(), reader).ensure_authenticated(
            page, make_settings()
        )

        self.assertTrue(recovered)
        page.goto.assert_any_await(
            PROTECTED_PROBE_URL, wait_until="domcontentloaded", timeout=30_000
        )

    async def test_missing_login_configuration_fails_without_visiting_page_or_gmail(self):
        page, _ = logged_out_page()
        reader = AsyncMock()
        settings = Mock(account="", password="password-456", verification_email="owner@example.com")

        with self.assertRaisesRegex(NexaCardLoginFailed, "configuration is incomplete") as captured:
            await NexaCardLogin(asyncio.Lock(), reader).ensure_authenticated(page, settings)

        self.assertNotIn("password-456", str(captured.exception))
        self.assertNotIn("owner@example.com", str(captured.exception))
        page.goto.assert_not_awaited()
        reader.wait_for_login_code.assert_not_called()

    async def test_gmail_timeout_maps_to_safe_login_failure(self):
        page, _ = logged_out_page()
        reader = AsyncMock()
        reader.wait_for_login_code.side_effect = TimeoutError("123456789")

        with self.assertRaisesRegex(NexaCardLoginFailed, "verification email timed out") as captured:
            await NexaCardLogin(asyncio.Lock(), reader).ensure_authenticated(page, make_settings())

        self.assertIsInstance(captured.exception.__cause__, TimeoutError)
        self.assertNotIn("123456789", str(captured.exception))

    async def test_gmail_authorization_failure_maps_to_safe_login_failure(self):
        page, _ = logged_out_page()
        reader = AsyncMock()
        reader.wait_for_login_code.side_effect = GmailAuthorizationRequired("owner@example.com")

        with self.assertRaisesRegex(NexaCardLoginFailed, "Gmail authorization is required") as captured:
            await NexaCardLogin(asyncio.Lock(), reader).ensure_authenticated(page, make_settings())

        self.assertIsInstance(captured.exception.__cause__, GmailAuthorizationRequired)
        self.assertNotIn("owner@example.com", str(captured.exception))

    async def test_gmail_temporary_failure_maps_to_safe_login_failure(self):
        page, _ = logged_out_page()
        reader = AsyncMock()
        reader.wait_for_login_code.side_effect = GmailTemporarilyUnavailable("access token")

        with self.assertRaisesRegex(NexaCardLoginFailed, "Gmail is temporarily unavailable") as captured:
            await NexaCardLogin(asyncio.Lock(), reader).ensure_authenticated(page, make_settings())

        self.assertIsInstance(captured.exception.__cause__, GmailTemporarilyUnavailable)
        self.assertNotIn("access token", str(captured.exception))

    async def test_playwright_failure_maps_to_safe_login_failure(self):
        page, _ = logged_out_page()
        page.goto.side_effect = PlaywrightError("password-456")
        reader = AsyncMock()

        with self.assertRaisesRegex(NexaCardLoginFailed, "NexaCard page operation failed") as captured:
            await NexaCardLogin(asyncio.Lock(), reader).ensure_authenticated(page, make_settings())

        self.assertIsInstance(captured.exception.__cause__, PlaywrightError)
        self.assertNotIn("password-456", str(captured.exception))
        reader.wait_for_login_code.assert_not_called()

    async def test_submit_navigation_failure_maps_to_safe_login_failure(self):
        page, _ = logged_out_page()
        page.wait_for_url.side_effect = PlaywrightError("login did not complete")
        reader = AsyncMock()
        reader.wait_for_login_code.return_value = "123456789"

        with self.assertRaisesRegex(NexaCardLoginFailed, "did not reach an authenticated page") as captured:
            await NexaCardLogin(asyncio.Lock(), reader).ensure_authenticated(page, make_settings())

        self.assertIsInstance(captured.exception.__cause__, PlaywrightError)

    async def test_second_caller_rechecks_after_lock_and_skips_duplicate_recovery(self):
        lock = asyncio.Lock()
        login = NexaCardLogin(lock, AsyncMock())
        state = {"logged_out": True, "initial_checks": 0}
        both_initial_checks_complete = asyncio.Event()

        async def is_logged_out(_page):
            if state["initial_checks"] < 2:
                state["initial_checks"] += 1
                if state["initial_checks"] == 2:
                    both_initial_checks_complete.set()
                await both_initial_checks_complete.wait()
                return True
            return state["logged_out"]

        async def perform_login(_page, _settings):
            state["logged_out"] = False

        login._is_logged_out = AsyncMock(side_effect=is_logged_out)
        login._navigate_for_recheck = AsyncMock()
        login._perform_login = AsyncMock(side_effect=perform_login)

        results = await asyncio.gather(
            login.ensure_authenticated(AsyncMock(), Mock()),
            login.ensure_authenticated(AsyncMock(), Mock()),
        )

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)
        self.assertEqual(login._perform_login.await_count, 1)
        self.assertEqual(login._navigate_for_recheck.await_count, 2)

    async def test_shared_cookie_recheck_uses_protected_route_and_skips_second_login(self):
        state = {"authenticated": False}
        first_probe_entered = asyncio.Event()
        release_first_probe = asyncio.Event()
        navigations = []

        def make_page(name):
            page, locators = logged_out_page()
            page.url = HASH_LOGIN_URL

            async def goto(url, **_kwargs):
                navigations.append((name, url))
                if url == PROTECTED_PROBE_URL:
                    if name == "first" and not state["authenticated"]:
                        first_probe_entered.set()
                        await release_first_probe.wait()
                    page.url = PROTECTED_PROBE_URL if state["authenticated"] else HASH_LOGIN_URL
                elif url == HASH_LOGIN_URL:
                    page.url = HASH_LOGIN_URL

            async def wait_for_url(predicate, **_kwargs):
                page.url = PROTECTED_PROBE_URL
                self.assertTrue(predicate(page.url))

            async def submit(**_kwargs):
                state["authenticated"] = True

            page.goto.side_effect = goto
            page.wait_for_url.side_effect = wait_for_url
            locators["button.submit-btn"].click.side_effect = submit
            return page

        login = NexaCardLogin(asyncio.Lock(), AsyncMock(wait_for_login_code=AsyncMock(return_value="123456789")))
        first = asyncio.create_task(login.ensure_authenticated(make_page("first"), make_settings()))
        try:
            await asyncio.wait_for(first_probe_entered.wait(), timeout=0.1)
            second = asyncio.create_task(login.ensure_authenticated(make_page("second"), make_settings()))
            await asyncio.sleep(0)
            release_first_probe.set()

            self.assertEqual(await first, True)
            self.assertEqual(await second, False)
            self.assertEqual(
                [url for _name, url in navigations if url == PROTECTED_PROBE_URL],
                [PROTECTED_PROBE_URL, PROTECTED_PROBE_URL],
            )
        finally:
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    async def test_login_timestamp_is_aware_utc_and_captured_before_request_click(self):
        page, locators = logged_out_page()
        reader = AsyncMock()
        reader.wait_for_login_code.return_value = "123456789"
        sent_at = datetime(2026, 7, 19, 5, 0, tzinfo=timezone.utc)
        click_observed_timestamp = []

        async def click(*_args, **_kwargs):
            click_observed_timestamp.append(reader.wait_for_login_code.await_count)

        locators["button.get-code-btn"].click.side_effect = click
        with patch("nexacard_otp.login.datetime") as clock:
            clock.now.return_value = sent_at
            await NexaCardLogin(asyncio.Lock(), reader).ensure_authenticated(page, make_settings())

        received_timestamp = reader.wait_for_login_code.await_args.args[0]
        self.assertEqual(received_timestamp, sent_at)
        self.assertIs(received_timestamp.tzinfo, timezone.utc)
        self.assertEqual(click_observed_timestamp[0], 0)

    async def test_gmail_refresh_error_maps_to_safe_login_failure(self):
        page, _ = logged_out_page()
        reader = AsyncMock()
        reader.wait_for_login_code.side_effect = RefreshError("token=secret-token")

        with self.assertRaisesRegex(NexaCardLoginFailed, "Gmail authorization is required") as captured:
            await NexaCardLogin(asyncio.Lock(), reader).ensure_authenticated(page, make_settings())

        self.assertIsInstance(captured.exception.__cause__, RefreshError)
        self.assertNotIn("secret-token", str(captured.exception))
