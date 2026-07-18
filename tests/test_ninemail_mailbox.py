import unittest

import requests

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
        self.json_calls = 0

    def json(self):
        self.json_calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses, clock=None, elapsed=None):
        self.responses = list(responses)
        self.calls = []
        self.clock = clock
        self.elapsed = list(elapsed or [])

    def post(self, url, json, timeout, allow_redirects=True):
        self.calls.append((url, json, timeout, allow_redirects))
        if self.clock is not None and self.elapsed:
            self.clock.sleep(self.elapsed.pop(0))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClock:
    def __init__(self, value=2_000_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class CancellingEvent:
    def __init__(self, clock):
        self.clock = clock
        self.cancelled = False

    def is_set(self):
        return self.cancelled

    def wait(self, seconds):
        self.clock.sleep(min(seconds, 0.25))
        self.cancelled = True
        return True


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
        url, payload, timeout, allow_redirects = self.session.calls[0]
        self.assertEqual(url, "https://www.appleemail.top/api/mail-all")
        self.assertEqual(timeout, 17)
        self.assertFalse(allow_redirects)
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
        response = FakeResponse(401, ValueError("not json"))
        client = self.client([response])
        with self.assertRaises(NineMallMailboxError) as caught:
            client.fetch_folder(account(), "INBOX")
        self.assertEqual(caught.exception.code, "http_401")
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("refresh-secret", str(caught.exception))
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(response.json_calls, 0)

    def test_403_is_non_retryable_before_json_decode(self):
        response = FakeResponse(403, ValueError("not json"))
        client = self.client([response])

        with self.assertRaises(NineMallMailboxError) as caught:
            client.fetch_folder(account(), "INBOX")

        self.assertEqual(caught.exception.code, "http_403")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(response.json_calls, 0)

    def test_307_and_308_redirects_are_never_followed_or_retried(self):
        for status in (307, 308):
            with self.subTest(status=status):
                response = FakeResponse(status, ValueError("not json"))
                client = self.client([response])
                with self.assertRaises(NineMallMailboxError) as caught:
                    client.fetch_folder(account(), "INBOX")
                self.assertEqual(caught.exception.code, "unexpected_http")
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(len(self.session.calls), 1)
                self.assertFalse(self.session.calls[0][3])
                self.assertEqual(response.json_calls, 0)

    def test_non_json_503_remains_retryable(self):
        responses = [
            FakeResponse(503, ValueError("not json")),
            FakeResponse(503, ValueError("not json")),
            FakeResponse(503, ValueError("not json")),
        ]
        client = self.client(responses)

        with self.assertRaises(NineMallMailboxError) as caught:
            client.fetch_folder(account(), "INBOX")

        self.assertEqual(caught.exception.code, "transient_http")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(self.session.calls), 3)
        self.assertEqual([response.json_calls for response in responses], [0, 0, 0])

    def test_chunked_transport_failure_retries_and_succeeds(self):
        client = self.client([
            requests.exceptions.ChunkedEncodingError(
                "truncated synthetic-refresh-token"
            ),
            FakeResponse(200, {"data": []}),
        ])

        self.assertEqual(client.fetch_folder(account(), "INBOX"), [])
        self.assertEqual(len(self.session.calls), 2)

    def test_content_decoding_failure_exhaustion_is_secret_safe_network_error(self):
        client = self.client([
            requests.exceptions.ContentDecodingError(
                "decode failed synthetic-refresh-token"
            )
            for _attempt in range(3)
        ])

        with self.assertRaises(NineMallMailboxError) as caught:
            client.fetch_folder(account(), "INBOX")

        self.assertEqual(caught.exception.code, "network_error")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("synthetic-refresh-token", str(caught.exception))
        self.assertEqual(len(self.session.calls), 3)

    def test_transport_failure_does_not_retry_after_deadline(self):
        self.clock = FakeClock()
        self.session = FakeSession(
            [requests.exceptions.ChunkedEncodingError("truncated")],
            clock=self.clock,
            elapsed=[4],
        )
        client = NineMallMailboxClient(
            base_url="https://www.appleemail.top",
            http_timeout=17,
            poll_interval=5,
            session=self.session,
            sleep=self.clock.sleep,
            clock=self.clock,
        )

        self.assertIsNone(client.poll_magic_link(account(), max_wait=5))
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.clock.value, 2_000_000_005.0)

    def test_every_unlisted_5xx_status_retries(self):
        responses = [
            FakeResponse(599, ValueError("not json")),
            FakeResponse(599, ValueError("not json")),
            FakeResponse(200, {"data": []}),
        ]
        client = self.client(responses)

        self.assertEqual(client.fetch_folder(account(), "INBOX"), [])
        self.assertEqual(len(self.session.calls), 3)
        self.assertEqual(responses[0].json_calls, 0)
        self.assertEqual(responses[1].json_calls, 0)

    def test_successful_2xx_invalid_json_is_invalid_json(self):
        client = self.client([FakeResponse(204, ValueError("not json"))])

        with self.assertRaises(NineMallMailboxError) as caught:
            client.fetch_folder(account(), "INBOX")

        self.assertEqual(caught.exception.code, "invalid_json")
        self.assertFalse(caught.exception.retryable)

    def test_polling_deadline_clamps_request_and_backoff_budget(self):
        self.clock = FakeClock()
        self.session = FakeSession(
            [FakeResponse(503, ValueError("not json"))],
            clock=self.clock,
            elapsed=[4],
        )
        client = NineMallMailboxClient(
            base_url="https://www.appleemail.top",
            http_timeout=17,
            poll_interval=5,
            session=self.session,
            sleep=self.clock.sleep,
            clock=self.clock,
        )

        self.assertIsNone(client.poll_magic_link(account(), max_wait=5))
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.session.calls[0][2], 5)
        self.assertEqual(self.session.calls[0][1]["mailbox"], "INBOX")
        self.assertEqual(self.clock.value, 2_000_000_005.0)

    def test_polling_stops_before_folder_switch_when_deadline_expires(self):
        self.clock = FakeClock()
        self.session = FakeSession(
            [FakeResponse(200, {"data": []})],
            clock=self.clock,
            elapsed=[3],
        )
        client = NineMallMailboxClient(
            base_url="https://www.appleemail.top",
            http_timeout=17,
            poll_interval=5,
            session=self.session,
            sleep=self.clock.sleep,
            clock=self.clock,
        )

        self.assertIsNone(client.poll_magic_link(account(), max_wait=3))
        self.assertEqual(
            [call[1]["mailbox"] for call in self.session.calls],
            ["INBOX"],
        )

    def test_cancellation_interrupts_backoff_before_retry_or_folder_switch(self):
        client = self.client([FakeResponse(503, ValueError("not json"))])
        cancelled = CancellingEvent(self.clock)

        self.assertIsNone(
            client.poll_magic_link(
                account(), max_wait=20, cancel_event=cancelled
            )
        )

        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.session.calls[0][1]["mailbox"], "INBOX")

    def test_positive_timeout_poll_interval_and_retry_count_are_required(self):
        for kwargs in (
            {"http_timeout": 0},
            {"poll_interval": 0},
            {"max_attempts": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                NineMallMailboxClient(
                    base_url="https://www.appleemail.top", **kwargs
                )

        client = self.client([])
        with self.assertRaises(ValueError):
            client.poll_magic_link(account(), max_wait=0)

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

    def test_only_exact_hosted_origin_is_accepted_before_posting_credentials(self):
        session = FakeSession([])
        invalid_bases = (
            "https://attacker.invalid",
            "https://www.appleemail.top.attacker.invalid",
            "https://www.appleemail.top:443",
            "https://user@www.appleemail.top",
            "https://www.appleemail.top/other-path",
            "https://www.appleemail.top?redirect=attacker.invalid",
            "https://www.appleemail.top#fragment",
            "https://[",
        )
        for base_url in invalid_bases:
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    NineMallMailboxClient(base_url=base_url, session=session)
        self.assertEqual(session.calls, [])

    def test_malformed_direct_link_is_skipped(self):
        messages = [NineMallMessage(
            "no-reply@claude.ai",
            "Claude login",
            "2033-05-18T03:33:25Z",
            "https://[",
        )]
        self.assertIsNone(extract_claude_magic_link(messages))

    def test_malformed_safelinks_target_is_skipped_while_scanning(self):
        malformed_target = (
            "https://nam01.safelinks.protection.outlook.com/"
            "?url=https%3A%2F%2F%5B"
        )
        messages = [NineMallMessage(
            "no-reply@claude.ai",
            "Claude login",
            "2033-05-18T03:33:25Z",
            malformed_target + " https://claude.ai/magic-link#valid-token",
        )]
        self.assertEqual(
            extract_claude_magic_link(messages),
            "https://claude.ai/magic-link#valid-token",
        )


if __name__ == "__main__":
    unittest.main()
