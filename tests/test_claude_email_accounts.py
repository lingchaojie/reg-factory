import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common.claude_email_accounts import (
    AccountFormatError,
    ClaudeEmailAccountStore,
    normalize_email_provider,
    reserve_shared_claude_account,
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

    def test_claude_and_claude_api_ledgers_are_independent(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        claude = ClaudeEmailAccountStore(
            "NINEMALL", source, self.root, purpose="claude"
        )
        account = claude.reserve_one()
        claude.mark_used(account)

        api = ClaudeEmailAccountStore(
            "NINEMALL", source, self.root, purpose="claude_api"
        )
        selected = api.reserve_one()

        self.assertEqual(selected.email, account.email)
        self.assertTrue((self.root / "mail_used_claude.txt").exists())
        self.assertTrue((self.root / "mail_used_claude_api.txt").exists())

    def test_shared_reservation_skips_address_blocked_for_one_purpose(self):
        source = self.write(
            "mail.txt",
            NINEMALL_ROW + "\n"
            "second@example.com----pass----client-2----refresh-2\n",
        )
        blocked = ClaudeEmailAccountStore(
            "NINEMALL", source, self.root, purpose="claude"
        )
        first = blocked.reserve_one()
        blocked.mark_used(first)

        result = reserve_shared_claude_account(
            "NINEMALL", ("claude", "claude_api"), source, self.root
        )

        account, stores = result
        self.assertEqual(account.email, "second@example.com")
        self.assertEqual(set(stores), {"claude", "claude_api"})

    def test_outlook_claude_api_uses_separate_state_files(self):
        source = self.write("emails.txt", OUTLOOK_ROW + "\n")
        store = ClaudeEmailAccountStore(
            "OUTLOOK", source, self.root, purpose="claude_api"
        )
        account = store.reserve_one()
        store.mark_error(account, "mail_timeout")

        self.assertTrue((self.root / "emails_used_claude_api.txt").exists())
        self.assertTrue((self.root / "emails_error_claude_api.txt").exists())
        self.assertFalse((self.root / "emails_used.txt").exists())
        state = (self.root / "emails_error_claude_api.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("legacy@example.com----MailboxPass2!----mail_timeout", state)

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

    def test_nonpositive_limit_returns_empty_without_state_writes(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)

        self.assertEqual(store.reserve_many(limit=0), [])
        self.assertEqual(store.reserve_many(limit=-1), [])

        self.assertFalse((self.root / "mail_used_claude.txt").exists())
        self.assertFalse((self.root / "mail_error_claude.txt").exists())

    def test_mark_error_sanitizes_reason(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = store.reserve_one()
        store.mark_error(account, "HTTP 401 refresh-secret")
        state = (self.root / "mail_error_claude.txt").read_text(encoding="utf-8")
        self.assertIn("http_401", state)
        self.assertNotIn("refresh-secret", state)

    def test_mark_error_preserves_stable_http_403_code(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = store.reserve_one()

        store.mark_error(account, "http_403")

        state = (self.root / "mail_error_claude.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("person@example.com----http_403", state)

    def test_released_reservation_is_selectable_after_store_restart(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = store.reserve_one()

        store.release(account)

        selected = ClaudeEmailAccountStore(
            "NINEMALL", source, self.root
        ).reserve_one()
        self.assertEqual(selected.email, account.email)
        state = (self.root / "mail_used_claude.txt").read_text(encoding="utf-8")
        self.assertIn("person@example.com----released", state)
        self.assertNotIn(account.password, state)
        self.assertNotIn(account.client_id, state)
        self.assertNotIn(account.refresh_token, state)

    def test_terminal_success_remains_blocked_after_later_release(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        self.write(
            "mail_used_claude.txt",
            "person@example.com----reserved\n"
            "person@example.com----ok\n"
            "person@example.com----released\n",
        )

        selected = ClaudeEmailAccountStore(
            "NINEMALL", source, self.root
        ).reserve_one()

        self.assertIsNone(selected)

    def test_terminal_error_remains_blocked_after_later_release(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        self.write(
            "mail_used_claude.txt",
            "person@example.com----reserved\n"
            "person@example.com----released\n",
        )
        self.write(
            "mail_error_claude.txt",
            "person@example.com----registration_error\n",
        )

        selected = ClaudeEmailAccountStore(
            "NINEMALL", source, self.root
        ).reserve_one()

        self.assertIsNone(selected)

    def test_nonterminal_release_remains_selectable(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        self.write(
            "mail_used_claude.txt",
            "person@example.com----reserved\n"
            "person@example.com----released\n",
        )

        selected = ClaudeEmailAccountStore(
            "NINEMALL", source, self.root
        ).reserve_one()

        self.assertEqual(selected.email, "person@example.com")

    def test_terminal_reservation_cannot_be_released(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = store.reserve_one()
        store.mark_used(account)

        store.release(account)

        self.assertIsNone(
            ClaudeEmailAccountStore("NINEMALL", source, self.root).reserve_one()
        )
        state = (self.root / "mail_used_claude.txt").read_text(encoding="utf-8")
        self.assertNotIn("released", state)

    def test_terminal_success_from_sibling_store_cannot_be_released(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        owner = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = owner.reserve_one()
        sibling = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        sibling.mark_used(account)

        self.assertFalse(owner.release(account))

        state = (self.root / "mail_used_claude.txt").read_text(encoding="utf-8")
        self.assertNotIn("released", state)
        self.assertIsNone(
            ClaudeEmailAccountStore("NINEMALL", source, self.root).reserve_one()
        )

    def test_terminal_error_from_sibling_store_cannot_be_released(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        owner = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = owner.reserve_one()
        sibling = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        sibling.mark_error(account, "registration_error")

        self.assertFalse(owner.release(account))

        state = (self.root / "mail_used_claude.txt").read_text(encoding="utf-8")
        self.assertNotIn("released", state)
        self.assertIsNone(
            ClaudeEmailAccountStore("NINEMALL", source, self.root).reserve_one()
        )


if __name__ == "__main__":
    unittest.main()
