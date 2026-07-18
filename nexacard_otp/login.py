import asyncio
from datetime import datetime, timezone
from urllib.parse import urlsplit

from google.auth.exceptions import RefreshError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from .errors import (
    GmailAuthorizationRequired,
    GmailTemporarilyUnavailable,
    NexaCardLoginFailed,
)
from .gmail_reader import GmailCodeReader
from .settings import Settings


BASE_URL = "https://www.nexacardvcc.com"
LOGIN_URL = f"{BASE_URL}/#/login"
PROTECTED_PROBE_URL = f"{BASE_URL}/#/nova-v-card-b/verify-code"
PAGE_TIMEOUT_MS = 30_000
USERNAME_INPUT = 'input[placeholder="请输入用户名"]'
PASSWORD_INPUT = 'input[placeholder="请输入密码"]'
VERIFICATION_EMAIL_INPUT = 'input[placeholder="请输入邮箱"]'
VERIFICATION_CODE_INPUT = 'input[placeholder="请输入邮箱验证码"]'
EMAIL_LOGIN_RADIO = ".el-radio"
REQUEST_CODE_BUTTON = "button.get-code-btn"
SUBMIT_BUTTON = "button.submit-btn"


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

    @classmethod
    def _is_authenticated_url(cls, url: str) -> bool:
        route = cls._route(url)
        return route.startswith("/nova-v-card-b") or route.startswith("/3d-1-card")

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

    async def _wait_for_login_code(self, sent_after: datetime) -> str:
        try:
            return await self._gmail_reader.wait_for_login_code(sent_after)
        except TimeoutError as exc:
            raise NexaCardLoginFailed("NexaCard login verification email timed out") from exc
        except (GmailAuthorizationRequired, RefreshError) as exc:
            raise NexaCardLoginFailed("NexaCard Gmail authorization is required") from exc
        except GmailTemporarilyUnavailable as exc:
            raise NexaCardLoginFailed("NexaCard Gmail is temporarily unavailable") from exc
        except Exception as exc:
            raise NexaCardLoginFailed("NexaCard login verification email could not be read") from exc

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
            sent_after = datetime.now(timezone.utc)
            await page.locator(REQUEST_CODE_BUTTON).click(timeout=PAGE_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise NexaCardLoginFailed("NexaCard page operation failed") from exc

        code = await self._wait_for_login_code(sent_after)

        try:
            await page.locator(VERIFICATION_CODE_INPUT).fill(code, timeout=PAGE_TIMEOUT_MS)
            await page.locator(SUBMIT_BUTTON).click(timeout=PAGE_TIMEOUT_MS)
            await page.wait_for_url(
                lambda url: self._is_authenticated_url(str(url)), timeout=PAGE_TIMEOUT_MS
            )
        except PlaywrightError as exc:
            raise NexaCardLoginFailed("NexaCard login did not reach an authenticated page") from exc

    async def ensure_authenticated(self, page: Page, settings: Settings) -> bool:
        if not await self._is_logged_out(page):
            return False

        async with self._login_lock:
            if not settings.account or not settings.password or not settings.verification_email:
                self._require_login_settings(settings)
            await self._navigate_for_recheck(page)
            if not await self._is_logged_out(page):
                return False
            await self._perform_login(page, settings)
            return True
