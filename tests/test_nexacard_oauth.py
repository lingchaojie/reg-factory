import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from nexacard_otp.gmail_auth import OAuthCoordinator
from nexacard_otp.models import AuthStatus
from webui import server


CALLBACK = "http://testserver/api/nexacard/oauth/callback"


def _flow(authorization_url="https://accounts.google.com/authorize", returned_state=None):
    flow = Mock()
    flow.authorization_url.side_effect = lambda **kwargs: (authorization_url, returned_state or kwargs["state"])
    flow.credentials.to_json.return_value = '{"access_token":"private","refresh_token":"secret"}'
    return flow


class NexaCardOAuthCoordinatorTests(unittest.TestCase):
    def test_start_uses_offline_consent_login_hint_and_strong_random_state(self):
        first, second = _flow(), _flow()
        with patch("nexacard_otp.gmail_auth.ensure_private_oauth_files"), patch(
            "nexacard_otp.gmail_auth.Flow.from_client_secrets_file", side_effect=[first, second]
        ):
            coordinator = OAuthCoordinator()
            url = coordinator.start(" Owner@Example.com ", CALLBACK)
            coordinator.start("owner@example.com", CALLBACK)

        self.assertEqual(url, "https://accounts.google.com/authorize")
        first_kwargs = first.authorization_url.call_args.kwargs
        second_kwargs = second.authorization_url.call_args.kwargs
        self.assertEqual(first.redirect_uri, CALLBACK)
        self.assertEqual(first_kwargs["access_type"], "offline")
        self.assertEqual(first_kwargs["prompt"], "consent")
        self.assertEqual(first_kwargs["login_hint"], "owner@example.com")
        self.assertEqual(first_kwargs["include_granted_scopes"], "true")
        self.assertGreaterEqual(len(first_kwargs["state"]), 32)
        self.assertNotEqual(first_kwargs["state"], second_kwargs["state"])
        self.assertIn(first_kwargs["state"], coordinator.pending)

    def test_start_rejects_invalid_verification_email_formats(self):
        coordinator = OAuthCoordinator()

        for email in (
            "",
            "not-an-email",
            "@example.com",
            "owner@",
            "owner@@example.com",
            "owner @example.com",
            "owner@example",
            ".owner@example.com",
            "owner.@example.com",
            "owner@example..com",
            "owner@.example.com",
            "owner@example.com.",
            "owner@-example.com",
        ):
            with self.subTest(email=email), self.assertRaisesRegex(ValueError, "valid verification email"):
                coordinator.start(email, CALLBACK)

    def test_start_rejects_a_flow_that_returns_a_different_state(self):
        flow = _flow(returned_state="attacker-state")
        with patch("nexacard_otp.gmail_auth.ensure_private_oauth_files"), patch(
            "nexacard_otp.gmail_auth.Flow.from_client_secrets_file", return_value=flow
        ):
            coordinator = OAuthCoordinator()
            with self.assertRaisesRegex(ValueError, "state"):
                coordinator.start("owner@example.com", CALLBACK)

        self.assertEqual(coordinator.pending, {})

    def test_start_cleans_expired_entries_and_enforces_pending_capacity(self):
        coordinator = OAuthCoordinator()
        coordinator.max_pending = 3
        expired_flow = _flow()
        coordinator.pending["expired"] = (
            "owner@example.com",
            expired_flow,
            datetime.now(timezone.utc) - coordinator.pending_ttl - timedelta(seconds=1),
        )
        flows = [_flow() for _ in range(4)]
        with patch("nexacard_otp.gmail_auth.ensure_private_oauth_files"), patch(
            "nexacard_otp.gmail_auth.Flow.from_client_secrets_file", side_effect=flows
        ):
            for _ in range(3):
                coordinator.start("owner@example.com", CALLBACK)
            with self.assertRaisesRegex(ValueError, "too many"):
                coordinator.start("owner@example.com", CALLBACK)

        self.assertNotIn("expired", coordinator.pending)
        self.assertEqual(len(coordinator.pending), 3)

    def test_complete_consumes_state_even_when_email_does_not_match(self):
        flow = _flow()
        coordinator = OAuthCoordinator()
        coordinator.pending["state1"] = ("expected@example.com", flow)
        profile = Mock()
        profile.users().getProfile().execute.return_value = {"emailAddress": "other@example.com"}

        with tempfile.TemporaryDirectory() as directory, patch(
            "nexacard_otp.gmail_auth.build", return_value=profile
        ), patch("nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", Path(directory) / "token.json"), patch(
            "nexacard_otp.gmail_auth.PRIVATE_TOKEN_META_PATH", Path(directory) / "token.meta.json"
        ):
            with self.assertRaisesRegex(ValueError, "does not match"):
                coordinator.complete("state1", CALLBACK + "?state=state1&code=abc")
            with self.assertRaisesRegex(ValueError, "missing or expired"):
                coordinator.complete("state1", CALLBACK + "?state=state1&code=abc")
            self.assertFalse((Path(directory) / "token.json").exists())
            self.assertFalse((Path(directory) / "token.meta.json").exists())

    def test_complete_rejects_missing_state_without_exchanging_a_token(self):
        coordinator = OAuthCoordinator()

        with self.assertRaisesRegex(ValueError, "missing or expired"):
            coordinator.complete("unknown", CALLBACK + "?state=unknown&code=abc")

    def test_complete_rejects_mismatched_or_expired_state_without_token_write(self):
        flow = _flow()
        coordinator = OAuthCoordinator()
        coordinator.pending["state1"] = ("owner@example.com", flow)

        with self.assertRaisesRegex(ValueError, "does not match"):
            coordinator.complete("state1", CALLBACK + "?state=other&code=abc")
        flow.fetch_token.assert_not_called()

        coordinator.pending["state2"] = (
            "owner@example.com",
            flow,
            datetime.now(timezone.utc) - coordinator.pending_ttl - timedelta(seconds=1),
        )
        with self.assertRaisesRegex(ValueError, "missing or expired"):
            coordinator.complete("state2", CALLBACK + "?state=state2&code=abc")
        flow.fetch_token.assert_not_called()

    def test_complete_cleans_other_expired_pending_entries(self):
        flow = _flow()
        coordinator = OAuthCoordinator()
        coordinator.pending["active"] = ("owner@example.com", flow, datetime.now(timezone.utc))
        coordinator.pending["expired"] = (
            "owner@example.com",
            _flow(),
            datetime.now(timezone.utc) - coordinator.pending_ttl - timedelta(seconds=1),
        )
        profile = Mock()
        profile.users().getProfile().execute.return_value = {"emailAddress": "owner@example.com"}

        with tempfile.TemporaryDirectory() as directory, patch(
            "nexacard_otp.gmail_auth.build", return_value=profile
        ), patch("nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", Path(directory) / "token.json"), patch(
            "nexacard_otp.gmail_auth.PRIVATE_TOKEN_META_PATH", Path(directory) / "token.meta.json"
        ):
            coordinator.complete("active", CALLBACK + "?state=active&code=abc")

        self.assertNotIn("expired", coordinator.pending)

    def test_concurrent_completion_of_one_state_has_exactly_one_success(self):
        flow = _flow()
        coordinator = OAuthCoordinator()
        coordinator.pending["state1"] = ("owner@example.com", flow, datetime.now(timezone.utc))
        profile = Mock()
        profile.users().getProfile().execute.return_value = {"emailAddress": "owner@example.com"}

        with tempfile.TemporaryDirectory() as directory, patch(
            "nexacard_otp.gmail_auth.build", return_value=profile
        ), patch("nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", Path(directory) / "token.json"), patch(
            "nexacard_otp.gmail_auth.PRIVATE_TOKEN_META_PATH", Path(directory) / "token.meta.json"
        ), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(coordinator.complete, "state1", CALLBACK + "?state=state1&code=abc") for _ in range(2)]
            results = [future.result() if future.exception() is None else future.exception() for future in futures]

        self.assertEqual(sum(isinstance(result, AuthStatus) for result in results), 1)
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        flow.fetch_token.assert_called_once()

    def test_complete_writes_private_token_and_secret_free_metadata_with_supplied_expiry(self):
        flow = _flow()
        flow.oauth2session.token = {"refresh_token_expires_in": 90}
        coordinator = OAuthCoordinator()
        coordinator.pending["state1"] = ("owner@example.com", flow)
        profile = Mock()
        profile.users().getProfile().execute.return_value = {"emailAddress": "Owner@Example.com"}

        with tempfile.TemporaryDirectory() as directory, patch(
            "nexacard_otp.gmail_auth.build", return_value=profile
        ), patch("nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", Path(directory) / "token.json"), patch(
            "nexacard_otp.gmail_auth.PRIVATE_TOKEN_META_PATH", Path(directory) / "token.meta.json"
        ):
            before = datetime.now(timezone.utc)
            status = coordinator.complete("state1", CALLBACK + "?state=state1&code=abc")
            after = datetime.now(timezone.utc)
            metadata = json.loads((Path(directory) / "token.meta.json").read_text(encoding="utf-8"))

        self.assertEqual(status.state, "valid")
        self.assertFalse(status.estimated)
        self.assertEqual(metadata["authorized_email"], "owner@example.com")
        self.assertEqual(set(metadata), {"authorized_email", "authorized_at", "estimated_expires_at", "estimated"})
        self.assertNotIn("secret", json.dumps(metadata))
        self.assertGreaterEqual(status.estimated_expires_at, before + timedelta(seconds=89))
        self.assertLessEqual(status.estimated_expires_at, after + timedelta(seconds=91))

    def test_complete_estimates_seven_day_expiry_when_google_omits_refresh_expiry(self):
        flow = _flow()
        flow.oauth2session.token = {}
        coordinator = OAuthCoordinator()
        coordinator.pending["state1"] = ("owner@example.com", flow)
        profile = Mock()
        profile.users().getProfile().execute.return_value = {"emailAddress": "owner@example.com"}

        with tempfile.TemporaryDirectory() as directory, patch(
            "nexacard_otp.gmail_auth.build", return_value=profile
        ), patch("nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", Path(directory) / "token.json"), patch(
            "nexacard_otp.gmail_auth.PRIVATE_TOKEN_META_PATH", Path(directory) / "token.meta.json"
        ):
            before = datetime.now(timezone.utc)
            status = coordinator.complete("state1", CALLBACK + "?state=state1&code=abc")
            after = datetime.now(timezone.utc)

        self.assertTrue(status.estimated)
        self.assertGreaterEqual(status.estimated_expires_at, before + timedelta(days=7) - timedelta(seconds=1))
        self.assertLessEqual(status.estimated_expires_at, after + timedelta(days=7) + timedelta(seconds=1))


class NexaCardOAuthWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_start_route_returns_only_the_google_authorization_url(self):
        with patch.object(server.NEXACARD_OAUTH, "start", return_value="https://accounts.google.com/authorize"):
            response = self.client.post("/api/nexacard/oauth/start", json={"email": "owner@example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "authorization_url": "https://accounts.google.com/authorize"})

    def test_callback_only_displays_whitelisted_local_errors_and_never_echoes_secrets(self):
        with patch.object(server.NEXACARD_OAUTH, "complete", side_effect=ValueError("OAuth state is missing or expired")):
            safe = self.client.get("/api/nexacard/oauth/callback?state=bad")

        self.assertEqual(safe.status_code, 400)
        self.assertIn("OAuth state is missing or expired", safe.text)

        for message, secret in (
            ("token=secret-value", "secret-value"),
            ("code=secret-code", "secret-code"),
            ("client_secret=secret-client", "secret-client"),
            ("http://127.0.0.1:8799/api/nexacard/oauth/callback?code=url-secret&state=bad", "url-secret"),
            ("<untrusted-error>", "<untrusted-error>"),
        ):
            with self.subTest(message=message), patch.object(server.NEXACARD_OAUTH, "complete", side_effect=RuntimeError(message)):
                response = self.client.get("/api/nexacard/oauth/callback?state=bad&code=request-secret")

            self.assertEqual(response.status_code, 400)
            self.assertIn("Google authorization could not be completed safely", response.text)
            self.assertNotIn(secret, response.text)
            self.assertNotIn("request-secret", response.text)

    def test_callback_success_escapes_authorized_email(self):
        status = AuthStatus("valid", "ok", "owner+<tag>@example.com")
        with patch.object(server.NEXACARD_OAUTH, "complete", return_value=status):
            response = self.client.get("/api/nexacard/oauth/callback?state=ok")

        self.assertEqual(response.status_code, 200)
        self.assertIn("owner+&lt;tag&gt;@example.com", response.text)
        self.assertNotIn("owner+<tag>@example.com", response.text)

    def test_status_route_serializes_safe_auth_status_without_token_fields(self):
        expires_at = datetime(2026, 7, 26, tzinfo=timezone.utc)
        status = AuthStatus("valid", "authorization available", "owner@example.com", expires_at, True)
        with patch.object(server, "get_auth_status", return_value=status) as get_status:
            response = self.client.get("/api/nexacard/oauth/status?email=Owner%40Example.com")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "state": "valid",
            "message": "authorization available",
            "authorized_email": "owner@example.com",
            "estimated_expires_at": "2026-07-26T00:00:00+00:00",
            "estimated": True,
        })
        get_status.assert_called_once_with("owner@example.com")


if __name__ == "__main__":
    unittest.main()
