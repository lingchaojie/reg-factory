import asyncio
import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from nexacard_otp.errors import GmailAuthorizationRequired, GmailTemporarilyUnavailable
from nexacard_otp.gmail_auth import get_auth_status, load_valid_credentials
from nexacard_otp.gmail_reader import GmailCodeReader, parse_login_code


SENT_AFTER = datetime(2026, 7, 19, 4, 54, tzinfo=timezone.utc)
RECEIVED_MS = int(datetime(2026, 7, 19, 4, 54, 10, tzinfo=timezone.utc).timestamp() * 1000)


def _encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class NexaCardGmailTests(unittest.TestCase):
    def _token_context(self, directory: str, credentials: Mock):
        token_path = Path(directory) / "token.json"
        token_path.write_text("{}", encoding="utf-8")
        return patch("nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", token_path), patch(
            "nexacard_otp.gmail_auth.Credentials.from_authorized_user_file",
            return_value=credentials,
        ), patch("nexacard_otp.gmail_auth.ensure_private_oauth_files")

    def test_sample_message_yields_nine_digit_code(self):
        raw = Path("tests/fixtures/nexacard_verification_code.eml").read_bytes()

        self.assertEqual(parse_login_code(_encoded(raw), RECEIVED_MS, SENT_AFTER), "123456789")

    def test_message_at_or_before_send_time_is_ignored(self):
        raw = Path("tests/fixtures/nexacard_verification_code.eml").read_bytes()

        self.assertIsNone(parse_login_code(_encoded(raw), int(SENT_AFTER.timestamp() * 1000), SENT_AFTER))

    def test_sender_and_subject_must_match_exactly(self):
        messages = (
            b"From: attacker <jushihui@mail.jushipay.com.attacker.test>\n"
            b"Subject: NexaCard Verification Code\n\n123456789",
            b"From: Nexa <jushihui@mail.jushipay.com>, attacker <attacker@example.com>\n"
            b"Subject: NexaCard Verification Code\n\n123456789",
            b"From: jushihui@mail.jushipay.com\nFrom: attacker@example.com\n"
            b"Subject: NexaCard Verification Code\n\n123456789",
            b"From: jushihui@mail.jushipay.com\nSubject: NexaCard Verification Code!\n\n123456789",
        )

        for raw in messages:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_login_code(_encoded(raw), RECEIVED_MS, SENT_AFTER))

    def test_parser_ignores_attachment_and_non_standalone_numbers(self):
        raw = (
            b"From: NexaCardVCC <jushihui@mail.jushipay.com>\n"
            b"Subject: NexaCard Verification Code\nMIME-Version: 1.0\n"
            b'Content-Type: multipart/mixed; boundary="part"\n\n'
            b"--part\nContent-Type: text/plain; charset=utf-8\n\nUse 1234567890 instead.\n"
            b"--part\nContent-Type: text/plain; name=code.txt\n"
            b"Content-Disposition: attachment; filename=code.txt\n\n123456789\n--part--\n"
        )

        self.assertIsNone(parse_login_code(_encoded(raw), RECEIVED_MS, SENT_AFTER))

    def test_expired_access_token_refreshes_and_is_rewritten(self):
        credentials = Mock(valid=False, expired=True, refresh_token="refresh", to_json=Mock(return_value='{"token":"new"}'))
        with tempfile.TemporaryDirectory() as directory:
            token_patch, credentials_patch, ensure_patch = self._token_context(directory, credentials)
            with token_patch, credentials_patch, ensure_patch:
                result = load_valid_credentials()

            token_path = Path(directory) / "token.json"
            self.assertIs(result, credentials)
            credentials.refresh.assert_called_once()
            self.assertEqual(json.loads(token_path.read_text(encoding="utf-8"))["token"], "new")
            self.assertFalse(token_path.with_suffix(".json.tmp").exists())

    def test_missing_corrupt_or_non_refreshable_tokens_require_reauthorization(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token.json"
            with patch("nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", token_path), patch(
                "nexacard_otp.gmail_auth.ensure_private_oauth_files"
            ):
                with self.assertRaises(GmailAuthorizationRequired):
                    load_valid_credentials()

            token_path.write_text("not-json", encoding="utf-8")
            with patch("nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", token_path), patch(
                "nexacard_otp.gmail_auth.Credentials.from_authorized_user_file", side_effect=ValueError("bad")
            ), patch("nexacard_otp.gmail_auth.ensure_private_oauth_files"):
                with self.assertRaises(GmailAuthorizationRequired):
                    load_valid_credentials()

            credentials = Mock(valid=False, expired=False, refresh_token=None)
            token_patch, credentials_patch, ensure_patch = self._token_context(directory, credentials)
            with token_patch, credentials_patch, ensure_patch:
                with self.assertRaises(GmailAuthorizationRequired):
                    load_valid_credentials()

    def test_invalid_grant_requires_reauthorization_but_network_error_does_not(self):
        credentials = Mock(valid=False, expired=True, refresh_token="refresh")
        credentials.refresh.side_effect = RefreshError("invalid_grant: Token has been expired or revoked")
        with tempfile.TemporaryDirectory() as directory:
            token_patch, credentials_patch, ensure_patch = self._token_context(directory, credentials)
            with token_patch, credentials_patch, ensure_patch:
                with self.assertRaises(GmailAuthorizationRequired):
                    load_valid_credentials()

            credentials.refresh.side_effect = OSError("temporary network failure")
            token_patch, credentials_patch, ensure_patch = self._token_context(directory, credentials)
            with token_patch, credentials_patch, ensure_patch:
                with self.assertRaises(GmailTemporarilyUnavailable):
                    load_valid_credentials()

    def test_auth_status_retries_profile_401_and_only_persists_safe_metadata(self):
        credentials = Mock(to_json=Mock(return_value='{"token":"refreshed"}'))
        unauthorized = HttpError(Mock(status=401), b"unauthorized")
        failed_profile = Mock()
        failed_profile.users().getProfile().execute.side_effect = unauthorized
        valid_profile = Mock()
        valid_profile.users().getProfile().execute.return_value = {"emailAddress": "Owner@Example.com"}
        with tempfile.TemporaryDirectory() as directory:
            meta_path = Path(directory) / "token.meta.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "access_token": "must-not-survive",
                        "authorized_email": "owner@example.com",
                        "estimated_expires_at": "2026-07-20T00:00:00+00:00",
                        "estimated": True,
                    }
                ),
                encoding="utf-8",
            )
            with patch("nexacard_otp.gmail_auth.load_valid_credentials", return_value=credentials), patch(
                "nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", Path(directory) / "token.json"
            ), patch("nexacard_otp.gmail_auth.PRIVATE_TOKEN_META_PATH", meta_path), patch(
                "nexacard_otp.gmail_auth.build", side_effect=[failed_profile, valid_profile]
            ):
                status = get_auth_status("owner@example.com")

            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(status.state, "valid")
            self.assertEqual(status.authorized_email, "owner@example.com")
            self.assertTrue(status.estimated)
            credentials.refresh.assert_called_once()
            self.assertNotIn("access_token", metadata)
            self.assertEqual(metadata["authorized_email"], "owner@example.com")

    def test_auth_status_mismatch_and_temporary_profile_error_are_distinct(self):
        credentials = Mock()
        profile = Mock()
        profile.users().getProfile().execute.return_value = {"emailAddress": "other@example.com"}
        with tempfile.TemporaryDirectory() as directory:
            with patch("nexacard_otp.gmail_auth.load_valid_credentials", return_value=credentials), patch(
                "nexacard_otp.gmail_auth.PRIVATE_TOKEN_META_PATH", Path(directory) / "token.meta.json"
            ), patch("nexacard_otp.gmail_auth.build", return_value=profile):
                self.assertEqual(get_auth_status("owner@example.com").state, "mismatch")

        profile.users().getProfile().execute.side_effect = OSError("offline")
        with patch("nexacard_otp.gmail_auth.load_valid_credentials", return_value=credentials), patch(
            "nexacard_otp.gmail_auth.build", return_value=profile
        ):
            self.assertEqual(get_auth_status("owner@example.com").state, "unknown")

    def test_temporary_gmail_failure_is_retried_inside_bounded_mail_poll(self):
        reader = GmailCodeReader()
        reader._fetch_once = Mock(side_effect=[GmailTemporarilyUnavailable("temporary"), "123456789"])
        with patch("nexacard_otp.gmail_reader.asyncio.sleep", new=AsyncMock()):
            code = asyncio.run(reader.wait_for_login_code(SENT_AFTER, interval_seconds=0.01, max_attempts=2))

        self.assertEqual(code, "123456789")
        self.assertEqual(reader._fetch_once.call_count, 2)

    def test_mail_poll_is_bounded_when_no_code_arrives(self):
        reader = GmailCodeReader()
        reader._fetch_once = Mock(return_value=None)
        with patch("nexacard_otp.gmail_reader.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(TimeoutError):
                asyncio.run(reader.wait_for_login_code(SENT_AFTER, interval_seconds=0.01, max_attempts=3))

        self.assertEqual(reader._fetch_once.call_count, 3)


if __name__ == "__main__":
    unittest.main()
