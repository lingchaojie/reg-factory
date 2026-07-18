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
                inspect.getsource(register.register),
                inspect.getsource(register.main),
            )
        )
        self.assertNotRegex(source, r"(?:session_key|sk_cookie|\[\"sk\"\])\[:")
        self.assertNotRegex(
            inspect.getsource(register.save_cookies), r"print\(f[^\n]*\{e\}"
        )


class ClaudeNineMallMainLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
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

    async def test_proxy_failure_marks_reserved_account_error(self):
        leaked = "proxy failed with synthetic-secret"
        patches = self.main_patches(
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
            register, "acquire_proxy", side_effect=register.IPMartProxyError(leaked)
        ), patch("builtins.print") as output:
            await register.main()

        self.store.mark_error.assert_called_once_with(
            self.account, "registration_error"
        )
        self.assertNotIn(leaked, " ".join(str(call) for call in output.call_args_list))

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
        self.assertNotIn(leaked, " ".join(str(call) for call in output.call_args_list))

    async def test_profile_creation_exception_is_sanitized_and_terminal(self):
        leaked = "profile failed with synthetic-secret"
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
        ), patch("builtins.print") as output:
            await register.main()

        self.store.mark_error.assert_called_once_with(
            self.account, "registration_error"
        )
        self.assertNotIn(leaked, " ".join(str(call) for call in output.call_args_list))


if __name__ == "__main__":
    unittest.main()
