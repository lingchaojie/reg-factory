import asyncio
from datetime import datetime, timezone
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .errors import NexaCardLoginFailed
from .gmail_reader import GmailCodeReader
from .settings import Settings


BASE_URL = "https://www.nexacardvcc.com"
LOGIN_URL = f"{BASE_URL}/#/login"
PROTECTED_PROBE_URL = f"{BASE_URL}/#/nova-v-card-b/verify-code"
AUTHENTICATED_ROUTE_ROOTS = (
    "/nova-v-card-b",
    "/3d-1-card",
    "/wallet/my-wallet",
    "/virtual-card/list",
)
PAGE_TIMEOUT_MS = 30_000
USERNAME_INPUT = 'input[placeholder="请输入用户名"]'
PASSWORD_INPUT = 'input[placeholder="请输入密码"]'
VERIFICATION_EMAIL_INPUT = 'input[placeholder="请输入邮箱"]'
VERIFICATION_CODE_INPUT = 'input[placeholder="请输入邮箱验证码"]'
EMAIL_LOGIN_RADIO = ".el-radio"
REQUEST_CODE_BUTTON = "button.get-code-btn"
SUBMIT_BUTTON = "button.submit-btn"
PROTECTED_CARD_SEARCH = "input[placeholder='请输入卡号']"
PROBE_STATE_SCRIPT = f"""
() => Boolean(
  document.querySelector({USERNAME_INPUT!r})
  || document.querySelector({PROTECTED_CARD_SEARCH!r})
)
"""


class NexaCardLogin:
    """Recover an expired NexaCard session only during a caller's lookup flow."""

    def __init__(self, login_lock: asyncio.Lock, gmail_reader: GmailCodeReader) -> None:
        self._login_lock = login_lock
        self._gmail_reader = gmail_reader

    @staticmethod
    def _route(url: str) -> str:
        parsed = urlsplit(url)
        route = parsed.fragment or parsed.path
        return route.split("?", 1)[0].rstrip("/")

    @classmethod
    def _is_login_url(cls, url: str) -> bool:
        return cls._route(url) == "/login"

    @staticmethod
    def _is_route_at_or_below(route: str, root: str) -> bool:
        return route == root or route.startswith(f"{root}/")

    @classmethod
    def _is_authenticated_url(cls, url: str) -> bool:
        route = cls._route(url)
        return any(cls._is_route_at_or_below(route, root) for root in AUTHENTICATED_ROUTE_ROOTS)

    async def _is_logged_out(self, page: Page) -> bool:
        if self._is_login_url(page.url):
            return True
        if self._is_authenticated_url(page.url):
            return False
        try:
            return await page.locator(USERNAME_INPUT).count() > 0
        except PlaywrightError as exc:
            raise NexaCardLoginFailed("NexaCard page operation failed") from exc

    @staticmethod
    def _require_login_settings(settings: Settings) -> None:
        if not settings.account or not settings.password or not settings.verification_email:
            raise NexaCardLoginFailed("NexaCard login configuration is incomplete")

    async def _navigate_for_recheck(self, page: Page) -> None:
        try:
            await page.goto(
                PROTECTED_PROBE_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS
            )
        except PlaywrightError as exc:
            raise NexaCardLoginFailed("NexaCard page operation failed") from exc

    async def _probe_is_logged_out(self, page: Page) -> bool:
        """Wait for the SPA guard to render login or protected-page evidence."""
        try:
            await page.wait_for_function(PROBE_STATE_SCRIPT, timeout=PAGE_TIMEOUT_MS)
            if await page.locator(USERNAME_INPUT).count() > 0:
                return True
            if await page.locator(PROTECTED_CARD_SEARCH).count() > 0:
                return False
        except PlaywrightTimeoutError as exc:
            raise NexaCardLoginFailed("NexaCard authentication check timed out") from exc
        except PlaywrightError as exc:
            raise NexaCardLoginFailed("NexaCard page operation failed") from exc
        raise NexaCardLoginFailed("NexaCard authentication state is unavailable")

    async def _snapshot_login_message_ids(
        self, expected_email: str
    ) -> frozenset[str]:
        return await self._gmail_reader.snapshot_login_message_ids(
            expected_email=expected_email
        )

    async def _wait_for_login_code(
        self,
        sent_after: datetime,
        excluded_message_ids: frozenset[str],
        expected_email: str,
    ) -> str:
        try:
            return await self._gmail_reader.wait_for_login_code(
                sent_after,
                excluded_message_ids=excluded_message_ids,
                expected_email=expected_email,
            )
        except TimeoutError as exc:
            raise NexaCardLoginFailed("NexaCard login verification email timed out") from exc

    async def _perform_login(self, page: Page, settings: Settings) -> None:
        self._require_login_settings(settings)
        try:
            await page.goto(
                LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS
            )
            await page.locator(USERNAME_INPUT).fill(settings.account, timeout=PAGE_TIMEOUT_MS)
            await page.locator(PASSWORD_INPUT).fill(settings.password, timeout=PAGE_TIMEOUT_MS)
            await page.locator(EMAIL_LOGIN_RADIO).nth(1).click(timeout=PAGE_TIMEOUT_MS)
            await page.locator(VERIFICATION_EMAIL_INPUT).fill(
                settings.verification_email, timeout=PAGE_TIMEOUT_MS
            )
        except PlaywrightError as exc:
            raise NexaCardLoginFailed("NexaCard page operation failed") from exc

        excluded_message_ids = await self._snapshot_login_message_ids(
            settings.verification_email
        )
        sent_after = datetime.now(timezone.utc)
        try:
            await page.locator(REQUEST_CODE_BUTTON).click(timeout=PAGE_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise NexaCardLoginFailed("NexaCard page operation failed") from exc

        code = await self._wait_for_login_code(
            sent_after, excluded_message_ids, settings.verification_email
        )

        try:
            await page.locator(VERIFICATION_CODE_INPUT).fill(code, timeout=PAGE_TIMEOUT_MS)
            await page.locator(SUBMIT_BUTTON).click(timeout=PAGE_TIMEOUT_MS)
            await page.wait_for_url(
                lambda url: self._is_authenticated_url(str(url)), timeout=PAGE_TIMEOUT_MS
            )
        except PlaywrightError as exc:
            raise NexaCardLoginFailed("NexaCard login did not reach an authenticated page") from exc

    async def ensure_authenticated(
        self,
        page: Page,
        settings: Settings,
        *,
        confirmed_failure: bool = False,
    ) -> bool:
        if not confirmed_failure and not await self._is_logged_out(page):
            return False

        async with self._login_lock:
            if not settings.account or not settings.password or not settings.verification_email:
                self._require_login_settings(settings)
            await self._navigate_for_recheck(page)
            if not await self._probe_is_logged_out(page):
                return False
            await self._perform_login(page, settings)
            return True
