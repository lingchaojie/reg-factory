import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from fastapi.testclient import TestClient

from nexacard_otp.app import create_app
from nexacard_otp.errors import (
    GmailAuthorizationRequired,
    GmailTemporarilyUnavailable,
    InvalidLookupInput,
    NexaCardLoginFailed,
    NexaCardPageError,
    NexaCardTransientError,
    OtpLookupTimedOut,
)


def _settings(name="settings"):
    return SimpleNamespace(page_timezone=SimpleNamespace(name=name))


class NexaCardApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.browser = Mock()
        self.browser.login_lock = object()
        self.browser.close = AsyncMock()
        self.lookup = Mock()
        self.lookup.lookup = AsyncMock(return_value="123456")
        self.stack.enter_context(
            patch("nexacard_otp.app.NativeChromeManager", return_value=self.browser)
        )
        self.stack.enter_context(patch("nexacard_otp.app.GmailCodeReader"))
        self.stack.enter_context(patch("nexacard_otp.app.NexaCardLogin"))
        self.stack.enter_context(
            patch("nexacard_otp.app.OtpLookupService", return_value=self.lookup)
        )
        self.app = create_app()
        self.client_context = TestClient(self.app, raise_server_exceptions=False)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.stack.close()

    def _valid_payload(self):
        return {
            "card_number": "6500000000000037",
            "card_type": "NexaCardB",
            "order_created_at": "2026-07-19T05:30:20+08:00",
        }

    def _post_with_valid_lookup(self, *, settings=None, lookup=None):
        settings = settings or _settings()
        lookup = lookup or Mock()
        with patch("nexacard_otp.app.load_settings", return_value=settings), patch(
            "nexacard_otp.app.parse_lookup_input", return_value=lookup
        ):
            return self.client.post("/v1/otp", json=self._valid_payload())

    def test_success_contains_only_otp_json_and_uses_one_settings_snapshot(self):
        first_settings, second_settings = _settings("first"), _settings("second")
        first_lookup, second_lookup = Mock(), Mock()
        with patch(
            "nexacard_otp.app.load_settings", side_effect=[first_settings, second_settings]
        ) as load_settings, patch(
            "nexacard_otp.app.parse_lookup_input", side_effect=[first_lookup, second_lookup]
        ) as parse_lookup:
            first = self.client.post("/v1/otp", json=self._valid_payload())
            second = self.client.post("/v1/otp", json=self._valid_payload())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {"otp": "123456"})
        self.assertEqual(first.headers["content-type"], "application/json")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(load_settings.call_count, 2)
        parse_lookup.assert_has_calls(
            [
                call("6500000000000037", "NexaCardB", "2026-07-19T05:30:20+08:00", first_settings.page_timezone),
                call("6500000000000037", "NexaCardB", "2026-07-19T05:30:20+08:00", second_settings.page_timezone),
            ]
        )
        self.lookup.lookup.assert_has_awaits(
            [call(first_lookup, first_settings), call(second_lookup, second_settings)]
        )

    def test_domain_failures_have_safe_stable_status_and_do_not_echo_secrets(self):
        secret = "6500000000000037 token=private payment-otp=123456"
        cases = (
            (InvalidLookupInput(secret), 400, "invalid_lookup_input"),
            (NexaCardLoginFailed(secret), 502, "nexacard_login_failed"),
            (NexaCardPageError(secret), 502, "nexacard_page_error"),
            (GmailAuthorizationRequired(secret), 503, "gmail_authorization_required"),
            (GmailTemporarilyUnavailable(secret), 503, "gmail_temporarily_unavailable"),
            (NexaCardTransientError(secret), 503, "nexacard_temporarily_unavailable"),
            (OtpLookupTimedOut(secret), 504, "otp_lookup_timed_out"),
        )
        for failure, status, code in cases:
            with self.subTest(failure=type(failure).__name__):
                self.lookup.lookup = AsyncMock(side_effect=failure)
                response = self._post_with_valid_lookup()

                self.assertEqual(response.status_code, status)
                self.assertEqual(set(response.json()), {"code", "message"})
                self.assertEqual(response.json()["code"], code)
                self.assertNotIn(secret, response.text)
                self.assertNotIn("6500000000000037", response.text)
                self.assertNotIn("private", response.text)

    def test_malformed_and_missing_json_are_safe_and_still_load_settings_once(self):
        for kwargs in (
            {"json": {"card_number": "6500000000000037"}},
            {"content": b"{not-json", "headers": {"content-type": "application/json"}},
        ):
            with self.subTest(kwargs=kwargs), patch(
                "nexacard_otp.app.load_settings", return_value=_settings()
            ) as load_settings, patch("nexacard_otp.app.parse_lookup_input") as parse_lookup:
                response = self.client.post("/v1/otp", **kwargs)

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"code": "invalid_request", "message": "invalid request"})
            load_settings.assert_called_once_with()
            parse_lookup.assert_not_called()
            self.assertNotIn("6500000000000037", response.text)

    def test_unexpected_failure_returns_generic_secret_free_500(self):
        self.lookup.lookup = AsyncMock(
            side_effect=RuntimeError("authorization=private card=6500000000000037")
        )
        response = self._post_with_valid_lookup()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(), {"code": "internal_error", "message": "internal server error"}
        )
        self.assertNotIn("private", response.text)
        self.assertNotIn("6500000000000037", response.text)

    def test_health_is_exact_and_does_not_use_lookup_or_browser_pages(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.lookup.lookup.assert_not_called()
        self.browser.page.assert_not_called()

    def test_lifespan_wires_one_lazy_service_and_closes_browser(self):
        browser = Mock(login_lock=object())
        browser.close = AsyncMock()
        reader = Mock()
        login = Mock()
        service = Mock()
        app = create_app()
        with patch("nexacard_otp.app.NativeChromeManager", return_value=browser) as browser_type, patch(
            "nexacard_otp.app.GmailCodeReader", return_value=reader
        ) as reader_type, patch("nexacard_otp.app.NexaCardLogin", return_value=login) as login_type, patch(
            "nexacard_otp.app.OtpLookupService", return_value=service
        ) as service_type:
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").json(), {"ok": True})
                self.assertIs(app.state.lookup_service, service)

        browser_type.assert_called_once_with()
        reader_type.assert_called_once_with()
        login_type.assert_called_once_with(browser.login_lock, reader)
        service_type.assert_called_once_with(browser, login)
        browser.close.assert_awaited_once_with()
        self.assertFalse(hasattr(app.state, "lookup_service"))
        self.assertFalse(hasattr(app.state, "browser"))

    def test_lifespan_closes_browser_when_startup_wiring_fails(self):
        browser = Mock(login_lock=object())
        browser.close = AsyncMock()
        app = create_app()
        with patch("nexacard_otp.app.NativeChromeManager", return_value=browser), patch(
            "nexacard_otp.app.GmailCodeReader", side_effect=RuntimeError("private token")
        ):
            with self.assertRaisesRegex(RuntimeError, "private token"):
                with TestClient(app):
                    pass

        browser.close.assert_awaited_once_with()
        self.assertFalse(hasattr(app.state, "lookup_service"))

    def test_lifespan_preserves_preinjected_lookup_service_for_requests_and_shutdown(self):
        app = create_app()
        supplied_service = Mock()
        supplied_service.lookup = AsyncMock(return_value="654321")
        app.state.lookup_service = supplied_service
        supplied_browser = Mock(login_lock=object())
        supplied_browser.close = AsyncMock()
        app.state.browser = supplied_browser

        with patch("nexacard_otp.app.NativeChromeManager") as browser_type, patch(
            "nexacard_otp.app.GmailCodeReader"
        ) as reader_type, patch("nexacard_otp.app.NexaCardLogin") as login_type, patch(
            "nexacard_otp.app.OtpLookupService"
        ) as service_type, patch("nexacard_otp.app.load_settings", return_value=_settings()), patch(
            "nexacard_otp.app.parse_lookup_input", return_value=Mock()
        ):
            with TestClient(app) as client:
                response = client.post("/v1/otp", json=self._valid_payload())
                self.assertEqual(response.json(), {"otp": "654321"})
                self.assertIs(app.state.lookup_service, supplied_service)

        self.assertIs(app.state.lookup_service, supplied_service)
        self.assertIs(app.state.browser, supplied_browser)
        supplied_service.lookup.assert_awaited_once()
        browser_type.assert_not_called()
        reader_type.assert_not_called()
        login_type.assert_not_called()
        service_type.assert_not_called()
        supplied_browser.close.assert_not_called()

    def test_nested_lifespans_keep_outer_service_until_outer_shutdown(self):
        app = create_app()
        outer_browser = Mock(login_lock=object())
        outer_browser.close = AsyncMock()
        outer_service = Mock()
        outer_service.lookup = AsyncMock(return_value="123456")

        with patch("nexacard_otp.app.NativeChromeManager", return_value=outer_browser) as browser_type, patch(
            "nexacard_otp.app.GmailCodeReader"
        ), patch("nexacard_otp.app.NexaCardLogin") as login_type, patch(
            "nexacard_otp.app.OtpLookupService", return_value=outer_service
        ) as service_type, patch("nexacard_otp.app.load_settings", return_value=_settings()), patch(
            "nexacard_otp.app.parse_lookup_input", return_value=Mock()
        ):
            with TestClient(app) as outer:
                self.assertEqual(outer.post("/v1/otp", json=self._valid_payload()).status_code, 200)
                with TestClient(app) as inner:
                    self.assertEqual(inner.post("/v1/otp", json=self._valid_payload()).status_code, 200)
                self.assertEqual(outer.post("/v1/otp", json=self._valid_payload()).status_code, 200)
                self.assertIs(app.state.lookup_service, outer_service)
                outer_browser.close.assert_not_called()

        browser_type.assert_called_once_with()
        login_type.assert_called_once()
        service_type.assert_called_once()
        outer_browser.close.assert_awaited_once_with()
        self.assertFalse(hasattr(app.state, "browser"))
        self.assertFalse(hasattr(app.state, "lookup_service"))

    def test_lifespan_uses_external_browser_for_temporary_lookup_without_closing_it(self):
        app = create_app()
        external_browser = Mock(login_lock=object())
        external_browser.close = AsyncMock()
        app.state.browser = external_browser
        created_service = Mock()
        created_service.lookup = AsyncMock(return_value="123456")

        with patch("nexacard_otp.app.NativeChromeManager") as browser_type, patch(
            "nexacard_otp.app.GmailCodeReader"
        ) as reader_type, patch("nexacard_otp.app.NexaCardLogin") as login_type, patch(
            "nexacard_otp.app.OtpLookupService", return_value=created_service
        ) as service_type, patch("nexacard_otp.app.load_settings", return_value=_settings()), patch(
            "nexacard_otp.app.parse_lookup_input", return_value=Mock()
        ):
            with TestClient(app) as client:
                self.assertEqual(client.post("/v1/otp", json=self._valid_payload()).status_code, 200)
                self.assertIs(app.state.browser, external_browser)
                self.assertIs(app.state.lookup_service, created_service)

        browser_type.assert_not_called()
        reader_type.assert_called_once_with()
        login_type.assert_called_once_with(external_browser.login_lock, reader_type.return_value)
        service_type.assert_called_once_with(external_browser, login_type.return_value)
        external_browser.close.assert_not_called()
        self.assertIs(app.state.browser, external_browser)
        self.assertFalse(hasattr(app.state, "lookup_service"))

    def test_lifespan_initialization_failure_restores_external_state(self):
        app = create_app()
        external_browser = Mock(login_lock=object())
        external_browser.close = AsyncMock()
        app.state.browser = external_browser

        with patch("nexacard_otp.app.NativeChromeManager") as browser_type, patch(
            "nexacard_otp.app.GmailCodeReader", side_effect=RuntimeError("private token")
        ):
            with self.assertRaisesRegex(RuntimeError, "private token"):
                with TestClient(app):
                    pass

        browser_type.assert_not_called()
        external_browser.close.assert_not_called()
        self.assertIs(app.state.browser, external_browser)
        self.assertFalse(hasattr(app.state, "lookup_service"))

    def test_concurrent_requests_share_no_api_lock(self):
        both_entered = threading.Event()
        release = threading.Event()
        count_lock = threading.Lock()
        entered = 0

        async def lookup(_lookup, _settings):
            nonlocal entered
            with count_lock:
                entered += 1
                if entered == 2:
                    both_entered.set()
            await asyncio.to_thread(release.wait)
            return "123456"

        self.lookup.lookup = AsyncMock(side_effect=lookup)
        with patch("nexacard_otp.app.load_settings", return_value=_settings()), patch(
            "nexacard_otp.app.parse_lookup_input", return_value=Mock()
        ), ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.client.post, "/v1/otp", json=self._valid_payload())
            second = executor.submit(self.client.post, "/v1/otp", json=self._valid_payload())
            # Both calls must enter lookup before either can complete; a global endpoint lock deadlocks this test.
            self.assertTrue(both_entered.wait(timeout=2))
            release.set()
            responses = [first.result(), second.result()]

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(self.lookup.lookup.await_count, 2)


class NexaCardServiceEntrypointTests(unittest.TestCase):
    def test_main_passes_configured_bind_and_disables_reload(self):
        import nexacard_otp_service

        settings = SimpleNamespace(service_host="127.0.0.1", service_port=8811)
        with patch("nexacard_otp_service.load_settings", return_value=settings) as load_settings, patch(
            "nexacard_otp_service.uvicorn.run"
        ) as run:
            nexacard_otp_service.main()

        load_settings.assert_called_once_with()
        run.assert_called_once_with(
            "nexacard_otp.app:app", host="127.0.0.1", port=8811, reload=False
        )


if __name__ == "__main__":
    unittest.main()
