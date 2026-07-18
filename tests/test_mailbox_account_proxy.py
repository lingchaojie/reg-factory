import unittest
from unittest.mock import patch

import requests

import register
from common import account_proxy, mailbox
from common.ipmart_proxy import ProxyLease, requests_proxy_url


class FakeSession:
    def __init__(self):
        self.trust_env = True
        self.proxies = {}


class RaisingSession:
    def post(self, *_args, **_kwargs):
        raise requests.exceptions.ProxyError(
            "proxy request failed via http://user:proxy-secret@gateway.example:8080"
        )

    def get(self, *_args, **_kwargs):
        raise requests.exceptions.ProxyError(
            "proxy request failed via http://user:proxy-secret@gateway.example:8080"
        )


class GenericRaisingSession:
    def post(self, *_args, **_kwargs):
        raise RuntimeError("user proxy-secret")

    def get(self, *_args, **_kwargs):
        raise RuntimeError("user proxy-secret")


class ParseErrorResponse:
    status_code = 200

    def json(self):
        raise ValueError("user proxy-secret")


class ParseErrorSession:
    def get(self, *_args, **_kwargs):
        return ParseErrorResponse()


def make_lease():
    return ProxyLease(
        "http",
        "gateway.example",
        8080,
        "account-res-US-sid-00000042",
        "proxy-secret",
        "00000042",
        "203.0.113.8",
    )


class MailboxAccountProxyTests(unittest.TestCase):
    def test_ms_session_uses_inherited_account_proxy(self):
        fake = FakeSession()
        lease = make_lease()
        session = mailbox._ms_session(
            account_proxy.lease_to_env(lease), session_factory=lambda: fake
        )
        self.assertFalse(session.trust_env)
        self.assertEqual(
            session.proxies,
            {
                "http": requests_proxy_url(lease),
                "https": requests_proxy_url(lease),
            },
        )

    def test_ms_session_remains_direct_without_account_lease(self):
        fake = FakeSession()
        session = mailbox._ms_session({}, session_factory=lambda: fake)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {"http": None, "https": None})

    def test_claude_magic_link_helper_delegates_to_lease_aware_mailbox(self):
        with patch(
            "common.mailbox.get_link_by_token",
            return_value="https://claude.ai/magic-link#abc",
        ) as get_link, patch.object(
            register.requests,
            "post",
            side_effect=AssertionError("legacy token request used"),
        ) as legacy_post:
            result = register.get_magic_link_by_token(
                "a@outlook.com", "refresh-token", client_id="cid", max_wait=60
            )
        self.assertEqual(result, "https://claude.ai/magic-link#abc")
        get_link.assert_called_once_with(
            "a@outlook.com",
            "refresh-token",
            client_id="cid",
            link_regex=r"https://claude\.ai/magic-link#[A-Za-z0-9_\-:=+/]+",
            sender_contains=("anthropic", "claude"),
            subject_contains=("magic", "verify", "sign in", "login"),
            must_contain="claude.ai/magic-link#",
            max_wait=60,
            poll=5,
        )
        legacy_post.assert_not_called()

    def test_graph_network_errors_do_not_print_proxy_credentials(self):
        with patch.object(
            mailbox, "_ms_session", return_value=RaisingSession()
        ), patch.object(mailbox.time, "sleep"), patch("builtins.print") as printer:
            result = mailbox._get_access_token("refresh-token")
        self.assertIsNone(result)
        self._assert_credentials_not_printed(printer)

    def test_fetch_network_errors_do_not_print_proxy_credentials(self):
        with patch.object(
            mailbox, "_ms_session", return_value=RaisingSession()
        ), patch.object(mailbox.time, "sleep"), patch("builtins.print") as printer:
            result = mailbox.fetch_messages("access-token", "inbox")
        self.assertEqual(result, [])
        self._assert_credentials_not_printed(printer)

    def test_generic_graph_errors_do_not_print_proxy_credentials(self):
        with patch.object(
            mailbox, "_ms_session", return_value=GenericRaisingSession()
        ), patch("builtins.print") as printer:
            token = mailbox._get_access_token("refresh-token")
            messages = mailbox.fetch_messages("access-token", "inbox")
        self.assertIsNone(token)
        self.assertEqual(messages, [])
        self._assert_credentials_not_printed(printer)

    def test_graph_parse_errors_do_not_print_proxy_credentials(self):
        with patch.object(
            mailbox, "_ms_session", return_value=ParseErrorSession()
        ), patch("builtins.print") as printer:
            result = mailbox.fetch_messages("access-token", "inbox")
        self.assertEqual(result, [])
        self._assert_credentials_not_printed(printer)

    def _assert_credentials_not_printed(self, printer):
        rendered = " ".join(str(call) for call in printer.call_args_list)
        self.assertNotIn("user", rendered)
        self.assertNotIn("proxy-secret", rendered)


if __name__ == "__main__":
    unittest.main()
