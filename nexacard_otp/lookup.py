"""Read NexaCard verification-code pages and poll for the nearest payment OTP."""

import asyncio
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

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
ACTIVE_PAGE = ".el-pagination .number.active"
LOADING_MASK = ".el-loading-mask"
TABLE_BODY = "table tbody"
MAX_PAGINATION_PAGES = 1_000
MAX_AUTH_CHECKS = 2
DOM_SETTLE_CHECKS = 4
DOM_SETTLE_INTERVAL_SECONDS = 0.05
DOM_SETTLE_TIMEOUT_MS = 1_000
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
        parsed = urlsplit(str(response.url))
        return (
            parsed.scheme == "https"
            and parsed.netloc == "admin.jushipay.com"
            and parsed.path.startswith(VERIFY_API_PATH)
        )

    async def _click_and_wait_for_query(self, page: Page, locator: Any) -> None:
        try:
            async with page.expect_response(self._is_verify_response) as response_info:
                await locator.click()
            response = await response_info.value
            failure = await response.finished()
        except PlaywrightTimeoutError as exc:
            raise NexaCardTransientError("NexaCard verification request timed out") from exc
        except PlaywrightError as exc:
            raise NexaCardTransientError("NexaCard verification request failed temporarily") from exc
        if failure:
            raise NexaCardTransientError("NexaCard verification request failed temporarily")

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
                if await self._is_empty_placeholder(table_row, cells):
                    continue
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
    async def _is_empty_placeholder(table_row: Any, cells: list[str]) -> bool:
        marker = " ".join(cells).strip().casefold()
        if len(cells) == 1 and marker in {"", "暂无数据", "no data", "no data available"}:
            return True
        try:
            row_class = (await table_row.get_attribute("class") or "").casefold()
            colspan = await table_row.locator("td").get_attribute("colspan")
        except (AttributeError, PlaywrightError):
            return False
        return bool(colspan) and ("empty" in row_class or marker in {"", "暂无数据", "no data", "no data available"})

    @staticmethod
    def _page_signature(rows: list[OtpRow]) -> tuple[tuple[int, str, str, datetime], ...]:
        return tuple((row.record_id, row.otp, row.card_number, row.created_at) for row in rows)

    async def _wait_for_table_attached(self, page: Page) -> None:
        try:
            await page.locator(TABLE_BODY).wait_for(
                state="attached", timeout=DOM_SETTLE_TIMEOUT_MS
            )
        except AttributeError:
            # Test doubles may expose rows directly; real Playwright locators always wait.
            return
        except PlaywrightTimeoutError as exc:
            raise NexaCardTransientError("NexaCard verification table did not appear") from exc
        except PlaywrightError as exc:
            raise NexaCardPageError("NexaCard verification table is unusable") from exc

    async def _loading_is_visible(self, page: Page) -> bool:
        try:
            mask = page.locator(LOADING_MASK)
            return await mask.count() > 0 and await mask.is_visible()
        except AttributeError:
            return False
        except PlaywrightError as exc:
            raise NexaCardTransientError("NexaCard verification result is still loading") from exc

    async def _active_page(self, page: Page) -> str:
        try:
            active = page.locator(ACTIVE_PAGE)
            if await active.count() == 0:
                return ""
            return (await active.inner_text()).strip()
        except AttributeError:
            return ""
        except PlaywrightError as exc:
            raise NexaCardPageError("NexaCard pagination state is unusable") from exc

    async def _settle_rows(
        self,
        page: Page,
        settings: Settings,
        previous_signature: tuple[tuple[int, str, str, datetime], ...] | None = None,
        previous_active_page: str | None = None,
    ) -> list[OtpRow]:
        """Wait briefly for Vue to finish rendering the response without accepting stale pages."""
        await self._wait_for_table_attached(page)
        changed = previous_signature is None
        stable_signature: tuple[tuple[int, str, str, datetime], ...] | None = None
        stable_count = 0
        for check in range(DOM_SETTLE_CHECKS):
            if await self._loading_is_visible(page):
                await asyncio.sleep(DOM_SETTLE_INTERVAL_SECONDS)
                continue
            rows = await self._current_rows(page, settings)
            signature = self._page_signature(rows)
            active_page = await self._active_page(page)
            if not changed:
                changed = (
                    signature != previous_signature
                    or (previous_active_page is not None and active_page != previous_active_page)
                )
            if changed:
                if signature == stable_signature:
                    stable_count += 1
                else:
                    stable_signature = signature
                    stable_count = 1
                if stable_count >= 2:
                    return rows
            if check < DOM_SETTLE_CHECKS - 1:
                await asyncio.sleep(DOM_SETTLE_INTERVAL_SECONDS)
        if previous_signature is None:
            raise NexaCardTransientError("NexaCard verification results did not settle")
        raise NexaCardPageError("NexaCard pagination did not advance")

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
        if await self._is_logged_out(page):
            raise PermissionError("NexaCard session is logged out")

        current_rows = await self._settle_rows(page, settings)
        rows: list[OtpRow] = []
        seen_signatures: set[tuple[tuple[int, str, str, datetime], ...]] = set()
        page_count = 0
        while True:
            page_count += 1
            if page_count > self._max_pages:
                raise NexaCardPageError("NexaCard pagination exceeded the safety bound")
            signature = self._page_signature(current_rows)
            if signature in seen_signatures:
                raise NexaCardPageError("NexaCard pagination did not make progress")
            rows.extend(current_rows)
            seen_signatures.add(signature)

            try:
                next_button = page.locator(NEXT_BUTTON)
                if await next_button.count() == 0 or await next_button.is_disabled():
                    return rows
                active_page = await self._active_page(page)
            except PlaywrightTimeoutError as exc:
                raise NexaCardTransientError("NexaCard pagination timed out") from exc
            except PlaywrightError as exc:
                raise NexaCardPageError("NexaCard pagination control is unusable") from exc

            await self._click_and_wait_for_query(page, next_button)
            if await self._is_logged_out(page):
                raise PermissionError("NexaCard session expired during pagination")
            current_rows = await self._settle_rows(
                page,
                settings,
                previous_signature=signature,
                previous_active_page=active_page,
            )


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
        auth_checks = 0
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
                    auth_checks += 1
                    if auth_checks > MAX_AUTH_CHECKS:
                        raise NexaCardPageError(
                            "NexaCard session could not be recovered"
                        ) from exc
                    did_recover = await self._login.ensure_authenticated(page, settings)
                    if did_recover:
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
