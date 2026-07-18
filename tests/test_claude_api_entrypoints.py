import argparse
import asyncio
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import mailbox_broker
import register_three_platforms
import run_full_flow
from common import claude_platform_mailbox, mailbox
from common.claude_email_accounts import (
    ClaudeEmailAccountStore,
    reserve_shared_claude_account,
)
from webui import scripts, server


def platform_args(platforms):
    return argparse.Namespace(
        platforms=platforms,
        timeout=600,
        node="auto",
        keep_on_fail=False,
        import_c2a=False,
        codex=False,
        codex_group=None,
        codex_manual_phone=False,
        grok_sub2api=False,
        grok_sub2api_group=None,
    )


def account_tuple(account):
    return (
        account.email,
        account.password,
        account.refresh_token,
        account.client_id,
    )


class RecordingStore:
    def __init__(self, purpose, delegate, events):
        self.purpose = purpose
        self.delegate = delegate
        self.events = events
        self.calls = 0

    def release(self, account):
        self.calls += 1
        self.events.append(f"release:{self.purpose}")
        return self.delegate.release(account)


class ParallelAbort(BaseException):
    pass


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
                    "person@example.com",
                    "mail-pass",
                    max_wait=30,
                    received_after=2_000_000_000.0,
                )
            )

        self.assertEqual(result.code, "482731")
        self.assertEqual(result.magic_link, payload["value"]["magic_link"])
        self.assertEqual(result.received_at, 2_000_000_001.0)
        self.assertEqual(capture["url"], "http://broker.test/fetch")
        self.assertEqual(capture["session_kwargs"]["timeout"].total, 30)
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
                "received_after": 2_000_000_000.0,
            },
        )

    def test_platform_broker_client_rejects_stale_and_raw_invalid_artifacts(self):
        cases = (
            {
                "magic_link": "https://platform.claude.com.evil.test/magic-link?code=x",
                "code": "",
                "received_at": 2_000_000_001.0,
            },
            {
                "magic_link": "",
                "code": "482731 trailing-junk",
                "received_at": 2_000_000_001.0,
            },
            {
                "magic_link": "",
                "code": "482731",
                "received_at": 1_999_999_000.0,
            },
        )
        for value in cases:
            with self.subTest(value=value):
                capture = {}
                with patch.dict(
                    "os.environ",
                    {"MAILBOX_BROKER": "http://broker.test"},
                    clear=True,
                ), patch.object(
                    claude_platform_mailbox.aiohttp,
                    "ClientSession",
                    side_effect=lambda **kwargs: _Session(
                        {"ok": True, "value": value},
                        capture,
                        **kwargs,
                    ),
                ):
                    result = asyncio.run(
                        claude_platform_mailbox.fetch_claude_platform_from_broker(
                            "person@example.com",
                            "mail-pass",
                            max_wait=30,
                            received_after=2_000_000_000.0,
                        )
                    )

                self.assertIsNone(result)

    def test_platform_broker_transport_and_json_failures_are_sanitized_misses(self):
        class JsonFailureResponse(_Response):
            async def json(self):
                raise ValueError("json-secret")

        class JsonFailureSession(_Session):
            def post(self, url, json):
                self.capture.update(url=url, json=json)
                return JsonFailureResponse({}, self.status)

        factories = (
            lambda _capture: (_ for _ in ()).throw(
                RuntimeError("connection-secret")
            ),
            lambda _capture: (_ for _ in ()).throw(
                asyncio.TimeoutError("timeout-secret")
            ),
            lambda capture: JsonFailureSession({}, capture),
        )
        for factory in factories:
            with self.subTest(factory=factory):
                capture = {}
                with patch.dict(
                    "os.environ",
                    {"MAILBOX_BROKER": "http://broker.test"},
                    clear=True,
                ), patch.object(
                    claude_platform_mailbox.aiohttp,
                    "ClientSession",
                    side_effect=lambda **kwargs: factory(capture),
                ):
                    result = asyncio.run(
                        claude_platform_mailbox.fetch_claude_platform_from_broker(
                            "person@example.com",
                            "mail-pass",
                            max_wait=0.1,
                            received_after=2_000_000_000.0,
                        )
                    )

                self.assertIsNone(result)

    def test_platform_broker_client_preserves_cancellation(self):
        with patch.dict(
            "os.environ",
            {"MAILBOX_BROKER": "http://broker.test"},
            clear=True,
        ), patch.object(
            claude_platform_mailbox.aiohttp,
            "ClientSession",
            side_effect=asyncio.CancelledError,
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(
                    claude_platform_mailbox.fetch_claude_platform_from_broker(
                        "person@example.com",
                        "mail-pass",
                        max_wait=0.1,
                        received_after=2_000_000_000.0,
                    )
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
        self.session.platform_seen = set()
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
                {
                    "stable_id": "message-a",
                    "received": "2033-05-18T03:33:21Z",
                },
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
                    2_000_000_000.0,
                )
            )

        self.assertEqual(result["code"], "482731")
        self.assertIn("message-a", self.session.platform_seen)
        rendered = " ".join(str(item) for item in output.call_args_list)
        self.assertNotIn("person@example.com", rendered)
        self.assertNotIn("482731", rendered)
        self.assertIn("pe***@example.com", rendered)

    def test_accessible_minute_precision_survives_broker_round_trip(self):
        accessible = "Sunday, July 19, 2033 at 3:33 AM"
        minute_start = claude_platform_mailbox._received_epoch(accessible)
        requested_at = minute_start + 25
        broker = mailbox_broker.Broker()
        broker.ensure_session = AsyncMock(return_value=self.session)
        self.session.page.evaluate = AsyncMock(side_effect=(
            [{
                "index": 0,
                "visible": True,
                "received": accessible,
                "stable_id": "message-accessible-minute",
            }],
            {
                "stable_id": "message-accessible-minute",
                "received": accessible,
            },
            {"subject": "Claude login code 482731", "body": "Sign in"},
        ))

        with patch.object(
            mailbox_broker, "_click_folder", new=AsyncMock()
        ), patch.object(
            mailbox_broker.asyncio, "sleep", new=AsyncMock()
        ), patch("builtins.print"):
            wire_value = asyncio.run(
                broker.fetch(
                    "person@example.com",
                    "mail-pass",
                    ("anthropic", "claude"),
                    ("code", "sign in", "login"),
                    "",
                    "claude_platform",
                    0,
                    requested_at,
                )
            )

        capture = {}
        with patch.dict(
            "os.environ",
            {"MAILBOX_BROKER": "http://broker.test"},
            clear=True,
        ), patch.object(
            claude_platform_mailbox.aiohttp,
            "ClientSession",
            side_effect=lambda **kwargs: _Session(
                {"ok": True, "value": wire_value}, capture, **kwargs
            ),
        ):
            result = asyncio.run(
                claude_platform_mailbox.fetch_claude_platform_from_broker(
                    "person@example.com",
                    "mail-pass",
                    max_wait=30,
                    received_after=requested_at,
                )
            )

        self.assertEqual(result.code, "482731")
        self.assertGreaterEqual(result.received_at, requested_at - 5)

    def test_broker_real_scanner_deduplicates_same_browser_message(self):
        broker = mailbox_broker.Broker()
        broker.ensure_session = AsyncMock(return_value=self.session)
        broker._count_matching = AsyncMock(return_value=0)

        async def evaluate(script, *args):
            if "return items.map" in script:
                return [{
                    "index": 0,
                    "visible": True,
                    "received": "2033-05-18T03:33:21Z",
                    "stable_id": "message-a",
                }]
            if args:
                return {
                    "stable_id": "message-a",
                    "received": "2033-05-18T03:33:21Z",
                }
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
            2_000_000_000.0,
        )
        with patch.object(mailbox_broker, "_click_folder", new=AsyncMock()), patch.object(
            mailbox_broker.asyncio, "sleep", new=AsyncMock()
        ), patch("builtins.print"):
            first = asyncio.run(broker.fetch(*kwargs))
            second = asyncio.run(broker.fetch(*kwargs))

        self.assertEqual(first["code"], "482731")
        self.assertEqual(first["received_at"], 2_000_000_001.0)
        self.assertIsNone(second)
        self.assertEqual(self.session.platform_seen, {"message-a"})

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
                    2_000_000_000.0,
                )
            )

        self.assertEqual(result["code"], "482731")
        self.assertIn("new-secret", result["magic_link"])

    def test_existing_session_does_not_absorb_current_platform_mail_into_baseline(self):
        self.session.just_created = False
        broker = mailbox_broker.Broker()
        broker.ensure_session = AsyncMock(return_value=self.session)
        broker._count_matching = AsyncMock(return_value=1)
        broker._scan_platform_artifact = AsyncMock(
            return_value={
                "magic_link": "",
                "code": "482731",
                "received_at": 2_000_000_001.0,
            }
        )

        with patch.object(
            mailbox_broker, "_click_folder", new=AsyncMock()
        ), patch.object(
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
                    0,
                    2_000_000_000.0,
                )
            )

        self.assertEqual(result["code"], "482731")
        broker._scan_platform_artifact.assert_awaited()

    def test_new_session_does_not_return_old_platform_mail(self):
        broker = mailbox_broker.Broker()
        broker.ensure_session = AsyncMock(return_value=self.session)
        self.session.page.evaluate = AsyncMock(side_effect=(
            [{
                "index": 0,
                "visible": True,
                "received": "2020-01-01T00:00:00Z",
                "stable_id": "old-inbox-row",
            }],
            [{
                "index": 0,
                "visible": True,
                "received": "2020-01-01T00:00:00Z",
                "stable_id": "old-junk-row",
            }],
        ))

        with patch.object(
            mailbox_broker, "_click_folder", new=AsyncMock()
        ), patch.object(
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
                    0,
                    2_000_000_000.0,
                )
            )

        self.assertIsNone(result)
        self.assertEqual(self.session.platform_seen, set())

    def test_http_handler_forwards_platform_received_after_to_broker(self):
        broker = Mock()
        broker.fetch = AsyncMock(
            return_value={
                "magic_link": "",
                "code": "482731",
                "received_at": 2_000_000_001.0,
            }
        )
        request = Mock()
        request.app = {"broker": broker}
        request.json = AsyncMock(return_value={
            "email": "person@example.com",
            "password": "mail-pass",
            "sender_hint": ["anthropic", "claude"],
            "subject_hint": ["code", "login"],
            "regex": "",
            "kind": "claude_platform",
            "timeout": 0.3,
            "received_after": 2_000_000_000.0,
        })

        response = asyncio.run(mailbox_broker.h_fetch(request))

        self.assertEqual(response.status, 200)
        broker.fetch.assert_awaited_once_with(
            "person@example.com",
            "mail-pass",
            ["anthropic", "claude"],
            ["code", "login"],
            r"\b(\d{6})\b",
            "claude_platform",
            0.3,
            2_000_000_000.0,
        )

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


class ClaudeAPIEntrypointTests(unittest.TestCase):
    def test_webui_exposes_standalone_claude_api_registration(self):
        item = scripts.script_by_id("register_claude_api")
        self.assertIsNotNone(item)
        self.assertEqual(item["file"], "register_claude_api.py")
        secret_flags = {
            spec["flag"] for spec in item["args"] if spec.get("secret")
        }
        self.assertTrue(
            {"--password", "--token", "--client-id"} <= secret_flags
        )

    def test_orchestrator_platform_choices_include_claude_api(self):
        for script_id in ("run_full_flow", "register_three_platforms"):
            item = scripts.script_by_id(script_id)
            platforms = next(
                spec for spec in item["args"] if spec["flag"] == "--platforms"
            )
            self.assertIn("claude_api", platforms["choices"])

    def test_webui_orchestrator_copy_describes_four_choices_and_prelaunch_routing(self):
        for script_id in ("run_full_flow", "register_three_platforms"):
            item = scripts.script_by_id(script_id)
            copy = f'{item["title"]} {item["desc"]}'
            self.assertIn("四平台", item["title"])
            self.assertNotIn("三平台", copy)
            self.assertIn("NINEMALL", item["desc"])
            self.assertIn("OUTLOOK", item["desc"])
            self.assertIn("启动前", item["desc"])

    def test_webui_claude_api_preview_redacts_mailbox_secrets(self):
        item = scripts.script_by_id("register_claude_api")
        values = {
            "--email": "person@example.com",
            "--password": "mail-pass",
            "--token": "refresh-secret",
            "--client-id": "client-guid",
        }
        command = server._build_cmd(item, values)
        preview = " ".join(server._redact_cmd(item, command))
        for secret in ("mail-pass", "refresh-secret", "client-guid"):
            self.assertNotIn(secret, preview)
        self.assertEqual(preview.count("***"), 3)

    def test_claude_api_documentation_contract(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")

        for command in (
            "python register_claude_api.py --count 1",
            "python run_full_flow.py --platforms claude_api",
            "python register_three_platforms.py --from-pool --platforms claude claude_api",
        ):
            self.assertIn(command, readme)
        for state_file in (
            "mail_used_claude_api.txt",
            "mail_error_claude_api.txt",
            "emails_used_claude_api.txt",
            "emails_error_claude_api.txt",
        ):
            self.assertIn(state_file, readme)
            self.assertIn(state_file, gitignore)
        for term in (
            "cookies/claude_api/",
            "个人账户",
            "组织",
            "API Key",
            "充值",
        ):
            self.assertIn(term, readme)
            self.assertIn(term, changelog)

    def test_documentation_distinguishes_preselected_outlook_from_fallback(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        for document in (readme, changelog):
            self.assertIn("Claude 家族专用流程在启动前选择 NINEMALL", document)
            self.assertIn("混合流程在启动前直接选择 OUTLOOK", document)
            self.assertIn("不会先尝试 NINEMALL", document)
            self.assertIn("不是 NINEMALL 失败后的回退", document)

    def test_readme_default_run_and_outlook_examples_are_provider_accurate(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn("默认 `EMAIL_PROVIDER=NINEMALL`", readme)
        self.assertIn("跳过 Outlook Stage A", readme)
        self.assertIn("严格使用 AppleEmail", readme)
        self.assertIn("显式设置 `EMAIL_PROVIDER=OUTLOOK`", readme)
        self.assertNotIn(
            "python run_full_flow.py                       # 注册 1 个 outlook 号后在 claude 上注册",
            readme,
        )
        self.assertNotIn("这项迁移只覆盖默认 Outlook → Claude 边界", readme)

    def test_readme_describes_tri_register_logs_as_multiplatform(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "| `tri_register_logs/` | 多平台注册日志（四种选择） |",
            readme,
        )
        self.assertNotIn("| `tri_register_logs/` | 三平台注册日志 |", readme)

    def test_claude_api_command_forwards_mailbox_credentials(self):
        command = register_three_platforms.build_command(
            "claude_api",
            platform_args(["claude_api"]),
            (
                "person@example.com",
                "mail-pass",
                "refresh-secret",
                "client-guid",
            ),
        )

        self.assertEqual(command[2], "register_claude_api.py")
        self.assertEqual(
            command[command.index("--token") + 1], "refresh-secret"
        )
        self.assertEqual(
            command[command.index("--client-id") + 1], "client-guid"
        )

    def test_claude_family_only_predicate_accepts_both_claude_choices(self):
        self.assertTrue(
            run_full_flow.is_ninemail_claude_family_only(
                argparse.Namespace(platforms=["claude_api"]),
                {"EMAIL_PROVIDER": "NINEMALL"},
            )
        )
        self.assertTrue(
            run_full_flow.is_ninemail_claude_family_only(
                argparse.Namespace(platforms=["claude", "claude_api"]),
                {"EMAIL_PROVIDER": "NINEMALL"},
            )
        )
        self.assertFalse(
            run_full_flow.is_ninemail_claude_family_only(
                argparse.Namespace(platforms=["claude_api", "chatgpt"]),
                {"EMAIL_PROVIDER": "NINEMALL"},
            )
        )

    def test_full_flow_dry_run_does_not_reserve_ninemail_account(self):
        args = argparse.Namespace(
            platforms=["claude", "claude_api"], dry_run=True
        )
        expected = ("dry-run@outlook.com", "DryRunPass1!", "", "")
        stage = Mock(return_value=expected)

        with patch.object(
            run_full_flow,
            "reserve_shared_claude_account",
            side_effect=AssertionError("dry-run reserved a mailbox"),
            create=True,
        ):
            selected = run_full_flow.acquire_stage_account(
                args,
                {"EMAIL_PROVIDER": "NINEMALL"},
                stage_email_fn=stage,
            )

        self.assertEqual(selected, expected)
        stage.assert_called_once_with(args, {"EMAIL_PROVIDER": "NINEMALL"})


class SharedClaudeReservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "mail.txt"
        self.source.write_text(
            "person@example.com----mail-pass----client-guid----refresh-secret\n",
            encoding="utf-8",
        )

    def args(self, platforms=None):
        values = platform_args(platforms or ["claude", "claude_api"])
        values.parallel = False
        values.broker = ""
        values.grok_timeout = 40
        return values

    def reserve(self):
        result = reserve_shared_claude_account(
            "NINEMALL",
            ("claude", "claude_api"),
            source_file=self.source,
            root_dir=self.root,
        )
        self.assertIsNotNone(result)
        return result

    def ledger(self, purpose):
        return (
            self.root / f"mail_used_{purpose}.txt"
        ).read_text(encoding="utf-8").splitlines()

    def child_store(self, purpose):
        return ClaudeEmailAccountStore(
            provider="NINEMALL",
            purpose=purpose,
            source_file=self.source,
            root_dir=self.root,
        )

    def recorded_reserved(self, events):
        account, stores = self.reserve()
        recorded = {
            purpose: RecordingStore(purpose, store, events)
            for purpose, store in stores.items()
        }
        reserved = register_three_platforms._ReservedPoolAccount(
            account, recorded
        )
        return account, recorded, reserved

    def parallel_args(self):
        args = self.args()
        args.parallel = True
        return args

    async def drain_tasks(self, tasks):
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_release_stays_retryable_until_every_process_is_confirmed(self):
        events = []
        account, stores, reserved = self.recorded_reserved(events)
        claude_process = object()
        api_process = object()
        reserved.track_process(claude_process)
        reserved.track_process(api_process)

        self.assertFalse(reserved.release())
        self.assertTrue(reserved.active)
        self.assertEqual(events, [])

        reserved.confirm_process_stopped(claude_process, True)
        reserved.confirm_process_stopped(api_process, False)
        self.assertFalse(reserved.release())
        self.assertTrue(reserved.active)
        self.assertEqual(events, [])

        reserved.confirm_process_stopped(api_process, True)
        self.assertTrue(reserved.release())
        self.assertFalse(reserved.active)
        self.assertFalse(reserved.release())
        self.assertEqual(
            events, ["release:claude", "release:claude_api"]
        )
        self.assertEqual(
            {purpose: store.calls for purpose, store in stores.items()},
            {"claude": 1, "claude_api": 1},
        )
        for purpose in ("claude", "claude_api"):
            self.assertEqual(
                self.ledger(purpose),
                [
                    "person@example.com----reserved",
                    "person@example.com----released",
                ],
            )

    async def test_parallel_launch_failure_cleans_running_sibling_before_release(self):
        events = []
        _account, stores, reserved = self.recorded_reserved(events)
        sibling_started = asyncio.Event()
        sibling_finished = asyncio.Event()
        child_tasks = []
        running = set()

        async def fake_run(
            platform, _command, _run_id, _child_env, process_owner=None
        ):
            task = asyncio.current_task()
            child_tasks.append(task)
            if platform == "claude_api":
                await sibling_started.wait()
                events.append("api:launch_failed")
                raise register_three_platforms.PlatformLaunchError(
                    "api launch failed"
                )
            process = object()
            process_owner.track_process(process)
            running.add(platform)
            events.append("claude:tracked")
            sibling_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                events.append("claude:cancelled")
                process_owner.confirm_process_stopped(process, True)
                running.discard(platform)
                sibling_finished.set()
                events.append("claude:confirmed")
                raise

        try:
            with patch.object(
                register_three_platforms, "run_platform", side_effect=fake_run
            ), self.assertRaisesRegex(
                register_three_platforms.PlatformLaunchError,
                "api launch failed",
            ):
                await register_three_platforms.process_account(
                    reserved, self.parallel_args(), {"EMAIL_PROVIDER": "NINEMALL"}
                )

            self.assertTrue(sibling_finished.is_set())
            self.assertEqual(running, set())
            self.assertEqual(reserved.owned_processes, set())
            self.assertFalse(reserved.active)
            self.assertLess(
                events.index("claude:confirmed"),
                events.index("release:claude"),
            )
            self.assertEqual(
                {purpose: store.calls for purpose, store in stores.items()},
                {"claude": 1, "claude_api": 1},
            )
        finally:
            await self.drain_tasks(child_tasks)

    async def test_parallel_runtime_error_preserves_primary_after_sibling_cleanup_error(self):
        events = []
        _account, stores, reserved = self.recorded_reserved(events)
        both_started = asyncio.Event()
        started = set()
        child_tasks = []
        running = set()

        async def fake_run(
            platform, _command, _run_id, _child_env, process_owner=None
        ):
            child_tasks.append(asyncio.current_task())
            process = object()
            process_owner.track_process(process)
            started.add(platform)
            running.add(platform)
            events.append(f"{platform}:tracked")
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            if platform == "claude_api":
                process_owner.confirm_process_stopped(process, True)
                running.discard(platform)
                events.append("claude_api:confirmed")
                raise RuntimeError("primary-runtime")
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                process_owner.confirm_process_stopped(process, True)
                running.discard(platform)
                events.append("claude:confirmed")
                raise ValueError("sibling-cleanup-error")

        try:
            with patch.object(
                register_three_platforms, "run_platform", side_effect=fake_run
            ), self.assertRaisesRegex(RuntimeError, "primary-runtime"):
                await register_three_platforms.process_account(
                    reserved, self.parallel_args(), {"EMAIL_PROVIDER": "NINEMALL"}
                )

            self.assertEqual(running, set())
            self.assertEqual(reserved.owned_processes, set())
            self.assertFalse(reserved.active)
            self.assertLess(
                events.index("claude:confirmed"),
                events.index("release:claude"),
            )
            self.assertEqual(
                {purpose: store.calls for purpose, store in stores.items()},
                {"claude": 1, "claude_api": 1},
            )
        finally:
            await self.drain_tasks(child_tasks)

    async def test_parallel_caller_cancellation_waits_for_both_children(self):
        events = []
        _account, stores, reserved = self.recorded_reserved(events)
        both_started = asyncio.Event()
        started = set()
        child_tasks = []
        running = set()

        async def fake_run(
            platform, _command, _run_id, _child_env, process_owner=None
        ):
            child_tasks.append(asyncio.current_task())
            process = object()
            process_owner.track_process(process)
            started.add(platform)
            running.add(platform)
            events.append(f"{platform}:tracked")
            if len(started) == 2:
                both_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                process_owner.confirm_process_stopped(process, True)
                running.discard(platform)
                events.append(f"{platform}:confirmed")
                raise

        parent = None
        try:
            with patch.object(
                register_three_platforms, "run_platform", side_effect=fake_run
            ):
                parent = asyncio.create_task(
                    register_three_platforms.process_account(
                        reserved,
                        self.parallel_args(),
                        {"EMAIL_PROVIDER": "NINEMALL"},
                    )
                )
                await both_started.wait()
                parent.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await parent

            self.assertEqual(running, set())
            self.assertEqual(reserved.owned_processes, set())
            self.assertFalse(reserved.active)
            release_index = events.index("release:claude")
            self.assertTrue(all(
                events.index(f"{platform}:confirmed") < release_index
                for platform in ("claude", "claude_api")
            ))
            self.assertEqual(
                {purpose: store.calls for purpose, store in stores.items()},
                {"claude": 1, "claude_api": 1},
            )
        finally:
            if parent is not None and not parent.done():
                parent.cancel()
            await self.drain_tasks(child_tasks)

    async def test_parallel_second_cancellation_cannot_skip_sibling_cleanup(self):
        events = []
        _account, stores, reserved = self.recorded_reserved(events)
        both_started = asyncio.Event()
        both_cleaning = asyncio.Event()
        allow_cleanup = asyncio.Event()
        started = set()
        cleaning = set()
        child_tasks = []
        running = set()

        async def fake_run(
            platform, _command, _run_id, _child_env, process_owner=None
        ):
            child_tasks.append(asyncio.current_task())
            process = object()
            process_owner.track_process(process)
            started.add(platform)
            running.add(platform)
            if len(started) == 2:
                both_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.add(platform)
                if len(cleaning) == 2:
                    both_cleaning.set()
                await allow_cleanup.wait()
                process_owner.confirm_process_stopped(process, True)
                running.discard(platform)
                events.append(f"{platform}:confirmed")
                raise

        parent = None
        try:
            with patch.object(
                register_three_platforms, "run_platform", side_effect=fake_run
            ):
                parent = asyncio.create_task(
                    register_three_platforms.process_account(
                        reserved,
                        self.parallel_args(),
                        {"EMAIL_PROVIDER": "NINEMALL"},
                    )
                )
                await both_started.wait()
                parent.cancel()
                await both_cleaning.wait()
                parent.cancel()
                await asyncio.sleep(0)
                self.assertFalse(parent.done())
                allow_cleanup.set()
                with self.assertRaises(asyncio.CancelledError):
                    await parent

            self.assertEqual(running, set())
            self.assertEqual(reserved.owned_processes, set())
            self.assertFalse(reserved.active)
            release_index = events.index("release:claude")
            self.assertTrue(all(
                events.index(f"{platform}:confirmed") < release_index
                for platform in ("claude", "claude_api")
            ))
            self.assertEqual(
                {purpose: store.calls for purpose, store in stores.items()},
                {"claude": 1, "claude_api": 1},
            )
        finally:
            allow_cleanup.set()
            if parent is not None and not parent.done():
                parent.cancel()
            await self.drain_tasks(child_tasks)

    async def test_parallel_base_exception_cleans_sibling_then_reraises_original(self):
        events = []
        _account, stores, reserved = self.recorded_reserved(events)
        sibling_started = asyncio.Event()
        child_tasks = []
        running = set()
        original = ParallelAbort("keyboard-interrupt-like")

        async def fake_run(
            platform, _command, _run_id, _child_env, process_owner=None
        ):
            child_tasks.append(asyncio.current_task())
            if platform == "claude_api":
                await sibling_started.wait()
                raise original
            process = object()
            process_owner.track_process(process)
            running.add(platform)
            sibling_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                process_owner.confirm_process_stopped(process, True)
                running.discard(platform)
                events.append("claude:confirmed")
                raise

        try:
            with patch.object(
                register_three_platforms, "run_platform", side_effect=fake_run
            ):
                with self.assertRaises(ParallelAbort) as caught:
                    await register_three_platforms.process_account(
                        reserved,
                        self.parallel_args(),
                        {"EMAIL_PROVIDER": "NINEMALL"},
                    )

            self.assertIs(caught.exception, original)
            self.assertEqual(running, set())
            self.assertEqual(reserved.owned_processes, set())
            self.assertFalse(reserved.active)
            self.assertLess(
                events.index("claude:confirmed"),
                events.index("release:claude"),
            )
            self.assertEqual(
                {purpose: store.calls for purpose, store in stores.items()},
                {"claude": 1, "claude_api": 1},
            )
        finally:
            await self.drain_tasks(child_tasks)

    async def test_parallel_unconfirmed_shutdown_keeps_owner_active_for_retry(self):
        events = []
        _account, stores, reserved = self.recorded_reserved(events)
        sibling_started = asyncio.Event()
        child_tasks = []
        running = set()
        sibling_process = object()

        async def fake_run(
            platform, _command, _run_id, _child_env, process_owner=None
        ):
            child_tasks.append(asyncio.current_task())
            if platform == "claude_api":
                await sibling_started.wait()
                raise RuntimeError("primary-runtime")
            process_owner.track_process(sibling_process)
            running.add(platform)
            sibling_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                process_owner.confirm_process_stopped(
                    sibling_process, False
                )
                running.discard(platform)
                events.append("claude:unconfirmed")
                raise

        try:
            with patch.object(
                register_three_platforms, "run_platform", side_effect=fake_run
            ), self.assertRaisesRegex(RuntimeError, "primary-runtime"):
                await register_three_platforms.process_account(
                    reserved,
                    self.parallel_args(),
                    {"EMAIL_PROVIDER": "NINEMALL"},
                )

            self.assertEqual(running, set())
            self.assertEqual(reserved.owned_processes, {id(sibling_process)})
            self.assertTrue(reserved.active)
            self.assertEqual(events, ["claude:unconfirmed"])
            self.assertEqual(
                {purpose: store.calls for purpose, store in stores.items()},
                {"claude": 0, "claude_api": 0},
            )

            reserved.confirm_process_stopped(sibling_process, True)
            self.assertTrue(reserved.release())
            self.assertFalse(reserved.active)
            self.assertFalse(reserved.release())
            self.assertEqual(
                events,
                [
                    "claude:unconfirmed",
                    "release:claude",
                    "release:claude_api",
                ],
            )
            self.assertEqual(
                {purpose: store.calls for purpose, store in stores.items()},
                {"claude": 1, "claude_api": 1},
            )
        finally:
            await self.drain_tasks(child_tasks)

    async def test_parallel_success_results_stay_ordered_without_parent_release(self):
        events = []
        _account, stores, reserved = self.recorded_reserved(events)
        allow_claude = asyncio.Event()

        async def fake_run(
            platform, _command, _run_id, _child_env, process_owner=None
        ):
            if platform == "claude":
                await allow_claude.wait()
            else:
                allow_claude.set()
            return platform, True, 0, f"{platform}.log"

        with patch.object(
            register_three_platforms, "run_platform", side_effect=fake_run
        ):
            results = await register_three_platforms.process_account(
                reserved,
                self.parallel_args(),
                {"EMAIL_PROVIDER": "NINEMALL"},
            )

        self.assertEqual(
            [result[0] for result in results], ["claude", "claude_api"]
        )
        self.assertTrue(reserved.active)
        self.assertEqual(events, [])
        self.assertEqual(
            {purpose: store.calls for purpose, store in stores.items()},
            {"claude": 0, "claude_api": 0},
        )

    async def test_from_pool_dual_family_uses_atomic_reservation(self):
        account, stores = self.reserve()
        shared = Mock(return_value=(account, stores))

        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "reserve_shared_claude_account",
            shared,
            create=True,
        ), patch.object(
            register_three_platforms,
            "ClaudeEmailAccountStore",
            side_effect=AssertionError("non-atomic reservation used"),
        ):
            selected = register_three_platforms.next_pool_account(self.args())

        self.assertEqual(selected, account_tuple(account))
        self.assertEqual(set(selected.stores), {"claude", "claude_api"})
        shared.assert_called_once_with(
            "NINEMALL", ("claude", "claude_api")
        )

    async def test_full_flow_dual_family_uses_atomic_reservation(self):
        account, stores = self.reserve()
        shared = Mock(return_value=(account, stores))

        with patch.object(
            run_full_flow,
            "reserve_shared_claude_account",
            shared,
            create=True,
        ):
            selected = run_full_flow.acquire_stage_account(
                self.args(),
                {"EMAIL_PROVIDER": "NINEMALL"},
                stage_email_fn=Mock(
                    side_effect=AssertionError("Outlook Stage A ran")
                ),
                store_factory=Mock(
                    side_effect=AssertionError("non-atomic reservation used")
                ),
            )

        self.assertEqual(selected, account_tuple(account))
        self.assertEqual(set(selected.stores), {"claude", "claude_api"})
        shared.assert_called_once_with(
            "NINEMALL", ("claude", "claude_api")
        )

    async def test_full_flow_failed_stage_releases_only_nonterminal_ledger(self):
        account, stores = self.reserve()
        args = self.args()
        args.skip_email = False
        args.email = ""
        args.password = ""
        args.token = ""
        args.client_id = ""
        args.dry_run = False

        def fake_platforms(
            _args,
            _env,
            _email,
            _password,
            _token,
            _client_id,
            process_owner=None,
        ):
            self.assertIsInstance(
                process_owner, run_full_flow._ReservedStageAccount
            )
            self.child_store("claude").mark_used(account)
            return 1

        with patch.object(
            run_full_flow,
            "reserve_shared_claude_account",
            return_value=(account, stores),
        ), patch.object(
            run_full_flow, "stage_platforms", side_effect=fake_platforms
        ):
            result = run_full_flow.run_once(
                args, {"EMAIL_PROVIDER": "NINEMALL"}
            )

        self.assertEqual(result, (1, account.email))
        self.assertEqual(
            self.ledger("claude"),
            ["person@example.com----reserved", "person@example.com----ok"],
        )
        self.assertEqual(
            self.ledger("claude_api"),
            [
                "person@example.com----reserved",
                "person@example.com----released",
            ],
        )

    async def test_mixed_result_releases_only_nonterminal_child_reservation(self):
        account, stores = self.reserve()
        reserved = register_three_platforms._ReservedPoolAccount(account, stores)
        commands = {}

        async def fake_run(
            platform, command, _run_id, _child_env, process_owner=None
        ):
            self.assertIs(process_owner, reserved)
            commands[platform] = command
            if platform == "claude":
                self.child_store(platform).mark_used(account)
                return platform, True, 0, "test.log"
            return platform, False, 1, "test.log"

        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms, "run_platform", side_effect=fake_run
        ):
            results = await register_three_platforms.process_account(
                reserved, self.args(), {"EMAIL_PROVIDER": "NINEMALL"}
            )

        self.assertEqual([result[1] for result in results], [True, False])
        self.assertEqual(
            self.ledger("claude"),
            ["person@example.com----reserved", "person@example.com----ok"],
        )
        self.assertEqual(
            self.ledger("claude_api"),
            [
                "person@example.com----reserved",
                "person@example.com----released",
            ],
        )
        for command in commands.values():
            self.assertEqual(command[command.index("--email") + 1], account.email)
            self.assertEqual(
                command[command.index("--password") + 1], account.password
            )
            self.assertEqual(
                command[command.index("--token") + 1], account.refresh_token
            )
            self.assertEqual(
                command[command.index("--client-id") + 1], account.client_id
            )

    async def test_terminal_failure_is_not_released_or_rewritten_by_parent(self):
        account, stores = self.reserve()
        reserved = register_three_platforms._ReservedPoolAccount(account, stores)

        async def fake_run(
            platform, _command, _run_id, _child_env, process_owner=None
        ):
            store = self.child_store(platform)
            if platform == "claude":
                store.mark_used(account)
                return platform, True, 0, "test.log"
            store.mark_error(account, "console_not_reached")
            return platform, False, 1, "test.log"

        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms, "run_platform", side_effect=fake_run
        ):
            await register_three_platforms.process_account(
                reserved, self.args(), {"EMAIL_PROVIDER": "NINEMALL"}
            )

        self.assertEqual(
            self.ledger("claude"),
            ["person@example.com----reserved", "person@example.com----ok"],
        )
        self.assertEqual(
            self.ledger("claude_api"), ["person@example.com----reserved"]
        )
        self.assertEqual(
            (self.root / "mail_error_claude_api.txt")
            .read_text(encoding="utf-8")
            .splitlines(),
            ["person@example.com----console_not_reached"],
        )

    async def test_cancellation_releases_only_child_without_terminal_state(self):
        account, stores = self.reserve()
        reserved = register_three_platforms._ReservedPoolAccount(account, stores)

        async def fake_run(
            platform, _command, _run_id, _child_env, process_owner=None
        ):
            if platform == "claude":
                self.child_store(platform).mark_used(account)
                return platform, True, 0, "test.log"
            raise asyncio.CancelledError

        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms, "run_platform", side_effect=fake_run
        ), self.assertRaises(asyncio.CancelledError):
            await register_three_platforms.process_account(
                reserved, self.args(), {"EMAIL_PROVIDER": "NINEMALL"}
            )

        self.assertEqual(
            self.ledger("claude"),
            ["person@example.com----reserved", "person@example.com----ok"],
        )
        self.assertEqual(
            self.ledger("claude_api"),
            [
                "person@example.com----reserved",
                "person@example.com----released",
            ],
        )

    async def test_startup_failure_releases_both_reservations_once(self):
        account, stores = self.reserve()
        reserved = register_three_platforms._ReservedPoolAccount(account, stores)

        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "run_platform",
            side_effect=register_three_platforms.PlatformLaunchError(
                "launch failed"
            ),
        ), self.assertRaises(register_three_platforms.PlatformLaunchError):
            await register_three_platforms.process_account(
                reserved, self.args(), {"EMAIL_PROVIDER": "NINEMALL"}
            )

        for purpose in ("claude", "claude_api"):
            self.assertEqual(
                self.ledger(purpose),
                [
                    "person@example.com----reserved",
                    "person@example.com----released",
                ],
            )

    async def test_orchestrator_output_never_prints_mailbox_secrets(self):
        account, stores = self.reserve()
        reserved = register_three_platforms._ReservedPoolAccount(account, stores)

        async def fake_run(
            platform, _command, _run_id, _child_env, process_owner=None
        ):
            self.child_store(platform).mark_used(account)
            return platform, True, 0, "sanitized.log"

        output = io.StringIO()
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms, "run_platform", side_effect=fake_run
        ), contextlib.redirect_stdout(output):
            await register_three_platforms.process_account(
                reserved, self.args(), {"EMAIL_PROVIDER": "NINEMALL"}
            )

        rendered = output.getvalue()
        for secret in (account.password, account.refresh_token, account.client_id):
            self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
