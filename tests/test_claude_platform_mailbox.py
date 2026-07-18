import unittest

from common.claude_platform_mailbox import (
    ClaudePlatformMessage,
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


if __name__ == "__main__":
    unittest.main()
