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

    def test_message_before_send_time_is_ignored(self):
        raw = Path("tests/fixtures/nexacard_verification_code.eml").read_bytes()

        self.assertIsNone(parse_login_code(_encoded(raw), int(SENT_AFTER.timestamp() * 1000) - 1, SENT_AFTER))

    def test_message_in_same_millisecond_as_send_time_is_accepted(self):
        raw = Path("tests/fixtures/nexacard_verification_code.eml").read_bytes()
        sent_after = datetime(2026, 7, 19, 4, 54, 0, 123456, tzinfo=timezone.utc)

        self.assertEqual(
            parse_login_code(_encoded(raw), int(sent_after.timestamp() * 1000), sent_after),
            "123456789",
        )

    def test_baseline_message_ids_skip_old_same_millisecond_code_but_accept_new_one(self):
        raw = _encoded(Path("tests/fixtures/nexacard_verification_code.eml").read_bytes())
        sent_after = datetime(2026, 7, 19, 4, 54, 0, 123456, tzinfo=timezone.utc)
        message_id = int(sent_after.timestamp() * 1000)
        messages = Mock()
        messages.list.return_value.execute.return_value = {
            "messages": [{"id": "old"}, {"id": "new"}]
        }
        messages.get.return_value.execute.return_value = {"raw": raw, "internalDate": str(message_id)}
        service = Mock()
        service.users.return_value.messages.return_value = messages
        reader = GmailCodeReader()

        with patch("nexacard_otp.gmail_reader.build", return_value=service), patch(
            "nexacard_otp.gmail_reader.load_valid_credentials"
        ):
            code = reader._fetch_once(sent_after, excluded_message_ids=frozenset({"old"}))

        self.assertEqual(code, "123456789")
        messages.get.assert_called_once_with(userId="me", id="new", format="raw")

    def test_message_id_snapshot_uses_the_same_bounded_matching_query(self):
        messages = Mock()
        messages.list.return_value.execute.return_value = {
            "messages": [{"id": "old"}, {"id": "new"}]
        }
        service = Mock()
        service.users.return_value.messages.return_value = messages
        reader = GmailCodeReader()

        with patch("nexacard_otp.gmail_reader.build", return_value=service), patch(
            "nexacard_otp.gmail_reader.load_valid_credentials"
        ):
            snapshot = asyncio.run(reader.snapshot_login_message_ids())

        self.assertEqual(snapshot, frozenset({"old", "new"}))
        messages.list.assert_called_once_with(
            userId="me",
            q='from:(jushihui@mail.jushipay.com) subject:"NexaCard Verification Code" newer_than:1d',
            maxResults=10,
        )

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

    def test_parser_ignores_filename_parts_without_attachment_disposition(self):
        message_prefix = (
            b"From: NexaCardVCC <jushihui@mail.jushipay.com>\n"
            b"Subject: NexaCard Verification Code\nMIME-Version: 1.0\n"
        )
        parts = (
            b"Content-Type: text/plain; name=legacy-code.txt\n\n123456789",
            b"Content-Type: text/plain\nContent-Disposition: inline; filename=inline-code.txt\n\n123456789",
        )

        for part in parts:
            with self.subTest(part=part):
                self.assertIsNone(parse_login_code(_encoded(message_prefix + part), RECEIVED_MS, SENT_AFTER))

    def test_naive_sent_after_is_rejected_without_using_host_timezone(self):
        raw = Path("tests/fixtures/nexacard_verification_code.eml").read_bytes()

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            parse_login_code(_encoded(raw), RECEIVED_MS, SENT_AFTER.replace(tzinfo=None))

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

    def test_temporary_profile_failure_still_removes_token_fields_from_metadata(self):
        credentials = Mock()
        profile = Mock()
        profile.users().getProfile().execute.side_effect = OSError("offline")
        with tempfile.TemporaryDirectory() as directory:
            meta_path = Path(directory) / "token.meta.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "authorized_email": "owner@example.com",
                        "access_token": "must-not-survive",
                        "refresh_token": "must-not-survive",
                    }
                ),
                encoding="utf-8",
            )
            with patch("nexacard_otp.gmail_auth.load_valid_credentials", return_value=credentials), patch(
                "nexacard_otp.gmail_auth.PRIVATE_TOKEN_META_PATH", meta_path
            ), patch("nexacard_otp.gmail_auth.build", return_value=profile):
                status = get_auth_status("owner@example.com")

            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(status.state, "unknown")
            self.assertEqual(metadata, {"authorized_email": "owner@example.com"})

    def test_second_profile_http_failure_remains_unknown_after_one_refresh(self):
        for retry_status in (401, 503):
            with self.subTest(retry_status=retry_status), tempfile.TemporaryDirectory() as directory:
                credentials = Mock(to_json=Mock(return_value="{}"))
                first_profile = Mock()
                first_profile.users().getProfile().execute.side_effect = HttpError(Mock(status=401), b"unauthorized")
                retry_profile = Mock()
                retry_profile.users().getProfile().execute.side_effect = HttpError(
                    Mock(status=retry_status), b"unavailable"
                )
                with patch("nexacard_otp.gmail_auth.load_valid_credentials", return_value=credentials), patch(
                    "nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", Path(directory) / "token.json"
                ), patch("nexacard_otp.gmail_auth.PRIVATE_TOKEN_META_PATH", Path(directory) / "token.meta.json"), patch(
                    "nexacard_otp.gmail_auth.build", side_effect=[first_profile, retry_profile]
                ):
                    self.assertEqual(get_auth_status("owner@example.com").state, "unknown")
                credentials.refresh.assert_called_once()

    def test_temporary_gmail_failure_is_retried_inside_bounded_mail_poll(self):
        reader = GmailCodeReader()
        reader._fetch_once = Mock(side_effect=[GmailTemporarilyUnavailable("temporary"), "123456789"])
        with patch("nexacard_otp.gmail_reader.asyncio.sleep", new=AsyncMock()):
            code = asyncio.run(reader.wait_for_login_code(SENT_AFTER, interval_seconds=0.01, max_attempts=2))

        self.assertEqual(code, "123456789")
        self.assertEqual(reader._fetch_once.call_count, 2)

    def test_authorization_failure_is_not_reclassified_as_mail_timeout(self):
        reader = GmailCodeReader()
        reader._fetch_once = Mock(side_effect=GmailAuthorizationRequired("authorization required"))

        with self.assertRaises(GmailAuthorizationRequired):
            asyncio.run(reader.wait_for_login_code(SENT_AFTER, interval_seconds=0.01, max_attempts=2))

    def test_exhausted_temporary_gmail_failures_keep_the_last_failure_as_cause(self):
        reader = GmailCodeReader()
        temporary = GmailTemporarilyUnavailable("temporary")
        last_temporary = GmailTemporarilyUnavailable("temporary")
        reader._fetch_once = Mock(side_effect=[temporary, last_temporary])
        with patch("nexacard_otp.gmail_reader.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(GmailTemporarilyUnavailable) as captured:
                asyncio.run(reader.wait_for_login_code(SENT_AFTER, interval_seconds=0.01, max_attempts=2))

        self.assertIs(captured.exception, last_temporary)

    def test_successful_no_mail_poll_ends_with_timeout_without_temporary_cause(self):
        reader = GmailCodeReader()
        reader._fetch_once = Mock(side_effect=[GmailTemporarilyUnavailable("temporary"), None, None])
        with patch("nexacard_otp.gmail_reader.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(TimeoutError) as captured:
                asyncio.run(reader.wait_for_login_code(SENT_AFTER, interval_seconds=0.01, max_attempts=3))

        self.assertIsNone(captured.exception.__cause__)

    def test_mail_poll_is_bounded_when_no_code_arrives(self):
        reader = GmailCodeReader()
        reader._fetch_once = Mock(return_value=None)
        with patch("nexacard_otp.gmail_reader.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(TimeoutError):
                asyncio.run(reader.wait_for_login_code(SENT_AFTER, interval_seconds=0.01, max_attempts=3))

        self.assertEqual(reader._fetch_once.call_count, 3)


if __name__ == "__main__":
    unittest.main()
