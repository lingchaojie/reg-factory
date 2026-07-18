import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from common import claude_platform_mailbox
from common.claude_platform_mailbox import (
    ClaudePlatformMessage,
    ClaudePlatformVerification,
    extract_claude_platform_verification,
)


class ClaudePlatformMailboxTests(unittest.TestCase):
    def message(self, subject, body, received="2033-05-18T03:33:25Z"):
        return ClaudePlatformMessage(
            sender="no-reply@claude.com",
            subject=subject,
            received=received,
            body=body,
        )

    def test_code_only_message_returns_code_without_waiting_for_link(self):
        result = extract_claude_platform_verification([
            self.message("Your Claude verification code is 482731", "Sign in")
        ])

        self.assertEqual(result.code, "482731")
        self.assertEqual(result.magic_link, "")

    def test_magic_link_only_message_returns_validated_platform_link(self):
        result = extract_claude_platform_verification([
            self.message(
                "Sign in to Claude Platform",
                '<a href="https://platform.claude.com/magic-link?code=abc">Continue</a>',
            )
        ])

        self.assertEqual(
            result.magic_link,
            "https://platform.claude.com/magic-link?code=abc",
        )
        self.assertEqual(result.code, "")

    def test_magic_link_trailing_bracket_is_not_part_of_the_artifact(self):
        result = extract_claude_platform_verification([
            self.message(
                "Sign in to Claude Platform",
                "(https://platform.claude.com/magic-link?code=abc]",
            )
        ])

        self.assertEqual(
            result.magic_link,
            "https://platform.claude.com/magic-link?code=abc",
        )

    def test_safelinks_target_is_decoded_to_validated_platform_link(self):
        wrapped = (
            "https://nam01.safelinks.protection.outlook.com/"
            "?url=https%3A%2F%2Fplatform.claude.com%2Fmagic-link%3Fcode%3Dsafe"
        )

        result = extract_claude_platform_verification([
            self.message("Claude Platform login", wrapped)
        ])

        self.assertEqual(
            result.magic_link,
            "https://platform.claude.com/magic-link?code=safe",
        )

    def test_both_artifacts_are_returned_without_global_priority(self):
        result = extract_claude_platform_verification([
            self.message(
                "Verification code: 482731",
                "https://platform.claude.com/magic-link?code=abc",
            )
        ])

        self.assertEqual(result.code, "482731")
        self.assertTrue(result.magic_link)

    def test_newest_matching_message_wins_regardless_of_artifact_kind(self):
        result = extract_claude_platform_verification([
            self.message(
                "Claude Platform login",
                "https://platform.claude.com/magic-link?code=older",
                received="2033-05-18T03:33:25Z",
            ),
            self.message(
                "Your Claude login code is 482731",
                "Use this code to sign in",
                received="2033-05-18T03:33:26Z",
            ),
        ])

        self.assertEqual(result.code, "482731")
        self.assertEqual(result.magic_link, "")

    def test_stale_or_unparseable_messages_are_rejected_after_resend(self):
        result = extract_claude_platform_verification(
            [
                self.message(
                    "Your Claude verification code is 482731",
                    "Sign in",
                    received="2020-01-01T00:00:00Z",
                ),
                self.message(
                    "Your Claude verification code is 999999",
                    "Sign in",
                    received="not a date",
                ),
            ],
            received_after=2_000_000_000,
        )

        self.assertIsNone(result)

    def test_dates_css_and_unrelated_numbers_are_rejected(self):
        result = extract_claude_platform_verification([
            self.message(
                "Claude notice 20260719",
                '<style>.x{color:#482731}</style><p>Invoice 123456</p>',
            )
        ])

        self.assertIsNone(result)

    def test_only_four_to_ten_digit_codes_are_accepted(self):
        for code in ("123", "12345678901"):
            with self.subTest(code=code):
                result = extract_claude_platform_verification([
                    self.message(
                        f"Your Claude verification code is {code}",
                        "Sign in",
                    )
                ])

                self.assertIsNone(result)

    def test_platform_link_rejects_noncanonical_authorities(self):
        for link in (
            "https://platform.claude.com:444/magic-link?code=port",
            "https://user:pass@platform.claude.com/magic-link?code=credentials",
        ):
            with self.subTest(link=link):
                result = extract_claude_platform_verification([
                    self.message("Sign in to Claude Platform", link)
                ])

                self.assertIsNone(result)

    def test_platform_link_requires_exact_magic_link_path(self):
        invalid_paths = ("/magic-link/", "/magic-link////", "/other-path")
        for path in invalid_paths:
            with self.subTest(kind="direct", path=path):
                result = extract_claude_platform_verification([
                    self.message(
                        "Sign in to Claude Platform",
                        f"https://platform.claude.com{path}?code=invalid",
                    )
                ])

                self.assertIsNone(result)

            with self.subTest(kind="safelinks", path=path):
                wrapped_path = path.replace("/", "%2F").replace("?", "%3F")
                result = extract_claude_platform_verification([
                    self.message(
                        "Sign in to Claude Platform",
                        "https://nam01.safelinks.protection.outlook.com/"
                        f"?url=https%3A%2F%2Fplatform.claude.com{wrapped_path}%3Fcode%3Dinvalid",
                    )
                ])

                self.assertIsNone(result)

    def test_graph_poll_returns_both_artifacts_and_forwards_account_route(self):
        lease = object()
        received_after = 2_000_000_000.0
        graph_message = {
            "subject": "Your Claude Platform verification code is 482731",
            "from": "no-reply@anthropic.com",
            "body": (
                '<html><body><p>Use this login code to sign in.</p>'
                '<a href="https://platform.claude.com/magic-link?code=graph-secret">'
                "Continue</a></body></html>"
            ),
            "received": "2033-05-18T03:33:25Z",
        }

        with patch("common.mailbox._get_access_token", return_value="access-token") as token, patch(
            "common.mailbox.fetch_messages",
            side_effect=([graph_message], []),
        ) as fetch, patch("builtins.print") as output:
            result = claude_platform_mailbox.get_claude_platform_verification_by_token(
                "person@example.com",
                "refresh-secret",
                "client-guid",
                max_wait=1,
                received_after=received_after,
                account_lease=lease,
            )

        self.assertEqual(result.code, "482731")
        self.assertEqual(
            result.magic_link,
            "https://platform.claude.com/magic-link?code=graph-secret",
        )
        deadline = token.call_args.kwargs["deadline"]
        token.assert_called_once_with(
            "refresh-secret",
            "client-guid",
            account_lease=lease,
            deadline=deadline,
        )
        self.assertEqual(
            fetch.call_args_list,
            [
                call(
                    "access-token",
                    "inbox",
                    top=10,
                    account_lease=lease,
                    deadline=deadline,
                ),
                call(
                    "access-token",
                    "junkemail",
                    top=10,
                    account_lease=lease,
                    deadline=deadline,
                ),
            ],
        )
        rendered = " ".join(str(item) for item in output.call_args_list)
        for secret in (
            "482731",
            "graph-secret",
            "refresh-secret",
            "client-guid",
            "person@example.com",
        ):
            self.assertNotIn(secret, rendered)

    def test_graph_poll_rejects_stale_artifact_using_received_after(self):
        stale = {
            "subject": "Your Claude verification code is 482731",
            "from": "no-reply@anthropic.com",
            "body": "Use this login code to sign in.",
            "received": "2020-01-01T00:00:00Z",
        }
        with patch("common.mailbox._get_access_token", return_value="token"), patch(
            "common.mailbox.fetch_messages", return_value=[stale]
        ), patch("builtins.print"):
            result = claude_platform_mailbox.get_claude_platform_verification_by_token(
                "person@example.com",
                "refresh-secret",
                "client-guid",
                max_wait=0.001,
                poll=0.001,
                received_after=2_000_000_000.0,
            )

        self.assertIsNone(result)

    def browser_page(self, candidates, payload):
        page = Mock()
        page.evaluate = AsyncMock(
            side_effect=(
                candidates,
                True,
                payload,
            )
        )
        return page

    def test_browser_folder_scan_uses_email_timestamp_for_both_artifacts(self):
        page = self.browser_page(
            [
                {
                    "index": 0,
                    "visible": True,
                    "received": "2033-05-18T03:33:25Z",
                    "stable_id": "message-a",
                }
            ],
            {
                "subject": "Your Claude Platform verification code is 482731",
                "body": (
                    "Use this login code. "
                    "https://platform.claude.com/magic-link?code=browser-secret"
                ),
            },
        )

        with patch.object(claude_platform_mailbox.asyncio, "sleep", new=AsyncMock()):
            result = asyncio.run(
                claude_platform_mailbox._scan_claude_platform_folder(
                    page, received_after=2_000_000_000.0
                )
            )

        self.assertEqual(result.code, "482731")
        self.assertEqual(
            result.magic_link,
            "https://platform.claude.com/magic-link?code=browser-secret",
        )
        self.assertEqual(result.received_at, 2_000_000_005.0)
        self.assertEqual(page.evaluate.await_args_list[1].args[1], 0)

    def test_browser_scan_ignores_hidden_first_match(self):
        page = self.browser_page(
            [
                {
                    "index": 0,
                    "visible": False,
                    "received": "2033-05-18T03:33:30Z",
                    "stable_id": "hidden",
                },
                {
                    "index": 1,
                    "visible": True,
                    "received": "2033-05-18T03:33:25Z",
                    "stable_id": "visible",
                },
            ],
            {"subject": "Claude login code 482731", "body": "Sign in"},
        )

        with patch.object(claude_platform_mailbox.asyncio, "sleep", new=AsyncMock()):
            result = asyncio.run(claude_platform_mailbox._scan_claude_platform_folder(page))

        self.assertEqual(result.code, "482731")
        self.assertEqual(page.evaluate.await_args_list[1].args[1], 1)

    def test_browser_scan_selects_newest_timestamp_not_dom_order(self):
        page = self.browser_page(
            [
                {
                    "index": 0,
                    "visible": True,
                    "received": "2033-05-18T03:33:25Z",
                    "stable_id": "older",
                },
                {
                    "index": 1,
                    "visible": True,
                    "received": "2033-05-18T03:33:35Z",
                    "stable_id": "newer",
                },
            ],
            {"subject": "Claude login code 482731", "body": "Sign in"},
        )

        with patch.object(claude_platform_mailbox.asyncio, "sleep", new=AsyncMock()):
            result = asyncio.run(claude_platform_mailbox._scan_claude_platform_folder(page))

        self.assertEqual(result.received_at, 2_000_000_015.0)
        self.assertEqual(page.evaluate.await_args_list[1].args[1], 1)

    def test_browser_scan_accepts_stable_epoch_millisecond_metadata(self):
        page = self.browser_page(
            [{
                "index": 0,
                "visible": True,
                "received": "2000000015000",
                "stable_id": "epoch-millis",
            }],
            {"subject": "Claude login code 482731", "body": "Sign in"},
        )

        with patch.object(claude_platform_mailbox.asyncio, "sleep", new=AsyncMock()):
            result = asyncio.run(
                claude_platform_mailbox._scan_claude_platform_folder(
                    page, received_after=2_000_000_000.0
                )
            )

        self.assertEqual(result.received_at, 2_000_000_015.0)

    def test_browser_scan_rejects_stale_or_unknown_time_after_resend(self):
        for received in ("2020-01-01T00:00:00Z", ""):
            with self.subTest(received=received):
                page = self.browser_page(
                    [{"index": 0, "visible": True, "received": received, "stable_id": "same"}],
                    {"subject": "Claude login code 482731", "body": "Sign in"},
                )
                with patch.object(
                    claude_platform_mailbox.asyncio, "sleep", new=AsyncMock()
                ):
                    result = asyncio.run(
                        claude_platform_mailbox._scan_claude_platform_folder(
                            page, received_after=2_000_000_000.0
                        )
                    )
                self.assertIsNone(result)

    def test_browser_scan_unknown_time_uses_stable_sentinel_without_filter(self):
        results = []
        for _ in range(2):
            page = self.browser_page(
                [{"index": 0, "visible": True, "received": "", "stable_id": "same"}],
                {"subject": "Claude login code 482731", "body": "Sign in"},
            )
            with patch.object(claude_platform_mailbox.asyncio, "sleep", new=AsyncMock()):
                results.append(
                    asyncio.run(claude_platform_mailbox._scan_claude_platform_folder(page))
                )

        self.assertEqual(results[0].received_at, results[1].received_at)
        self.assertEqual(results[0].received_at, 0.0)

    def test_graph_deadline_starts_before_token_acquisition(self):
        clock = Mock(side_effect=(100.0, 101.0))
        with patch.object(claude_platform_mailbox.time, "monotonic", clock), patch(
            "common.mailbox._get_access_token", return_value="token"
        ) as token, patch("common.mailbox.fetch_messages") as fetch:
            result = claude_platform_mailbox.get_claude_platform_verification_by_token(
                "person@example.com", "refresh-secret", "client-guid", max_wait=1
            )

        self.assertIsNone(result)
        self.assertEqual(token.call_args.kwargs["deadline"], 101.0)
        fetch.assert_not_called()

    def test_graph_deadline_stops_before_second_folder(self):
        now = [100.0]

        def fetch_folder(_token, folder, **_kwargs):
            if folder == "inbox":
                now[0] = 101.0
            return []

        with patch.object(
            claude_platform_mailbox.time, "monotonic", side_effect=lambda: now[0]
        ), patch("common.mailbox._get_access_token", return_value="token"), patch(
            "common.mailbox.fetch_messages", side_effect=fetch_folder
        ) as fetch, patch.object(claude_platform_mailbox.time, "sleep") as sleep:
            result = claude_platform_mailbox.get_claude_platform_verification_by_token(
                "person@example.com", "refresh-secret", "client-guid", max_wait=1
            )

        self.assertIsNone(result)
        self.assertEqual([item.args[1] for item in fetch.call_args_list], ["inbox"])
        sleep.assert_not_called()

    def test_outlook_deadline_starts_before_login_and_skips_navigation_when_spent(self):
        page = Mock()
        page.goto = AsyncMock()
        now = [100.0]

        async def spend_budget(*_args, **_kwargs):
            now[0] = 101.0
            return True

        with patch.object(
            claude_platform_mailbox.time, "monotonic", side_effect=lambda: now[0]
        ), patch("common.mailbox._outlook_login", new=AsyncMock(side_effect=spend_budget)) as login, patch(
            "common.mailbox._click_folder", new=AsyncMock()
        ) as click_folder, patch.object(claude_platform_mailbox.asyncio, "sleep", new=AsyncMock()) as sleep:
            result = asyncio.run(
                claude_platform_mailbox.get_claude_platform_verification_outlook_pw(
                    page, "person@example.com", "mail-pass", max_wait=1
                )
            )

        self.assertIsNone(result)
        self.assertEqual(login.await_args.kwargs["deadline"], 101.0)
        page.goto.assert_not_awaited()
        click_folder.assert_not_awaited()
        sleep.assert_not_awaited()

    def test_outlook_password_poll_scans_shared_inbox_and_junk_names(self):
        page = Mock()
        page.goto = AsyncMock()
        verification = ClaudePlatformVerification(code="482731", received_at=2_000_000_001.0)

        with patch("common.mailbox._outlook_login", new=AsyncMock(return_value=True)) as login, patch(
            "common.mailbox._click_folder", new=AsyncMock()
        ) as click_folder, patch.object(
            claude_platform_mailbox,
            "_scan_claude_platform_folder",
            new=AsyncMock(side_effect=(None, verification)),
        ) as scan, patch.object(claude_platform_mailbox.asyncio, "sleep", new=AsyncMock()):
            result = asyncio.run(
                claude_platform_mailbox.get_claude_platform_verification_outlook_pw(
                    page,
                    "person@example.com",
                    "mail-pass",
                    max_wait=1,
                    received_after=2_000_000_000.0,
                )
            )

        from common import mailbox

        self.assertEqual(result, verification)
        self.assertGreater(page.goto.await_args.kwargs["timeout"], 0)
        self.assertLessEqual(page.goto.await_args.kwargs["timeout"], 1000)
        deadline = login.await_args.kwargs["deadline"]
        login.assert_awaited_once_with(
            page,
            "person@example.com",
            "mail-pass",
            deadline=deadline,
        )
        self.assertEqual(
            click_folder.await_args_list,
            [call(page, mailbox.INBOX_NAMES), call(page, mailbox.JUNK_NAMES)],
        )
        self.assertEqual(
            scan.await_args_list,
            [
                call(
                    page,
                    received_after=2_000_000_000.0,
                    deadline=deadline,
                ),
                call(
                    page,
                    received_after=2_000_000_000.0,
                    deadline=deadline,
                ),
            ],
        )

if __name__ == "__main__":
    unittest.main()
