import asyncio
import inspect
import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import register
from common.claude_email_accounts import (
    ClaudeEmailAccount,
    ClaudeEmailAccountStore,
)


def args(**overrides):
    values = {
        "count": 1,
        "emails": None,
        "email": None,
        "password": "",
        "token": "",
        "client_id": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ClaudeNineMallCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def factory(self, **kwargs):
        return ClaudeEmailAccountStore(root_dir=self.root, **kwargs)

    def test_default_ninemail_reserves_count_from_mail_file(self):
        (self.root / "mail.txt").write_text(
            "a@example.com----pa----client-a----refresh-a\n"
            "b@example.com----pb----client-b----refresh-b\n",
            encoding="utf-8",
        )
        accounts, _store = register.prepare_email_accounts(
            args(count=2), provider="NINEMALL", store_factory=self.factory
        )
        self.assertEqual(
            [item.email for item in accounts],
            ["a@example.com", "b@example.com"],
        )
        self.assertEqual(
            [item.client_id for item in accounts], ["client-a", "client-b"]
        )

    def test_ninemail_explicit_account_requires_token_and_client_id(self):
        with self.assertRaisesRegex(
            SystemExit, "requires --token and --client-id"
        ):
            register.prepare_email_accounts(
                args(email="a@example.com", token="refresh-a"),
                provider="NINEMALL",
                store_factory=self.factory,
            )

    def test_ninemail_emails_override_uses_new_column_order(self):
        source = self.root / "custom.txt"
        source.write_text(
            "a@example.com----pa----client-a----refresh-a\n",
            encoding="utf-8",
        )
        accounts, _store = register.prepare_email_accounts(
            args(emails=str(source)),
            provider="NINEMALL",
            store_factory=self.factory,
        )
        self.assertEqual(accounts[0].client_id, "client-a")
        self.assertEqual(accounts[0].refresh_token, "refresh-a")

    def test_outlook_without_explicit_accounts_keeps_self_registration_slots(self):
        accounts, _store = register.prepare_email_accounts(
            args(count=2), provider="OUTLOOK", store_factory=self.factory
        )
        self.assertEqual(accounts, [None, None])

    def test_outlook_explicit_file_is_read_without_preconsuming_rows(self):
        source = self.root / "custom-outlook.txt"
        source.write_text(
            "a@example.com----pa----refresh-a----client-a\n"
            "b@example.com----pb----refresh-b----client-b\n",
            encoding="utf-8",
        )
        accounts, store = register.prepare_email_accounts(
            args(emails=str(source)),
            provider="OUTLOOK",
            store_factory=self.factory,
        )

        self.assertEqual(
            [account.email for account in accounts],
            ["a@example.com", "b@example.com"],
        )
        self.assertFalse((self.root / "emails_used.txt").exists())
        self.assertFalse((self.root / "emails_error.txt").exists())
        self.assertEqual(store.reserve_one().email, "a@example.com")

    def test_explicit_ninemail_account_keeps_all_four_fields(self):
        accounts, _store = register.prepare_email_accounts(
            args(
                email="a@example.com",
                password="pa",
                token="refresh-a",
                client_id="client-a",
            ),
            provider="NINEMALL",
            store_factory=self.factory,
        )
        self.assertEqual(
            accounts[0],
            ClaudeEmailAccount(
                provider="NINEMALL",
                email="a@example.com",
                password="pa",
                client_id="client-a",
                refresh_token="refresh-a",
            ),
        )

    def test_state_helpers_delegate_without_legacy_password_writes(self):
        account = ClaudeEmailAccount(
            "NINEMALL", "a@example.com", "pa", "client-a", "refresh-a"
        )
        store = Mock()
        with patch.object(register, "mark_email_used") as legacy_used, patch.object(
            register, "mark_email_error"
        ) as legacy_error:
            register.mark_claude_account_used(account, store)
            register.mark_claude_account_error(account, store, "http_401")
        store.mark_used.assert_called_once_with(account)
        store.mark_error.assert_called_once_with(account, "http_401")
        legacy_used.assert_not_called()
        legacy_error.assert_not_called()

    def test_cli_never_previews_session_tokens(self):
        source = "\n".join(
            (
                inspect.getsource(register.save_cookies),
                inspect.getsource(register.handle_onboarding),
                inspect.getsource(register.register),
                inspect.getsource(register.main),
            )
        )
        self.assertNotRegex(source, r"(?:session_key|sk_cookie|\[\"sk\"\])\[:")
        self.assertNotRegex(
            inspect.getsource(register.save_cookies), r"print\(f[^\n]*\{e\}"
        )
        for function in (
            register._pick_claude_node,
            register.configure_claude_proxy,
            register.solve_turnstile,
            register.hero_get_phone_number,
            register.hero_get_sms_code,
            register.handle_birthday_page,
            register.handle_onboarding,
            register._get_and_verify_phone,
            register.register,
            register.main,
        ):
            self.assertNotRegex(
                inspect.getsource(function),
                r"print\(f[^\n]*\{(?:e|exc|err_msg)\}",
            )
        self.assertNotRegex(
            inspect.getsource(register.handle_onboarding),
            r"log_claude_flow_error\(\s*f",
        )

    def test_ninemail_safe_logger_never_renders_exception_or_account_secrets(self):
        account = ClaudeEmailAccount(
            "NINEMALL", "a@example.com", "pa", "client-a", "refresh-a"
        )
        leaked = (
            "https://claude.ai/magic-link/path?password=pa&"
            "client_id=client-a#refresh-a"
        )
        with patch("builtins.print") as output:
            register.log_claude_flow_error(
                "registration_step_failed", RuntimeError(leaked), account=account
            )
        rendered = " ".join(str(call) for call in output.call_args_list)
        self.assertIn("registration_step_failed", rendered)
        for secret in (
            leaked,
            "magic-link/path",
            "password=pa",
            "client_id=client-a",
            "refresh-a",
        ):
            self.assertNotIn(secret, rendered)


class ClaudeNineMallMainLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.account = ClaudeEmailAccount(
            "NINEMALL", "a@example.com", "pa", "client-a", "refresh-a"
        )
        self.store = Mock()

    def main_patches(self, **overrides):
        values = {
            "prepare_email_accounts": ([self.account], self.store),
            "lease_from_env": None,
            "settings_from_env": SimpleNamespace(enabled=False),
            "BitBrowser": Mock(),
        }
        values.update(overrides)
        return values

    def assert_secret_free(self, output, leaked):
        rendered = " ".join(str(call) for call in output.call_args_list)
        for secret in (
            leaked,
            "magic-link/path",
            "password=pa",
            "client_id=client-a",
            "refresh-a",
        ):
            self.assertNotIn(secret, rendered)

    async def test_unexpected_proxy_failure_releases_account_for_reselection(self):
        (self.root / "mail.txt").write_text(
            "a@example.com----pa----client-a----refresh-a\n",
            encoding="utf-8",
        )
        store = ClaudeEmailAccountStore("NINEMALL", "mail.txt", self.root)
        account = store.reserve_one()
        leaked = (
            "proxy failed https://claude.ai/magic-link/path?password=pa&"
            "client_id=client-a#refresh-a"
        )
        patches = self.main_patches(
            prepare_email_accounts=([account], store),
            settings_from_env=SimpleNamespace(enabled=True),
            acquire_proxy=RuntimeError(leaked),
        )
        with patch.object(sys, "argv", ["register.py", "--node", "none"]), patch.object(
            register, "prepare_email_accounts", return_value=patches["prepare_email_accounts"]
        ), patch.object(
            register, "lease_from_env", return_value=patches["lease_from_env"]
        ), patch.object(
            register, "settings_from_env", return_value=patches["settings_from_env"]
        ), patch.object(
            register, "prepare_claude_network"
        ), patch.object(
            register, "configure_claude_proxy"
        ), patch.object(
            register, "BitBrowser", return_value=patches["BitBrowser"]
        ), patch.object(
            register, "acquire_proxy", side_effect=patches["acquire_proxy"]
        ), patch("builtins.print") as output:
            await register.main()

        self.assert_secret_free(output, leaked)
        selected = ClaudeEmailAccountStore(
            "NINEMALL", "mail.txt", self.root
        ).reserve_one()
        self.assertEqual(selected.email, account.email)

    async def test_escaped_registration_exception_is_sanitized_and_terminal(self):
        leaked = "registration failed with synthetic-secret"
        patches = self.main_patches()
        with patch.object(sys, "argv", ["register.py", "--node", "none"]), patch.object(
            register, "prepare_email_accounts", return_value=patches["prepare_email_accounts"]
        ), patch.object(
            register, "lease_from_env", return_value=patches["lease_from_env"]
        ), patch.object(
            register, "settings_from_env", return_value=patches["settings_from_env"]
        ), patch.object(
            register, "prepare_claude_network"
        ), patch.object(
            register, "configure_claude_proxy"
        ), patch.object(
            register, "BitBrowser", return_value=patches["BitBrowser"]
        ), patch.object(
            register, "create_claude_profile", return_value="profile-a"
        ), patch.object(
            register, "register", new=AsyncMock(side_effect=RuntimeError(leaked))
        ), patch("builtins.print") as output:
            await register.main()

        self.store.mark_error.assert_called_once_with(
            self.account, "registration_error"
        )
        self.assert_secret_free(output, leaked)

    async def test_profile_creation_exception_is_sanitized_and_terminal(self):
        leaked = (
            "network https://claude.ai/magic-link/path?password=pa&"
            "client_id=client-a#refresh-a"
        )
        patches = self.main_patches()
        with patch.object(sys, "argv", ["register.py", "--node", "none"]), patch.object(
            register, "prepare_email_accounts", return_value=patches["prepare_email_accounts"]
        ), patch.object(
            register, "lease_from_env", return_value=patches["lease_from_env"]
        ), patch.object(
            register, "settings_from_env", return_value=patches["settings_from_env"]
        ), patch.object(
            register, "prepare_claude_network"
        ), patch.object(
            register, "configure_claude_proxy"
        ), patch.object(
            register, "BitBrowser", return_value=patches["BitBrowser"]
        ), patch.object(
            register, "create_claude_profile", side_effect=RuntimeError(leaked)
        ), patch.object(
            register.asyncio, "sleep", new=AsyncMock()
        ), patch("builtins.print") as output:
            await register.main()

        self.store.mark_error.assert_called_once_with(
            self.account, "registration_error"
        )
        self.assert_secret_free(output, leaked)

    async def test_bitbrowser_construction_failure_releases_reserved_account(self):
        (self.root / "mail.txt").write_text(
            "a@example.com----pa----client-a----refresh-a\n",
            encoding="utf-8",
        )
        store = ClaudeEmailAccountStore("NINEMALL", "mail.txt", self.root)
        account = store.reserve_one()
        leaked = "constructor failed #refresh-a?client_id=client-a"
        with patch.object(sys, "argv", ["register.py", "--node", "none"]), patch.object(
            register, "prepare_email_accounts", return_value=([account], store)
        ), patch.object(
            register, "lease_from_env", return_value=None
        ), patch.object(
            register, "settings_from_env", return_value=SimpleNamespace(enabled=False)
        ), patch.object(
            register, "prepare_claude_network"
        ), patch.object(
            register, "configure_claude_proxy"
        ), patch.object(
            register, "BitBrowser", side_effect=RuntimeError(leaked)
        ), patch("builtins.print") as output, self.assertRaisesRegex(
            RuntimeError, "browser_initialization_failed"
        ):
            await register.main()

        self.assert_secret_free(output, leaked)
        selected = ClaudeEmailAccountStore(
            "NINEMALL", "mail.txt", self.root
        ).reserve_one()
        self.assertEqual(selected.email, account.email)

    async def test_cancelled_partial_batch_releases_only_unfinished_accounts(self):
        (self.root / "mail.txt").write_text(
            "a@example.com----pa----client-a----refresh-a\n"
            "b@example.com----pb----client-b----refresh-b\n",
            encoding="utf-8",
        )
        store = ClaudeEmailAccountStore("NINEMALL", "mail.txt", self.root)
        accounts = store.reserve_many(limit=2)
        second_started = asyncio.Event()

        async def registration(_profile_id, *, account, account_store, **_kwargs):
            if account.email == "a@example.com":
                account_store.mark_used(account)
                return "synthetic-session"
            second_started.set()
            await asyncio.Future()

        with patch.object(
            sys, "argv", ["register.py", "--count", "2", "--concurrency", "2", "--node", "none"]
        ), patch.object(
            register, "prepare_email_accounts", return_value=(accounts, store)
        ), patch.object(
            register, "lease_from_env", return_value=None
        ), patch.object(
            register, "settings_from_env", return_value=SimpleNamespace(enabled=False)
        ), patch.object(
            register, "prepare_claude_network"
        ), patch.object(
            register, "configure_claude_proxy"
        ), patch.object(
            register, "BitBrowser", return_value=Mock()
        ), patch.object(
            register, "create_claude_profile", return_value="profile"
        ), patch.object(
            register, "register", new=registration
        ), patch.object(
            register.random, "uniform", return_value=0
        ):
            batch = asyncio.create_task(register.main())
            await second_started.wait()
            batch.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await batch

        selected = ClaudeEmailAccountStore(
            "NINEMALL", "mail.txt", self.root
        ).reserve_many(limit=2)
        self.assertEqual([account.email for account in selected], ["b@example.com"])


if __name__ == "__main__":
    unittest.main()
