import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from nexacard_otp.errors import NexaCardPageError, NexaCardTransientError, OtpLookupTimedOut
from nexacard_otp.lookup import (
    OtpLookupService,
    QUERY_MARKER_ADVANCED_SCRIPT,
    QUERY_MARKER_INSTALL_SCRIPT,
    VerificationPage,
)
from nexacard_otp.models import CardType, LookupInput, OtpRow


class _RowsLocator:
    def __init__(self, rows):
        self._rows = rows

    async def all(self):
        return self._rows


class _CellRow:
    def __init__(self, cells):
        self._cells = cells

    def locator(self, selector):
        self.selector = selector
        return self

    async def all_inner_texts(self):
        return self._cells


class _CountLocator:
    def __init__(self, count=0, disabled=False):
        self._count = count
        self._disabled = disabled

    async def count(self):
        return self._count

    async def is_disabled(self):
        return self._disabled


class _ResponseContext:
    def __init__(self, response):
        self.value = self._response_value(response)

    @staticmethod
    async def _response_value(response):
        return response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _TablePage:
    def __init__(self, rows):
        self.rows = rows

    def locator(self, selector):
        if selector == "table tbody tr":
            return _RowsLocator(self.rows)
        raise AssertionError(selector)


class _RoutePage:
    def __init__(self, url="https://www.nexacardvcc.com/#/nova-v-card-b/verify-code"):
        self.url = url
        self.goto = AsyncMock()
        self.card_input = Mock(fill=AsyncMock())
        self.search_button = Mock(click=AsyncMock())
        self.next_button = _CountLocator()
        self.response = Mock(
            status=200,
            url="https://admin.jushipay.com/api/verify/code/list",
            finished=AsyncMock(return_value=None),
        )
        self.evaluate = AsyncMock(return_value=5)
        self.wait_for_function = AsyncMock()

    def locator(self, selector):
        if selector == "input[placeholder='请输入卡号']":
            return self.card_input
        if selector == "button.act-color":
            return self.search_button
        if selector == ".el-pagination .btn-next":
            return self.next_button
        if selector == ".el-pagination .number.active":
            return _CountLocator()
        if selector == 'input[placeholder="请输入用户名"]':
            return _CountLocator()
        raise AssertionError(selector)

    def expect_response(self, predicate):
        self.response_predicate = predicate
        return _ResponseContext(self.response)


class _SettlePage:
    def __init__(self):
        self.loading = _CountLocator()
        self.active = _CountLocator()
        self.table = _CountLocator()
        self.url = "https://www.nexacardvcc.com/#/nova-v-card-b/verify-code"
        self.wait_for_function = AsyncMock()

    def locator(self, selector):
        if selector == ".el-loading-mask":
            return self.loading
        if selector == ".el-pagination .number.active":
            return self.active
        if selector == "table tbody":
            return self.table
        if selector == 'input[placeholder="请输入用户名"]':
            return _CountLocator()
        raise AssertionError(selector)


class _Manager:
    def __init__(self):
        self.pages = []

    @asynccontextmanager
    async def page(self, settings):
        page = AsyncMock(name=f"page-{len(self.pages)}")
        self.pages.append(page)
        try:
            yield page
        finally:
            await page.close()


class VerificationPageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zone = ZoneInfo("Asia/Shanghai")
        self.settings = Mock(page_timezone=self.zone)
        self.lookup_b = LookupInput("6500000000000037", CardType.NEXACARD_B, datetime(2026, 7, 19, 3, 0, tzinfo=self.zone))
        self.lookup_3d = LookupInput("6500000000000037", CardType.THREE_D_1, datetime(2026, 7, 19, 3, 0, tzinfo=self.zone))

    async def test_current_rows_uses_exact_structural_indexes_and_shanghai_timezone(self):
        page = _TablePage([_CellRow(["9", "unused", "123456", "6500 0000-0000 0037", "unused", "unused", "2026-07-19 03:00:01", "unused"])])
        rows = await VerificationPage()._current_rows(page, self.settings)
        self.assertEqual(rows, [OtpRow(9, "123456", "6500000000000037", datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone))])

    async def test_current_rows_rejects_malformed_id_otp_card_or_created_value_without_secrets(self):
        invalid_rows = [
            ["bad", "", "123456", "6500000000000037", "", "", "2026-07-19 03:00:01", ""],
            ["9", "", "12345", "6500000000000037", "", "", "2026-07-19 03:00:01", ""],
            ["9", "", "123456", "not-a-card", "", "", "2026-07-19 03:00:01", ""],
            ["9", "", "123456", "6500000000000037", "", "", "not-a-time", ""],
        ]
        for cells in invalid_rows:
            with self.subTest(cells=cells):
                with self.assertRaises(NexaCardPageError) as caught:
                    await VerificationPage()._current_rows(_TablePage([_CellRow(cells)]), self.settings)
                self.assertNotIn("6500000000000037", str(caught.exception))
                self.assertNotIn("123456", str(caught.exception))

    async def test_current_rows_rejects_short_rows_instead_of_silently_ignoring_them(self):
        with self.assertRaises(NexaCardPageError):
            await VerificationPage()._current_rows(_TablePage([_CellRow(["1"]) ]), self.settings)

    async def test_search_uses_each_confirmed_route(self):
        reader = VerificationPage()
        for lookup, route in ((self.lookup_b, "/nova-v-card-b/verify-code"), (self.lookup_3d, "/3d-1-card/verify-code")):
            with self.subTest(route=route):
                page = _RoutePage()
                reader._settle_rows = AsyncMock(return_value=[])
                await reader.search_rows(page, lookup, self.settings)
                self.assertTrue(page.goto.await_args.args[0].endswith(route))
                page.card_input.fill.assert_awaited_once_with(lookup.card_number)
                page.search_button.click.assert_awaited_once()
                page.response.finished.assert_awaited_once()

    async def test_query_marker_is_installed_after_fill_and_before_the_search_click(self):
        page = _RoutePage()
        trace = []

        async def evaluate(_script):
            trace.append("marker")
            return 11

        async def click():
            trace.append("click")

        async def fill(_value):
            trace.append("fill")

        page.evaluate.side_effect = evaluate
        page.search_button.click.side_effect = click
        page.card_input.fill.side_effect = fill
        reader = VerificationPage()
        reader._settle_rows = AsyncMock(return_value=[])
        await reader.search_rows(page, self.lookup_b, self.settings)
        self.assertEqual(trace, ["fill", "marker", "click"])
        self.assertEqual(reader._settle_rows.await_args.kwargs["query_marker"], 11)

    async def test_query_observer_is_limited_to_result_and_loading_roots(self):
        self.assertNotIn("document.documentElement", QUERY_MARKER_INSTALL_SCRIPT)
        for selector in (".el-table", "table tbody", ".el-table__empty-block", ".el-pagination", ".el-loading-mask"):
            self.assertIn(selector, QUERY_MARKER_INSTALL_SCRIPT)
        self.assertIn("disconnect()", QUERY_MARKER_INSTALL_SCRIPT)
        self.assertIn("resultObserver", QUERY_MARKER_INSTALL_SCRIPT)
        self.assertIn("loadingWasVisible", QUERY_MARKER_INSTALL_SCRIPT)
        self.assertIn("loadingWasVisible", QUERY_MARKER_ADVANCED_SCRIPT)
        self.assertIn("new MutationObserver(() => { marker.generation += 1; })", QUERY_MARKER_INSTALL_SCRIPT)
        self.assertIn("if (marker.loadingWasVisible && !visible) marker.generation += 1;", QUERY_MARKER_INSTALL_SCRIPT)
        self.assertIn("resultRoots.forEach", QUERY_MARKER_INSTALL_SCRIPT)
        self.assertNotIn("document.body", QUERY_MARKER_INSTALL_SCRIPT)

    async def test_query_marker_roots_missing_becomes_a_safe_page_error(self):
        page = _RoutePage()
        page.evaluate.return_value = None
        with self.assertRaises(NexaCardPageError):
            await VerificationPage()._query_marker(page)

    async def test_hash_login_route_signals_logout(self):
        page = _RoutePage("https://www.nexacardvcc.com/#/login")
        with self.assertRaises(PermissionError):
            await VerificationPage().search_rows(page, self.lookup_b, self.settings)

    async def test_search_rechecks_hash_logout_after_successful_query_before_reading_rows(self):
        page = _RoutePage()
        page.search_button.click.side_effect = lambda: setattr(page, "url", "https://www.nexacardvcc.com/#/login")
        reader = VerificationPage()
        reader._current_rows = AsyncMock(return_value=[])
        with self.assertRaises(PermissionError):
            await reader.search_rows(page, self.lookup_b, self.settings)
        reader._current_rows.assert_not_awaited()
        page.response.finished.assert_awaited_once()

    async def test_verify_response_requires_the_confirmed_https_admin_origin_and_prefix(self):
        self.assertTrue(VerificationPage._is_verify_response(Mock(url="https://admin.jushipay.com/api/verify/code/list")))
        self.assertFalse(VerificationPage._is_verify_response(Mock(url="https://evil.invalid/api/verify/code/list")))
        self.assertFalse(VerificationPage._is_verify_response(Mock(url="http://admin.jushipay.com/api/verify/code/list")))
        self.assertFalse(VerificationPage._is_verify_response(Mock(url="https://admin.jushipay.com/api/other")))

    async def test_response_body_that_never_finishes_becomes_a_bounded_transient_error(self):
        page = _RoutePage()
        page.response.finished = AsyncMock(side_effect=asyncio.Event().wait)
        with patch("nexacard_otp.lookup.RESPONSE_FINISHED_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(NexaCardTransientError):
                await asyncio.wait_for(
                    VerificationPage()._click_and_wait_for_query(page, page.search_button),
                    timeout=0.2,
                )

    async def test_empty_table_placeholder_is_an_empty_result(self):
        page = _TablePage([_CellRow(["暂无数据"])])
        self.assertEqual(await VerificationPage()._current_rows(page, self.settings), [])

    async def test_initial_settle_waits_for_a_short_stable_table_after_the_response(self):
        stale = [OtpRow(1, "123456", self.lookup_b.card_number, datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone))]
        fresh = [OtpRow(2, "234567", self.lookup_b.card_number, datetime(2026, 7, 19, 3, 0, 2, tzinfo=self.zone))]
        reader = VerificationPage()
        reader._current_rows = AsyncMock(side_effect=[stale, fresh, fresh])
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()) as sleep:
            rows = await reader._settle_rows(_SettlePage(), self.settings)
        self.assertEqual(rows, fresh)
        self.assertEqual(reader._current_rows.await_count, 3)
        self.assertGreaterEqual(sleep.await_count, 1)

    async def test_pagination_settle_requires_a_changed_then_stable_table(self):
        old = [OtpRow(1, "123456", self.lookup_b.card_number, datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone))]
        fresh = [OtpRow(2, "234567", self.lookup_b.card_number, datetime(2026, 7, 19, 3, 0, 2, tzinfo=self.zone))]
        reader = VerificationPage()
        reader._current_rows = AsyncMock(side_effect=[old, old, fresh, fresh])
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()):
            rows = await reader._settle_rows(_SettlePage(), self.settings, previous_signature=reader._page_signature(old))
        self.assertEqual(rows, fresh)
        self.assertEqual(reader._current_rows.await_count, 4)

    async def test_pagination_dom_that_never_changes_fails_with_a_bounded_page_error(self):
        old = [OtpRow(1, "123456", self.lookup_b.card_number, datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone))]
        reader = VerificationPage()
        reader._current_rows = AsyncMock(return_value=old)
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(NexaCardPageError):
                await reader._settle_rows(_SettlePage(), self.settings, previous_signature=reader._page_signature(old))
        self.assertLessEqual(reader._current_rows.await_count, 4)

    async def test_query_marker_allows_same_rows_after_this_query_mutates_the_page(self):
        same = [OtpRow(1, "123456", self.lookup_b.card_number, datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone))]
        reader = VerificationPage()
        reader._current_rows = AsyncMock(return_value=same)
        page = _SettlePage()
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()):
            rows = await reader._settle_rows(page, self.settings, query_marker=7)
        self.assertEqual(rows, same)
        page.wait_for_function.assert_awaited_once()

    async def test_query_marker_allows_an_empty_result_after_this_query_mutates_the_page(self):
        reader = VerificationPage()
        reader._current_rows = AsyncMock(return_value=[])
        page = _SettlePage()
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()):
            rows = await reader._settle_rows(page, self.settings, query_marker=7)
        self.assertEqual(rows, [])
        page.wait_for_function.assert_awaited_once()

    async def test_missing_query_mutation_becomes_a_bounded_transient_error(self):
        page = _SettlePage()
        page.wait_for_function.side_effect = PlaywrightTimeoutError("timed out")
        with self.assertRaises(NexaCardTransientError):
            await VerificationPage()._settle_rows(page, self.settings, query_marker=7)

    async def test_settle_detects_delayed_hash_logout_before_parsing_rows(self):
        page = _SettlePage()

        async def mutation(*_args, **_kwargs):
            page.url = "https://www.nexacardvcc.com/#/login"

        page.wait_for_function.side_effect = mutation
        reader = VerificationPage()
        reader._current_rows = AsyncMock(return_value=[])
        with self.assertRaises(PermissionError):
            await reader._settle_rows(page, self.settings, query_marker=7)
        reader._current_rows.assert_not_awaited()

    async def test_missing_or_disabled_next_ends_pagination(self):
        for next_button in (_CountLocator(), _CountLocator(1, disabled=True)):
            with self.subTest(next_button=next_button):
                page = _RoutePage()
                page.next_button = next_button
                reader = VerificationPage()
                reader._click_and_wait_for_query = AsyncMock()
                reader._settle_rows = AsyncMock(return_value=[])
                self.assertEqual(await reader.search_rows(page, self.lookup_b, self.settings), [])
                self.assertEqual(reader._settle_rows.await_count, 1)

    async def test_pagination_reads_all_pages_and_requires_progress(self):
        page = _RoutePage()
        page.next_button = Mock(count=AsyncMock(return_value=1), is_disabled=AsyncMock(return_value=False), click=AsyncMock())
        reader = VerificationPage(max_pages=2)
        page_one = [OtpRow(1, "123456", self.lookup_b.card_number, datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone))]
        page_two = [OtpRow(2, "234567", self.lookup_b.card_number, datetime(2026, 7, 19, 3, 0, 2, tzinfo=self.zone))]

        async def rows_with_progress(_page, _settings, **_kwargs):
            if reader._settle_rows.await_count == 2:
                page.next_button.is_disabled.return_value = True
                return page_two
            return page_one

        reader._settle_rows = AsyncMock(side_effect=rows_with_progress)
        await reader.search_rows(page, self.lookup_b, self.settings)
        self.assertEqual(reader._settle_rows.await_count, 2)
        page.next_button.click.assert_awaited_once()
        self.assertEqual(page.response.finished.await_count, 2)

    async def test_pagination_safety_bound_prevents_unbounded_loop(self):
        page = _RoutePage()
        page.next_button = _CountLocator(1, disabled=False)
        reader = VerificationPage(max_pages=1)
        counter = 0

        async def distinct_rows(_page, _settings, **_kwargs):
            nonlocal counter
            counter += 1
            return [OtpRow(counter, "123456", self.lookup_b.card_number, datetime(2026, 7, 19, 3, 0, counter, tzinfo=self.zone))]

        reader._settle_rows = AsyncMock(side_effect=distinct_rows)
        reader._click_and_wait_for_query = AsyncMock()
        with self.assertRaises(NexaCardPageError):
            await reader.search_rows(page, self.lookup_b, self.settings)

    async def test_pagination_rejects_a_non_adjacent_repeated_page_signature(self):
        page = _RoutePage()
        page.next_button = _CountLocator(1, disabled=False)
        first = [OtpRow(1, "123456", self.lookup_b.card_number, datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone))]
        second = [OtpRow(2, "234567", self.lookup_b.card_number, datetime(2026, 7, 19, 3, 0, 2, tzinfo=self.zone))]
        reader = VerificationPage()
        reader._settle_rows = AsyncMock(side_effect=[first, second, first])
        reader._click_and_wait_for_query = AsyncMock()
        with self.assertRaises(NexaCardPageError):
            await reader.search_rows(page, self.lookup_b, self.settings)
        self.assertEqual(reader._settle_rows.await_count, 3)

    async def test_query_status_maps_auth_to_logout_transient_and_unusable_to_page_error(self):
        reader = VerificationPage()
        locator = Mock()
        for status, expected in ((401, PermissionError), (403, PermissionError), (500, NexaCardTransientError), (400, NexaCardPageError)):
            with self.subTest(status=status):
                page = Mock()
                response = Mock(status=status, url="https://admin.jushipay.com/api/verify/code/list")
                response.finished = AsyncMock(return_value=None)
                context = _ResponseContext(response)
                page.expect_response.return_value = context
                locator.click = AsyncMock()
                if expected is NexaCardPageError:
                    with self.assertRaises(expected):
                        await reader._click_and_wait_for_query(page, locator)
                else:
                    with self.assertRaises(expected):
                        await reader._click_and_wait_for_query(page, locator)


class OtpLookupServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zone = ZoneInfo("Asia/Shanghai")
        self.lookup = LookupInput("6500000000000037", CardType.NEXACARD_B, datetime(2026, 7, 19, 3, 0, tzinfo=self.zone))
        self.settings = Mock(max_attempts=3, poll_interval_seconds=0.25, page_timezone=self.zone)

    async def test_first_attempt_returns_only_six_digit_otp_without_sleep(self):
        row = OtpRow(1, "123456", self.lookup.card_number, datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone))
        manager = _Manager()
        reader = AsyncMock(search_rows=AsyncMock(return_value=[row]))
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()) as sleep:
            otp = await OtpLookupService(manager, AsyncMock(), reader).lookup(self.lookup, self.settings)
        self.assertEqual(otp, "123456")
        sleep.assert_not_awaited()
        manager.pages[0].close.assert_awaited_once()

    async def test_exactly_max_attempts_and_between_attempt_sleeps(self):
        manager = _Manager()
        reader = AsyncMock(search_rows=AsyncMock(return_value=[]))
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(OtpLookupTimedOut):
                await OtpLookupService(manager, AsyncMock(), reader).lookup(self.lookup, self.settings)
        self.assertEqual(reader.search_rows.await_count, 3)
        self.assertEqual(sleep.await_args_list, [((0.25,),), ((0.25,),)])

    async def test_one_logout_recovery_repeats_current_attempt_and_second_logout_fails(self):
        manager = _Manager()
        reader = AsyncMock(search_rows=AsyncMock(side_effect=[PermissionError(), [], [], []]))
        login = AsyncMock()
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(OtpLookupTimedOut):
                await OtpLookupService(manager, login, reader).lookup(self.lookup, self.settings)
        login.ensure_authenticated.assert_awaited_once()
        self.assertEqual(reader.search_rows.await_count, 4)

        reader.search_rows = AsyncMock(side_effect=[PermissionError(), PermissionError()])
        with self.assertRaises(NexaCardPageError) as caught:
            await OtpLookupService(_Manager(), AsyncMock(), reader).lookup(self.lookup, self.settings)
        self.assertNotIn(self.lookup.card_number, str(caught.exception))

    async def test_auth_check_false_does_not_spend_real_recovery_budget(self):
        reader = AsyncMock(search_rows=AsyncMock(side_effect=[PermissionError(), PermissionError(), []]))
        login = AsyncMock(ensure_authenticated=AsyncMock(side_effect=[False, True]))
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(OtpLookupTimedOut):
                await OtpLookupService(_Manager(), login, reader).lookup(self.lookup, Mock(max_attempts=1, poll_interval_seconds=0.25))
        self.assertEqual(login.ensure_authenticated.await_count, 2)
        self.assertEqual(reader.search_rows.await_count, 3)

    async def test_repeated_false_auth_checks_are_bounded(self):
        reader = AsyncMock(search_rows=AsyncMock(side_effect=PermissionError()))
        login = AsyncMock(ensure_authenticated=AsyncMock(return_value=False))
        with self.assertRaises(NexaCardPageError):
            await OtpLookupService(_Manager(), login, reader).lookup(self.lookup, Mock(max_attempts=1, poll_interval_seconds=0.25))
        self.assertLessEqual(login.ensure_authenticated.await_count, 3)

    async def test_two_transient_retries_do_not_consume_and_third_fails(self):
        manager = _Manager()
        reader = AsyncMock(search_rows=AsyncMock(side_effect=[NexaCardTransientError("temporary"), NexaCardTransientError("temporary"), [], [], []]))
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(OtpLookupTimedOut):
                await OtpLookupService(manager, AsyncMock(), reader).lookup(self.lookup, self.settings)
        self.assertEqual(reader.search_rows.await_count, 5)
        self.assertEqual(sleep.await_args_list[:2], [((0.25,),), ((0.25,),)])

        reader.search_rows = AsyncMock(side_effect=[NexaCardTransientError("one"), NexaCardTransientError("two"), NexaCardTransientError("three")])
        with self.assertRaises(NexaCardTransientError):
            await OtpLookupService(_Manager(), AsyncMock(), reader).lookup(self.lookup, self.settings)

    async def test_successful_page_read_resets_transient_budget(self):
        row = OtpRow(1, "123456", self.lookup.card_number, datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone))
        reader = AsyncMock(search_rows=AsyncMock(side_effect=[NexaCardTransientError("one"), NexaCardTransientError("two"), [], NexaCardTransientError("one"), NexaCardTransientError("two"), [row]]))
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()):
            otp = await OtpLookupService(_Manager(), AsyncMock(), reader).lookup(self.lookup, self.settings)
        self.assertEqual(otp, "123456")
        self.assertEqual(reader.search_rows.await_count, 6)

    async def test_concurrent_lookups_receive_isolated_pages(self):
        manager = _Manager()
        row = OtpRow(1, "123456", self.lookup.card_number, datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone))
        reader = AsyncMock(search_rows=AsyncMock(return_value=[row]))
        service = OtpLookupService(manager, AsyncMock(), reader)
        first, second = await asyncio.gather(service.lookup(self.lookup, self.settings), service.lookup(self.lookup, self.settings))
        self.assertEqual((first, second), ("123456", "123456"))
        self.assertEqual(len(manager.pages), 2)
        self.assertIsNot(manager.pages[0], manager.pages[1])
