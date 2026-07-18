import unittest

from common.claude_email_accounts import ClaudeEmailAccount
from common.ninemail_mailbox import (
    NineMallMailboxClient,
    NineMallMailboxError,
    NineMallMessage,
    extract_claude_magic_link,
)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        return self.responses.pop(0)


class FakeClock:
    def __init__(self, value=2_000_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def account():
    return ClaudeEmailAccount(
        provider="NINEMALL",
        email="person@example.com",
        password="mail-pass",
        client_id="client-guid",
        refresh_token="refresh-secret",
    )


def message(body, date="2033-05-18T03:33:25Z", sender="no-reply@claude.ai"):
    return {
        "send": sender,
        "subject": "Your Claude sign-in link",
        "date": date,
        "html": body,
        "text": "",
    }


class NineMallMailboxTests(unittest.TestCase):
    def client(self, responses):
        self.clock = FakeClock()
        self.session = FakeSession(responses)
        return NineMallMailboxClient(
            base_url="https://www.appleemail.top",
            api_password="service-pass",
            http_timeout=17,
            poll_interval=5,
            session=self.session,
            sleep=self.clock.sleep,
            clock=self.clock,
        )

    def test_post_contract_keeps_credentials_out_of_url(self):
        client = self.client([FakeResponse(200, {"data": []})])
        client.fetch_folder(account(), "INBOX")
        url, payload, timeout = self.session.calls[0]
        self.assertEqual(url, "https://www.appleemail.top/api/mail-all")
        self.assertEqual(timeout, 17)
        self.assertEqual(payload, {
            "refresh_token": "refresh-secret",
            "client_id": "client-guid",
            "email": "person@example.com",
            "mailbox": "INBOX",
            "response_type": "json",
            "password": "service-pass",
        })
        self.assertNotIn("refresh-secret", url)
        self.assertNotIn("client-guid", url)

    def test_inbox_then_junk_finds_direct_magic_link(self):
        client = self.client([
            FakeResponse(200, {"data": []}),
            FakeResponse(200, {"data": [message(
                '<a href="https://claude.ai/magic-link#direct-token">Sign in</a>'
            )]}),
        ])
        result = client.poll_magic_link(account(), max_wait=20)
        self.assertEqual(result, "https://claude.ai/magic-link#direct-token")
        self.assertEqual(
            [call[1]["mailbox"] for call in self.session.calls],
            ["INBOX", "Junk"],
        )

    def test_safelinks_target_is_decoded_and_validated(self):
        good = (
            "https://nam01.safelinks.protection.outlook.com/"
            "?url=https%3A%2F%2Fclaude.ai%2Fmagic-link%23safe-token"
        )
        bad = (
            "https://nam01.safelinks.protection.outlook.com/"
            "?url=https%3A%2F%2Fexample.invalid%2Fmagic-link%23bad-token"
        )
        messages = [
            NineMallMessage("no-reply@claude.ai", "Claude login", "2033-05-18T03:33:25Z", bad),
            NineMallMessage("no-reply@claude.ai", "Claude login", "2033-05-18T03:33:26Z", good),
        ]
        self.assertEqual(
            extract_claude_magic_link(messages),
            "https://claude.ai/magic-link#safe-token",
        )

    def test_stale_message_is_rejected_after_resend(self):
        messages = [NineMallMessage(
            "no-reply@claude.ai",
            "Claude login",
            "2020-01-01T00:00:00Z",
            "https://claude.ai/magic-link#stale-token",
        )]
        self.assertIsNone(extract_claude_magic_link(messages, received_after=2_000_000_000))

    def test_nothing_to_fetch_is_empty_result(self):
        client = self.client([FakeResponse(500, {"data": {"error": "Nothing to fetch"}})])
        self.assertEqual(client.fetch_folder(account(), "INBOX"), [])

    def test_429_and_5xx_retry_three_times(self):
        client = self.client([
            FakeResponse(429, {}),
            FakeResponse(503, {}),
            FakeResponse(200, {"data": []}),
        ])
        self.assertEqual(client.fetch_folder(account(), "INBOX"), [])
        self.assertEqual(len(self.session.calls), 3)

    def test_401_is_non_retryable_and_secret_safe(self):
        client = self.client([FakeResponse(401, {"error": "refresh-secret rejected"})])
        with self.assertRaises(NineMallMailboxError) as caught:
            client.fetch_folder(account(), "INBOX")
        self.assertEqual(caught.exception.code, "http_401")
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("refresh-secret", str(caught.exception))
        self.assertEqual(len(self.session.calls), 1)

    def test_new_refresh_token_is_ignored(self):
        client = self.client([
            FakeResponse(200, {"data": [], "new_refresh_token": "replacement-secret"}),
            FakeResponse(200, {"data": [message(
                "https://claude.ai/magic-link#original-token"
            )]}),
        ])
        self.assertEqual(
            client.poll_magic_link(account(), max_wait=20),
            "https://claude.ai/magic-link#original-token",
        )
        self.assertEqual(
            {call[1]["refresh_token"] for call in self.session.calls},
            {"refresh-secret"},
        )

    def test_non_https_base_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            NineMallMailboxClient(base_url="http://www.appleemail.top")


if __name__ == "__main__":
    unittest.main()
