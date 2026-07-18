"""Read NexaCard verification-code pages and poll for the nearest payment OTP."""

import asyncio
import re
from datetime import datetime
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .browser import NativeChromeManager
from .errors import InvalidLookupInput, NexaCardPageError, NexaCardTransientError, OtpLookupTimedOut
from .login import BASE_URL, NexaCardLogin, USERNAME_INPUT
from .matching import normalize_card_number, route_for, select_nearest_otp
from .models import LookupInput, OtpRow
from .settings import Settings


VERIFY_API_PATH = "/api/verify/code/"
CARD_INPUT = "input[placeholder='请输入卡号']"
SEARCH_BUTTON = "button.act-color"
NEXT_BUTTON = ".el-pagination .btn-next"
MAX_PAGINATION_PAGES = 1_000
_OTP_PATTERN = re.compile(r"\d{6}\Z")


class VerificationPage:
    """A stateless reader; a request-specific Playwright page supplies its state."""

    def __init__(self, max_pages: int = MAX_PAGINATION_PAGES) -> None:
        self._max_pages = max_pages

    async def _is_logged_out(self, page: Page) -> bool:
        fragment = page.url.split("#", 1)[-1].split("?", 1)[0].rstrip("/")
        if fragment == "/login":
            return True
        try:
            return await page.locator(USERNAME_INPUT).count() > 0
        except PlaywrightError as exc:
            raise NexaCardTransientError("NexaCard login state could not be checked") from exc

    @staticmethod
    def _is_verify_response(response: Any) -> bool:
        return VERIFY_API_PATH in str(response.url)

    async def _click_and_wait_for_query(self, page: Page, locator: Any) -> None:
        try:
            async with page.expect_response(self._is_verify_response) as response_info:
                await locator.click()
            response = await response_info.value
        except PlaywrightTimeoutError as exc:
            raise NexaCardTransientError("NexaCard verification request timed out") from exc
        except PlaywrightError as exc:
            raise NexaCardTransientError("NexaCard verification request failed temporarily") from exc

        status = response.status
        if status in {401, 403}:
            raise PermissionError("NexaCard session is logged out")
        if status in {408, 425, 429} or 500 <= status < 600:
            raise NexaCardTransientError("NexaCard verification request failed temporarily")
        if not 200 <= status < 300:
            raise NexaCardPageError("NexaCard verification request returned an unusable response")

    async def _current_rows(self, page: Page, settings: Settings) -> list[OtpRow]:
        output: list[OtpRow] = []
        try:
            table_rows = await page.locator("table tbody tr").all()
        except PlaywrightError as exc:
            raise NexaCardTransientError("NexaCard verification rows could not be read") from exc

        for table_row in table_rows:
            try:
                cells = await table_row.locator("td").all_inner_texts()
                if len(cells) != 8:
                    raise ValueError("unexpected cell count")
                record_id = int(cells[0].strip())
                otp = cells[2].strip()
                card_number = normalize_card_number(cells[3].strip())
                created_at = datetime.strptime(cells[6].strip(), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=settings.page_timezone
                )
                if record_id < 0 or not _OTP_PATTERN.fullmatch(otp):
                    raise ValueError("invalid verification row")
            except (IndexError, TypeError, ValueError, InvalidLookupInput) as exc:
                raise NexaCardPageError("NexaCard verification table has an unexpected row") from exc
            output.append(OtpRow(record_id, otp, card_number, created_at))
        return output

    @staticmethod
    def _page_signature(rows: list[OtpRow]) -> tuple[tuple[int, str, str, datetime], ...]:
        return tuple((row.record_id, row.otp, row.card_number, row.created_at) for row in rows)

    async def search_rows(self, page: Page, lookup: LookupInput, settings: Settings) -> list[OtpRow]:
        try:
            await page.goto(BASE_URL + route_for(lookup.card_type), wait_until="networkidle")
        except PlaywrightTimeoutError as exc:
            raise NexaCardTransientError("NexaCard verification page timed out") from exc
        except PlaywrightError as exc:
            raise NexaCardTransientError("NexaCard verification page is temporarily unavailable") from exc
        if await self._is_logged_out(page):
            raise PermissionError("NexaCard session is logged out")

        try:
            card_input = page.locator(CARD_INPUT)
            await card_input.fill(lookup.card_number)
        except PlaywrightTimeoutError as exc:
            raise NexaCardTransientError("NexaCard card search timed out") from exc
        except PlaywrightError as exc:
            raise NexaCardPageError("NexaCard card search form is unusable") from exc
        await self._click_and_wait_for_query(page, page.locator(SEARCH_BUTTON))

        rows: list[OtpRow] = []
        previous_signature: tuple[tuple[int, str, str, datetime], ...] | None = None
        page_count = 0
        while True:
            page_count += 1
            if page_count > self._max_pages:
                raise NexaCardPageError("NexaCard pagination exceeded the safety bound")
            current_rows = await self._current_rows(page, settings)
            signature = self._page_signature(current_rows)
            if previous_signature is not None and signature == previous_signature:
                raise NexaCardPageError("NexaCard pagination did not make progress")
            rows.extend(current_rows)
            previous_signature = signature

            try:
                next_button = page.locator(NEXT_BUTTON)
                if await next_button.count() == 0 or await next_button.is_disabled():
                    return rows
            except PlaywrightTimeoutError as exc:
                raise NexaCardTransientError("NexaCard pagination timed out") from exc
            except PlaywrightError as exc:
                raise NexaCardPageError("NexaCard pagination control is unusable") from exc

            await self._click_and_wait_for_query(page, next_button)
            if await self._is_logged_out(page):
                raise PermissionError("NexaCard session expired during pagination")


class OtpLookupService:
    def __init__(
        self,
        browser: NativeChromeManager,
        login: NexaCardLogin,
        verification_page: VerificationPage | None = None,
    ) -> None:
        self._browser = browser
        self._login = login
        self._verification_page = verification_page or VerificationPage()

    async def lookup(self, lookup: LookupInput, settings: Settings) -> str:
        """Poll one stable settings snapshot, with bounded recovery-only retries."""
        recovered = False
        transient_failures = 0
        async with self._browser.page(settings) as page:
            attempt = 0
            while attempt < settings.max_attempts:
                try:
                    rows = await self._verification_page.search_rows(page, lookup, settings)
                except PermissionError as exc:
                    if recovered:
                        raise NexaCardPageError(
                            "NexaCard session expired again after login recovery"
                        ) from exc
                    await self._login.ensure_authenticated(page, settings)
                    recovered = True
                    continue
                except NexaCardTransientError:
                    transient_failures += 1
                    if transient_failures > 2:
                        raise
                    await asyncio.sleep(min(settings.poll_interval_seconds, 1.0))
                    continue

                transient_failures = 0
                attempt += 1
                match = select_nearest_otp(rows, lookup)
                if match is not None:
                    return match.otp
                if attempt < settings.max_attempts:
                    await asyncio.sleep(settings.poll_interval_seconds)
        raise OtpLookupTimedOut("no matching OTP appeared before the configured attempt limit")
