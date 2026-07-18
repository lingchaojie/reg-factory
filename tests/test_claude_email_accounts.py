import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common.claude_email_accounts import (
    AccountFormatError,
    ClaudeEmailAccountStore,
    normalize_email_provider,
)


NINEMALL_ROW = "person@example.com----MailboxPass1!----client-guid----refresh-secret"
OUTLOOK_ROW = "legacy@example.com----MailboxPass2!----refresh-old----client-old"


class ClaudeEmailAccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_provider_defaults_and_validation(self):
        self.assertEqual(normalize_email_provider(None), "NINEMALL")
        self.assertEqual(normalize_email_provider(""), "NINEMALL")
        self.assertEqual(normalize_email_provider("outlook"), "OUTLOOK")
        with self.assertRaisesRegex(ValueError, "unsupported email provider"):
            normalize_email_provider("unknown")

    def test_ninemail_column_order(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = store.reserve_one()
        self.assertEqual(account.email, "person@example.com")
        self.assertEqual(account.client_id, "client-guid")
        self.assertEqual(account.refresh_token, "refresh-secret")

    def test_outlook_column_order(self):
        source = self.write("emails.txt", OUTLOOK_ROW + "\n")
        store = ClaudeEmailAccountStore("OUTLOOK", source, self.root)
        account = store.reserve_one()
        self.assertEqual(account.client_id, "client-old")
        self.assertEqual(account.refresh_token, "refresh-old")

    def test_ninemail_requires_exactly_four_nonempty_fields(self):
        with self.assertRaises(AccountFormatError) as caught:
            ClaudeEmailAccountStore.parse_line(
                "person@example.com----password----client-only", "NINEMALL", 7
            )
        self.assertIn("line 7", str(caught.exception))
        self.assertNotIn("password", str(caught.exception))
        self.assertNotIn("client-only", str(caught.exception))

    def test_reservation_never_writes_secrets_or_source(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        before = source.read_bytes()
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = store.reserve_one()
        self.assertEqual(source.read_bytes(), before)
        state = (self.root / "mail_used_claude.txt").read_text(encoding="utf-8")
        self.assertIn("person@example.com", state)
        self.assertNotIn(account.password, state)
        self.assertNotIn(account.client_id, state)
        self.assertNotIn(account.refresh_token, state)

    def test_concurrent_reservations_are_distinct(self):
        rows = "\n".join(
            f"user{i}@example.com----pass{i}----client{i}----refresh{i}"
            for i in range(8)
        )
        source = self.write("mail.txt", rows + "\n")
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        with ThreadPoolExecutor(max_workers=8) as pool:
            accounts = list(pool.map(lambda _i: store.reserve_one(), range(8)))
        self.assertEqual(len({account.email for account in accounts}), 8)

    def test_mark_error_sanitizes_reason(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = store.reserve_one()
        store.mark_error(account, "HTTP 401 refresh-secret")
        state = (self.root / "mail_error_claude.txt").read_text(encoding="utf-8")
        self.assertIn("http_401", state)
        self.assertNotIn("refresh-secret", state)


if __name__ == "__main__":
    unittest.main()
