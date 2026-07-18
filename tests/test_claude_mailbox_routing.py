import unittest
from unittest.mock import AsyncMock, Mock, patch

import register
from common.claude_email_accounts import ClaudeEmailAccount


def mailbox_account(provider):
    return ClaudeEmailAccount(
        provider=provider,
        email="person@example.com",
        password="mail-pass",
        client_id="client-guid",
        refresh_token="refresh-secret",
    )


class ClaudeMailboxRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_fallback_passes_magic_link_as_data(self):
        magic_link = (
            "https://claude.ai/magic-link#token`"
            "${globalThis.injected = true}`"
        )
        page = Mock()
        page.goto = AsyncMock(side_effect=RuntimeError("navigation failed"))
        page.evaluate = AsyncMock()
        page.wait_for_load_state = AsyncMock()

        await register.navigate_to_claude_magic_link(page, magic_link, timeout=60_000)

        page.evaluate.assert_awaited_once_with(
            "(url) => { window.location.href = url; }", magic_link
        )
        script = page.evaluate.await_args.args[0]
        self.assertNotIn("globalThis.injected", script)
        page.wait_for_load_state.assert_awaited_once_with(
            "domcontentloaded", timeout=60_000
        )

    async def test_safe_page_origin_log_omits_magic_link_and_fragment(self):
        token = "secret-token"
        page_url = f"https://claude.ai/magic-link#token`$\u007b{token}\u007d`"

        with patch("builtins.print") as output:
            register.log_safe_page_origin("  URL origin: ", page_url)

        printed = " ".join(str(arg) for call in output.call_args_list for arg in call.args)
        self.assertEqual(printed, "  URL origin: https://claude.ai")
        self.assertNotIn(token, printed)
        self.assertNotIn("magic-link", printed)

    async def test_ninemail_uses_only_hosted_client(self):
        context = Mock()
        context.new_page = AsyncMock(side_effect=AssertionError("Outlook page opened"))
        client = Mock()
        client.poll_magic_link.return_value = "https://claude.ai/magic-link#hosted-token"
        with patch.object(register, "get_magic_link_by_token") as graph, patch.object(
            register, "get_magic_link_outlook_pw", new=AsyncMock()
        ) as browser:
            result = await register.fetch_claude_magic_link(
                context, mailbox_account("NINEMALL"), 60, ninemail_client=client
            )
        self.assertEqual(result, "https://claude.ai/magic-link#hosted-token")
        graph.assert_not_called()
        browser.assert_not_awaited()
        context.new_page.assert_not_awaited()

    async def test_ninemail_failure_never_opens_outlook(self):
        context = Mock()
        context.new_page = AsyncMock(side_effect=AssertionError("Outlook page opened"))
        client = Mock()
        client.poll_magic_link.return_value = None
        result = await register.fetch_claude_magic_link(
            context, mailbox_account("NINEMALL"), 60, ninemail_client=client
        )
        self.assertIsNone(result)
        context.new_page.assert_not_awaited()

    async def test_outlook_token_path_receives_account_client_id(self):
        context = Mock()
        context.new_page = AsyncMock()
        with patch.object(
            register,
            "get_magic_link_by_token",
            return_value="https://claude.ai/magic-link#graph-token",
        ) as graph:
            result = await register.fetch_claude_magic_link(
                context,
                mailbox_account("OUTLOOK"),
                45,
                account_lease="lease-object",
            )
        self.assertEqual(result, "https://claude.ai/magic-link#graph-token")
        graph.assert_called_once_with(
            "person@example.com",
            "refresh-secret",
            client_id="client-guid",
            max_wait=45,
            account_lease="lease-object",
        )
        context.new_page.assert_not_awaited()

    async def test_outlook_browser_fallback_closes_page(self):
        page = Mock()
        page.close = AsyncMock()
        context = Mock()
        context.new_page = AsyncMock(return_value=page)
        with patch.object(
            register, "get_magic_link_by_token", return_value=None
        ), patch.object(
            register,
            "get_magic_link_outlook_pw",
            new=AsyncMock(return_value="https://claude.ai/magic-link#browser-token"),
        ) as browser:
            result = await register.fetch_claude_magic_link(
                context, mailbox_account("OUTLOOK"), 30
            )
        self.assertEqual(result, "https://claude.ai/magic-link#browser-token")
        browser.assert_awaited_once_with(
            page, "person@example.com", "mail-pass", max_wait=30
        )
        page.close.assert_awaited_once_with()

    async def test_ninemail_received_after_is_forwarded(self):
        client = Mock()
        client.poll_magic_link.return_value = None
        await register.fetch_claude_magic_link(
            Mock(),
            mailbox_account("NINEMALL"),
            25,
            received_after=1_234.5,
            ninemail_client=client,
        )
        client.poll_magic_link.assert_called_once_with(
            mailbox_account("NINEMALL"), 25, 1_234.5
        )


if __name__ == "__main__":
    unittest.main()
