import unittest
import os
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


class TokenResponse:
    status_code = 200

    def json(self):
        return {"access_token": "access-token"}


class MessagesResponse:
    status_code = 200

    def json(self):
        return {"value": []}


class SuccessfulSession(FakeSession):
    def post(self, *_args, **_kwargs):
        return TokenResponse()

    def get(self, *_args, **_kwargs):
        return MessagesResponse()


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

    def test_ms_session_accepts_an_explicit_task_local_lease(self):
        fake = FakeSession()
        lease = make_lease()
        session = mailbox._ms_session(
            account_lease=lease, session_factory=lambda: fake
        )
        self.assertEqual(
            session.proxies,
            {
                "http": requests_proxy_url(lease),
                "https": requests_proxy_url(lease),
            },
        )

    def test_claude_magic_link_helper_delegates_to_lease_aware_mailbox(self):
        lease = make_lease()
        with patch(
            "common.mailbox.get_link_by_token",
            return_value="https://claude.ai/magic-link#abc",
        ) as get_link, patch.object(
            register.requests,
            "post",
            side_effect=AssertionError("legacy token request used"),
        ) as legacy_post:
            result = register.get_magic_link_by_token(
                "a@outlook.com",
                "refresh-token",
                client_id="cid",
                max_wait=60,
                account_lease=lease,
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
            account_lease=lease,
        )
        legacy_post.assert_not_called()

    def test_link_polling_propagates_explicit_lease_to_refresh_and_reads(self):
        lease = make_lease()
        message = {
            "subject": "Sign in",
            "from": "hello@anthropic.com",
            "body": "https://claude.ai/magic-link#abc",
            "received": "",
        }
        with patch.object(
            mailbox, "_get_access_token", return_value="access-token"
        ) as get_token, patch.object(
            mailbox, "fetch_messages", return_value=[message]
        ) as fetch:
            result = mailbox.get_link_by_token(
                "a@outlook.com",
                "refresh-token",
                link_regex=r"https://claude\.ai/magic-link#[A-Za-z]+",
                sender_contains=("anthropic",),
                account_lease=lease,
            )

        self.assertEqual(result, "https://claude.ai/magic-link#abc")
        get_token.assert_called_once_with(
            "refresh-token", mailbox.DEFAULT_CLIENT_ID, account_lease=lease
        )
        self.assertTrue(fetch.call_args_list)
        for call in fetch.call_args_list:
            self.assertIs(call.kwargs["account_lease"], lease)

    def test_token_refresh_uses_default_os_environ_lease(self):
        lease = make_lease()
        fake = SuccessfulSession()
        real_ms_session = mailbox._ms_session

        def session_from_default_env(**kwargs):
            return real_ms_session(session_factory=lambda: fake, **kwargs)

        with patch.dict(
            os.environ, account_proxy.lease_to_env(lease), clear=True
        ), patch.object(
            mailbox, "_ms_session", side_effect=session_from_default_env
        ):
            token = mailbox._get_access_token("refresh-token")

        self.assertEqual(token, "access-token")
        self.assertEqual(fake.proxies["https"], requests_proxy_url(lease))

    def test_mailbox_read_uses_default_os_environ_lease(self):
        lease = make_lease()
        fake = SuccessfulSession()
        real_ms_session = mailbox._ms_session

        def session_from_default_env(**kwargs):
            return real_ms_session(session_factory=lambda: fake, **kwargs)

        with patch.dict(
            os.environ, account_proxy.lease_to_env(lease), clear=True
        ), patch.object(
            mailbox, "_ms_session", side_effect=session_from_default_env
        ):
            messages = mailbox.fetch_messages("access-token", "inbox")

        self.assertEqual(messages, [])
        self.assertEqual(fake.proxies["https"], requests_proxy_url(lease))

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
