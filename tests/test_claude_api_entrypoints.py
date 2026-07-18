import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import mailbox_broker
from common import claude_platform_mailbox, mailbox


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, payload, capture, status=200, **kwargs):
        self.payload = payload
        self.capture = capture
        self.status = status
        self.capture["session_kwargs"] = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def post(self, url, json):
        self.capture.update(url=url, json=json)
        return _Response(self.payload, self.status)


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
            side_effect=lambda **kwargs: _Session(payload, capture, **kwargs),
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
        self.assertEqual(capture["session_kwargs"]["timeout"].total, 90)
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

    def test_platform_broker_rejects_http_error_and_false_envelope(self):
        cases = (
            (503, {"ok": True, "value": {"code": "482731", "received_at": 1}}),
            (200, {"ok": False, "value": {"code": "482731", "received_at": 1}}),
        )
        for status, payload in cases:
            with self.subTest(status=status, ok=payload["ok"]):
                capture = {}
                with patch.dict(
                    "os.environ", {"MAILBOX_BROKER": "http://broker.test"}, clear=True
                ), patch.object(
                    claude_platform_mailbox.aiohttp,
                    "ClientSession",
                    side_effect=lambda **kwargs: _Session(
                        payload, capture, status=status, **kwargs
                    ),
                ):
                    result = asyncio.run(
                        claude_platform_mailbox.fetch_claude_platform_from_broker(
                            "person@example.com", "mail-pass", max_wait=30
                        )
                    )
                self.assertIsNone(result)

    def test_platform_broker_rejects_invalid_received_at_without_leaking_it(self):
        capture = {}
        raw = "invalid-received raw-secret"
        payload = {"ok": True, "value": {"code": "482731", "received_at": raw}}
        with patch.dict(
            "os.environ", {"MAILBOX_BROKER": "http://broker.test"}, clear=True
        ), patch.object(
            claude_platform_mailbox.aiohttp,
            "ClientSession",
            side_effect=lambda **kwargs: _Session(payload, capture, **kwargs),
        ), patch("builtins.print") as output:
            result = asyncio.run(
                claude_platform_mailbox.fetch_claude_platform_from_broker(
                    "person@example.com", "mail-pass", max_wait=30
                )
            )

        self.assertIsNone(result)
        rendered = " ".join(str(item) for item in output.call_args_list)
        self.assertNotIn(raw, rendered)

    def test_platform_broker_rejects_non_finite_received_at(self):
        for raw in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(raw=raw):
                capture = {}
                payload = {
                    "ok": True,
                    "value": {"code": "482731", "received_at": raw},
                }
                with patch.dict(
                    "os.environ", {"MAILBOX_BROKER": "http://broker.test"}, clear=True
                ), patch.object(
                    claude_platform_mailbox.aiohttp,
                    "ClientSession",
                    side_effect=lambda **kwargs: _Session(payload, capture, **kwargs),
                ), patch("builtins.print") as output:
                    result = asyncio.run(
                        claude_platform_mailbox.fetch_claude_platform_from_broker(
                            "person@example.com", "mail-pass", max_wait=30
                        )
                    )

                self.assertIsNone(result)
                rendered = " ".join(str(item) for item in output.call_args_list)
                self.assertNotIn(raw, rendered)


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
        self.session.page.evaluate = AsyncMock(
            side_effect=(
                [{
                    "index": 0,
                    "visible": True,
                    "received": "2033-05-18T03:33:21Z",
                    "stable_id": "message-a",
                }],
                True,
                {"subject": "Claude login code 482731", "body": "Sign in"},
            )
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

    def test_broker_real_scanner_deduplicates_same_browser_message(self):
        broker = mailbox_broker.Broker()
        broker.ensure_session = AsyncMock(return_value=self.session)
        broker._count_matching = AsyncMock(return_value=0)

        async def evaluate(script, *args):
            if "getBoundingClientRect" in script:
                return [{
                    "index": 0,
                    "visible": True,
                    "received": "2033-05-18T03:33:21Z",
                    "stable_id": "message-a",
                }]
            if args:
                return True
            return {"subject": "Claude login code 482731", "body": "Sign in"}

        self.session.page.evaluate = AsyncMock(side_effect=evaluate)
        kwargs = (
            "person@example.com",
            "mail-pass",
            ("anthropic", "claude"),
            ("code", "sign in", "login"),
            "",
            "claude_platform",
            0,
        )
        with patch.object(mailbox_broker, "_click_folder", new=AsyncMock()), patch.object(
            mailbox_broker.asyncio, "sleep", new=AsyncMock()
        ), patch("builtins.print"):
            first = asyncio.run(broker.fetch(*kwargs))
            second = asyncio.run(broker.fetch(*kwargs))

        self.assertEqual(first["code"], "482731")
        self.assertEqual(first["received_at"], 2_000_000_001.0)
        self.assertIsNone(second)
        self.assertEqual(
            self.session.seen,
            {("", "482731", 2_000_000_001.0)},
        )

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
