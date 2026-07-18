import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nexacard_otp.models import CardType
from nexacard_otp.settings import (
    PRIVATE_CREDENTIALS_PATH,
    PRIVATE_TOKEN_PATH,
    ensure_private_oauth_files,
    load_settings,
)


class NexaCardSettingsTests(unittest.TestCase):
    def test_private_directory_is_created_without_legacy_oauth_files(self):
        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory) / "private"
            with patch("nexacard_otp.settings.PRIVATE_DIR", private_dir), patch(
                "nexacard_otp.settings.PRIVATE_CREDENTIALS_PATH", private_dir / "credentials.json"
            ), patch("nexacard_otp.settings.PRIVATE_TOKEN_PATH", private_dir / "token.json"), patch(
                "nexacard_otp.settings.LEGACY_CREDENTIALS_PATH", Path(directory) / "missing-credentials.json"
            ), patch("nexacard_otp.settings.LEGACY_TOKEN_PATH", Path(directory) / "missing-token.json"):
                ensure_private_oauth_files()
            self.assertTrue(private_dir.is_dir())

    def test_defaults_are_headless_shanghai_three_seconds_one_hundred(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(Path(directory) / ".env")
        self.assertTrue(settings.headless)
        self.assertEqual(settings.page_timezone.key, "Asia/Shanghai")
        self.assertEqual(settings.poll_interval_seconds, 3.0)
        self.assertEqual(settings.max_attempts, 100)
        self.assertEqual(settings.service_host, "127.0.0.1")

    def test_current_env_file_values_override_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "NEXACARD_ACCOUNT=user1\n"
                "NEXACARD_PASSWORD=secret1\n"
                "NEXACARD_VERIFICATION_EMAIL=mail@example.com\n"
                "NEXACARD_HEADLESS=false\n"
                "NEXACARD_OTP_POLL_INTERVAL_SECONDS=4.5\n"
                "NEXACARD_OTP_MAX_ATTEMPTS=12\n",
                encoding="utf-8",
            )
            settings = load_settings(env_path)
        self.assertEqual(settings.account, "user1")
        self.assertFalse(settings.headless)
        self.assertEqual(settings.poll_interval_seconds, 4.5)
        self.assertEqual(settings.max_attempts, 12)

    def test_changed_env_file_beats_stale_service_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("NEXACARD_OTP_MAX_ATTEMPTS=25\n", encoding="utf-8")
            with patch.dict(os.environ, {"NEXACARD_OTP_MAX_ATTEMPTS": "100"}):
                settings = load_settings(env_path)
        self.assertEqual(settings.max_attempts, 25)

    def test_non_positive_polling_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "NEXACARD_OTP_POLL_INTERVAL_SECONDS=0\n"
                "NEXACARD_OTP_MAX_ATTEMPTS=-1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "positive"):
                load_settings(env_path)

    def test_legacy_oauth_files_copy_only_when_private_files_are_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_credentials = root / "source-credentials.json"
            source_token = root / "source-token.json"
            destination_credentials = root / "private" / "credentials.json"
            destination_token = root / "private" / "token.json"
            source_credentials.write_text('{"installed": {}}', encoding="utf-8")
            source_token.write_text('{"refresh_token": "old"}', encoding="utf-8")
            with patch("nexacard_otp.settings.PRIVATE_CREDENTIALS_PATH", destination_credentials), patch(
                "nexacard_otp.settings.PRIVATE_TOKEN_PATH", destination_token
            ), patch("nexacard_otp.settings.LEGACY_CREDENTIALS_PATH", source_credentials), patch(
                "nexacard_otp.settings.LEGACY_TOKEN_PATH", source_token
            ):
                ensure_private_oauth_files()
                destination_token.write_text('{"refresh_token": "new"}', encoding="utf-8")
                ensure_private_oauth_files()
            self.assertTrue(destination_credentials.exists())
            self.assertEqual(destination_token.read_text(encoding="utf-8"), '{"refresh_token": "new"}')

    def test_card_type_values_are_stable_api_names(self):
        self.assertEqual(CardType.NEXACARD_B.value, "NexaCardB")
        self.assertEqual(CardType.THREE_D_1.value, "3D-1卡")
