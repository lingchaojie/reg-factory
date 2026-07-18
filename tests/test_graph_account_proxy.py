import unittest
from unittest.mock import patch

import extract_graph_tokens


class FakeSession:
    def __init__(self):
        self.trust_env = True
        self.proxies = {}
        self.headers = {}


class GraphAccountProxyTests(unittest.TestCase):
    def test_oauth_session_uses_explicit_account_proxy(self):
        fake = FakeSession()

        session = extract_graph_tokens._oauth_session(
            "http://user:pass@gateway.example:8080",
            session_factory=lambda: fake,
        )

        self.assertIs(session, fake)
        self.assertFalse(session.trust_env)
        self.assertEqual(
            session.proxies,
            {
                "http": "http://user:pass@gateway.example:8080",
                "https": "http://user:pass@gateway.example:8080",
            },
        )

    def test_oauth_session_preserves_legacy_environment_mode_without_proxy(self):
        fake = FakeSession()

        session = extract_graph_tokens._oauth_session(
            session_factory=lambda: fake,
        )

        self.assertTrue(session.trust_env)
        self.assertEqual(session.proxies, {})

    def test_oauth_exception_output_does_not_reveal_proxy_credentials(self):
        class RaisingSession:
            def get(self, *_args, **_kwargs):
                raise RuntimeError(
                    "connect failed via "
                    "http://user:proxy-secret@gateway.example:8080"
                )

        with patch.object(
            extract_graph_tokens,
            "_oauth_session",
            return_value=RaisingSession(),
        ), patch("builtins.print") as printer:
            result = extract_graph_tokens.get_graph_token(
                "a@outlook.com",
                "Pass1!",
                proxy_url="http://user:proxy-secret@gateway.example:8080",
            )

        self.assertIsNone(result)
        rendered = " ".join(str(call) for call in printer.call_args_list)
        self.assertNotIn("user", rendered)
        self.assertNotIn("proxy-secret", rendered)


if __name__ == "__main__":
    unittest.main()
