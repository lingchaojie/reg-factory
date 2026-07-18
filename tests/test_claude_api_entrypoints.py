import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import mailbox_broker
from common import claude_platform_mailbox, mailbox


class _Response:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, payload, capture):
        self.payload = payload
        self.capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def post(self, url, json):
        self.capture.update(url=url, json=json)
        return _Response(self.payload)


class ClaudeApiMailboxEntrypointTests(unittest.TestCase):
    def test_platform_broker_client_returns_structured_dual_artifact(self):
        capture = {}
        payload = {
            "ok": True,
            "value": {
                "magic_link": "https://platform.claude.com/magic-link?code=broker-secret",
                "code": "482731",
                "received_at": 2_000_000_001.0,
            },
        }

        with patch.dict("os.environ", {"MAILBOX_BROKER": "http://broker.test/"}, clear=True), patch.object(
            claude_platform_mailbox.aiohttp,
            "ClientSession",
            side_effect=lambda **_kwargs: _Session(payload, capture),
        ):
            result = asyncio.run(
                claude_platform_mailbox.fetch_claude_platform_from_broker(
                    "person@example.com", "mail-pass", max_wait=30
                )
            )

        self.assertEqual(result.code, "482731")
        self.assertEqual(result.magic_link, payload["value"]["magic_link"])
        self.assertEqual(result.received_at, 2_000_000_001.0)
        self.assertEqual(capture["url"], "http://broker.test/fetch")
        self.assertEqual(
            capture["json"],
            {
                "email": "person@example.com",
                "password": "mail-pass",
                "sender_hint": ["anthropic", "claude"],
                "subject_hint": ["code", "verify", "sign in", "login"],
                "regex": "",
                "kind": "claude_platform",
                "timeout": 30,
            },
        )

    def test_generic_broker_transport_preserves_structured_values_without_logging_them(self):
        capture = {}
        value = {
            "magic_link": "https://platform.claude.com/magic-link?code=broker-secret",
            "code": "482731",
            "received_at": 2_000_000_001.0,
        }
        with patch.dict("os.environ", {"MAILBOX_BROKER": "http://broker.test"}, clear=True), patch(
            "aiohttp.ClientSession",
            side_effect=lambda **_kwargs: _Session({"ok": True, "value": value}, capture),
        ), patch("builtins.print") as output:
            result = asyncio.run(
                mailbox.fetch_from_broker(
                    "person@example.com",
                    "mail-pass",
                    ("anthropic",),
                    ("code",),
                    "",
                    "claude_platform",
                    30,
                )
            )

        self.assertEqual(result, value)
        rendered = " ".join(str(item) for item in output.call_args_list)
        self.assertNotIn("482731", rendered)
        self.assertNotIn("broker-secret", rendered)

    def test_existing_string_broker_transport_contract_is_unchanged(self):
        capture = {}
        with patch.dict("os.environ", {"MAILBOX_BROKER": "http://broker.test"}, clear=True), patch(
            "aiohttp.ClientSession",
            side_effect=lambda **_kwargs: _Session({"ok": True, "value": "482731"}, capture),
        ), patch("builtins.print"):
            result = asyncio.run(
                mailbox.fetch_from_broker(
                    "person@example.com",
                    "mail-pass",
                    ("openai",),
                    ("code",),
                    r"\b(\d{6})\b",
                    "code",
                    30,
                )
            )

        self.assertEqual(result, "482731")


class BrokerPlatformTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.lock = asyncio.Lock()
        self.session.page = Mock()
        self.session.last_used = 0.0
        self.session.seen = set()
        self.session.just_created = True

    def test_broker_platform_kind_returns_structured_artifact(self):
        broker = mailbox_broker.Broker()
        broker.ensure_session = AsyncMock(return_value=self.session)
        broker._count_matching = AsyncMock(return_value=0)
        broker._scan_platform_artifact = AsyncMock(
            return_value={
                "magic_link": "",
                "code": "482731",
                "received_at": 2_000_000_001.0,
            }
        )

        with patch.object(mailbox_broker, "_click_folder", new=AsyncMock()), patch.object(
            mailbox_broker.asyncio, "sleep", new=AsyncMock()
        ), patch("builtins.print") as output:
            result = asyncio.run(
                broker.fetch(
                    "person@example.com",
                    "mail-pass",
                    ("anthropic", "claude"),
                    ("code", "sign in", "login"),
                    "",
                    "claude_platform",
                    30,
                )
            )

        self.assertEqual(result["code"], "482731")
        self.assertIn(("", "482731", 2_000_000_001.0), self.session.seen)
        rendered = " ".join(str(item) for item in output.call_args_list)
        self.assertNotIn("person@example.com", rendered)
        self.assertNotIn("482731", rendered)
        self.assertIn("pe***@example.com", rendered)

    def test_broker_new_message_path_preserves_both_artifacts(self):
        self.session.just_created = False
        broker = mailbox_broker.Broker()
        broker.ensure_session = AsyncMock(return_value=self.session)
        broker._count_matching = AsyncMock(side_effect=(0, 0, 1))
        broker._scan_platform_artifact = AsyncMock(
            return_value={
                "magic_link": "https://platform.claude.com/magic-link?code=new-secret",
                "code": "482731",
                "received_at": 2_000_000_001.0,
            }
        )

        with patch.object(mailbox_broker, "_click_folder", new=AsyncMock()), patch.object(
            mailbox_broker.asyncio, "sleep", new=AsyncMock()
        ), patch("builtins.print"):
            result = asyncio.run(
                broker.fetch(
                    "person@example.com",
                    "mail-pass",
                    ("anthropic", "claude"),
                    ("code", "sign in", "login"),
                    "",
                    "claude_platform",
                    30,
                )
            )

        self.assertEqual(result["code"], "482731")
        self.assertIn("new-secret", result["magic_link"])

    def test_session_initialization_masks_mailbox_address_on_failure(self):
        broker = mailbox_broker.Broker()
        with patch.object(mailbox_broker, "create_browser_with_retry", return_value=None), patch(
            "builtins.print"
        ) as output:
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    broker.ensure_session("person@example.com", "mail-pass")
                )

        rendered = " ".join(str(item) for item in output.call_args_list)
        self.assertNotIn("person@example.com", rendered)
        self.assertNotIn("mail-pass", rendered)
        self.assertIn("pe***@example.com", rendered)


if __name__ == "__main__":
    unittest.main()
