import argparse
import asyncio
import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import register_three_platforms
import run_full_flow
from common import process_lifecycle
from common.claude_email_accounts import ClaudeEmailAccount
from common.ipmart_proxy import ProxyLease
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


class FakeStore:
    def __init__(self, account, events=None):
        self.account = account
        self.released = []
        self.events = events

    def reserve_one(self):
        return self.account

    def release(self, account):
        if self.events is not None:
            self.events.append("release")
        self.released.append(account)
        return True


class ControllableAsyncStdout:
    def __init__(self, events, failure=None):
        self.events = events
        self.failure = failure
        self.started = asyncio.Event()

    async def readline(self):
        self.events.append("read")
        self.started.set()
        if self.failure is not None:
            raise self.failure
        await asyncio.Future()


class ControllableAsyncProcess:
    def __init__(self, stdout, events, shutdown_results=None):
        self.stdout = stdout
        self.events = events
        self.shutdown_results = list(shutdown_results or [0])
        self.returncode = None
        self.pid = 0

    def terminate(self):
        self.events.append("terminate")

    def kill(self):
        self.events.append("kill")

    async def wait(self):
        self.events.append("wait")
        result = self.shutdown_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        self.returncode = result
        return result


class ControllableSyncProcess:
    def __init__(self, events, initial_failure, shutdown_results=None):
        self.events = events
        self.initial_failure = initial_failure
        self.shutdown_results = list(shutdown_results or [0])
        self.shutdown_started = False
        self.returncode = None
        self.pid = 0

    def terminate(self):
        self.events.append("terminate")
        self.shutdown_started = True

    def kill(self):
        self.events.append("kill")
        self.shutdown_started = True

    def wait(self, timeout=None):
        if not self.shutdown_started:
            self.events.append("initial_wait")
            raise self.initial_failure
        self.events.append("wait")
        result = self.shutdown_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        self.returncode = result
        return result


def full_flow_args():
    return argparse.Namespace(
        skip_email=False,
        email="",
        password="",
        token="",
        client_id="",
        platforms=["claude"],
        dry_run=False,
        node="auto",
        platform_timeout=600,
        broker="",
        keep_on_fail=False,
        import_c2a=False,
        codex=False,
        codex_group=None,
        codex_manual_phone=False,
        grok_sub2api=False,
        grok_sub2api_group=None,
    )


class ClaudeNineMallEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.account = ClaudeEmailAccount(
            "NINEMALL",
            "person@example.com",
            "mail-pass",
            "client-guid",
            "refresh-secret",
        )

    def test_claude_command_passes_token_and_client_id(self):
        command = register_three_platforms.build_command(
            "claude",
            platform_args(["claude"]),
            ("person@example.com", "mail-pass", "refresh-secret", "client-guid"),
        )
        self.assertEqual(command[command.index("--token") + 1], "refresh-secret")
        self.assertEqual(command[command.index("--client-id") + 1], "client-guid")

    def test_pure_claude_from_pool_uses_ninemail_store(self):
        args = platform_args(["claude"])
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "ClaudeEmailAccountStore",
            return_value=FakeStore(self.account),
        ):
            selected = register_three_platforms.next_pool_account(args)
        self.assertEqual(
            selected,
            ("person@example.com", "mail-pass", "refresh-secret", "client-guid"),
        )

    def test_mixed_platform_from_pool_uses_legacy_email_pool(self):
        args = platform_args(["claude", "chatgpt"])
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms, "ClaudeEmailAccountStore"
        ) as store, patch.object(
            register_three_platforms.email_pool,
            "next_email",
            return_value=("legacy@example.com", "pw", "rt", "cid"),
        ) as legacy:
            selected = register_three_platforms.next_pool_account(args)
        self.assertEqual(selected[0], "legacy@example.com")
        store.assert_not_called()
        legacy.assert_called_once_with("tri")

    def test_mixed_platform_claude_child_forces_outlook_provider(self):
        args = platform_args(["claude", "chatgpt"])
        args.parallel = False
        args.broker = ""
        args.grok_timeout = 40
        captured = {}

        async def fake_run(platform, _cmd, _run_id, child_env):
            captured[platform] = dict(child_env)
            return platform, True, 0, "test.log"

        account = ("legacy@example.com", "pw", "rt", "cid")
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms, "run_platform", side_effect=fake_run
        ), patch.object(register_three_platforms, "broker_release"):
            asyncio.run(
                register_three_platforms.process_account(
                    account, args, {"EMAIL_PROVIDER": "NINEMALL"}
                )
            )
        self.assertEqual(captured["claude"]["EMAIL_PROVIDER"], "OUTLOOK")
        self.assertEqual(captured["chatgpt"]["EMAIL_PROVIDER"], "NINEMALL")

    def test_outlook_pure_claude_from_pool_uses_legacy_email_pool(self):
        args = platform_args(["claude"])
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "OUTLOOK"}), patch.object(
            register_three_platforms, "ClaudeEmailAccountStore"
        ) as store, patch.object(
            register_three_platforms.email_pool,
            "next_email",
            return_value=("legacy@example.com", "pw", "rt", "cid"),
        ) as legacy:
            selected = register_three_platforms.next_pool_account(args)
        self.assertEqual(selected[0], "legacy@example.com")
        store.assert_not_called()
        legacy.assert_called_once_with("tri")

    def test_full_flow_pure_claude_bypasses_stage_email(self):
        args = argparse.Namespace(platforms=["claude"])
        stage = Mock(side_effect=AssertionError("Outlook Stage A ran"))
        selected = run_full_flow.acquire_stage_account(
            args,
            {"EMAIL_PROVIDER": "NINEMALL"},
            stage_email_fn=stage,
            store_factory=lambda **_kwargs: FakeStore(self.account),
        )
        self.assertEqual(
            selected,
            ("person@example.com", "mail-pass", "refresh-secret", "client-guid"),
        )
        stage.assert_not_called()

    def test_full_flow_mixed_platform_keeps_stage_email(self):
        args = argparse.Namespace(platforms=["claude", "chatgpt"])
        expected = ("legacy@example.com", "pw", "rt", "cid")
        stage = Mock(return_value=expected)
        selected = run_full_flow.acquire_stage_account(
            args,
            {"EMAIL_PROVIDER": "NINEMALL"},
            stage_email_fn=stage,
        )
        self.assertEqual(selected, expected)
        stage.assert_called_once_with(args, {"EMAIL_PROVIDER": "NINEMALL"})

    def test_full_flow_outlook_pure_claude_keeps_stage_email(self):
        args = argparse.Namespace(platforms=["claude"])
        expected = ("legacy@example.com", "pw", "rt", "cid")
        stage = Mock(return_value=expected)
        selected = run_full_flow.acquire_stage_account(
            args,
            {"EMAIL_PROVIDER": "OUTLOOK"},
            stage_email_fn=stage,
        )
        self.assertEqual(selected, expected)
        stage.assert_called_once_with(args, {"EMAIL_PROVIDER": "OUTLOOK"})

    def test_full_flow_non_claude_keeps_stage_email(self):
        args = argparse.Namespace(platforms=["chatgpt"])
        expected = ("legacy@example.com", "pw", "rt", "cid")
        stage = Mock(return_value=expected)
        selected = run_full_flow.acquire_stage_account(
            args,
            {"EMAIL_PROVIDER": "NINEMALL"},
            stage_email_fn=stage,
        )
        self.assertEqual(selected, expected)
        stage.assert_called_once_with(args, {"EMAIL_PROVIDER": "NINEMALL"})

    def test_full_flow_releases_ninemail_reservation_on_proxy_recheck_failure(self):
        store = FakeStore(self.account)
        args = argparse.Namespace(
            skip_email=False,
            email="",
            password="",
            token="",
            client_id="",
            platforms=["claude"],
            dry_run=False,
        )
        env = {
            "EMAIL_PROVIDER": "NINEMALL",
            "IPMART_ENABLED": "1",
            "IPMART_PROXY_HOST": "gateway.example",
            "IPMART_PROXY_PORT": "8080",
            "IPMART_PROXY_USERNAME_TEMPLATE": "user-{sid}",
            "IPMART_PROXY_PASSWORD": "proxy-pass",
        }
        lease = ProxyLease(
            "http",
            "gateway.example",
            8080,
            "user-00000042",
            "proxy-pass",
            "00000042",
            "203.0.113.8",
        )
        with patch.object(
            run_full_flow, "ClaudeEmailAccountStore", return_value=store
        ), patch.object(run_full_flow, "stage_platforms") as platforms:
            result = run_full_flow.run_once(
                args,
                env,
                acquire=lambda **_kwargs: lease,
                verify=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    run_full_flow.IPMartProxyError("changed")
                ),
            )
        self.assertEqual(result, (1, "person@example.com"))
        self.assertEqual(store.released, [self.account])
        platforms.assert_not_called()

    def test_full_flow_runtime_error_releases_once_and_propagates(self):
        store = FakeStore(self.account)
        args = argparse.Namespace(
            skip_email=False,
            email="",
            password="",
            token="",
            client_id="",
            platforms=["claude"],
            dry_run=False,
        )
        with patch.object(
            run_full_flow, "ClaudeEmailAccountStore", return_value=store
        ), patch.object(
            run_full_flow,
            "stage_platforms",
            side_effect=RuntimeError("unexpected stage unwind"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected stage unwind"):
                run_full_flow.run_once(args, {"EMAIL_PROVIDER": "NINEMALL"})
        self.assertEqual(store.released, [self.account])

    def test_full_flow_keyboard_interrupt_releases_once_and_propagates(self):
        store = FakeStore(self.account)
        args = argparse.Namespace(
            skip_email=False,
            email="",
            password="",
            token="",
            client_id="",
            platforms=["claude"],
            dry_run=False,
        )
        with patch.object(
            run_full_flow, "ClaudeEmailAccountStore", return_value=store
        ), patch.object(
            run_full_flow,
            "stage_platforms",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_full_flow.run_once(args, {"EMAIL_PROVIDER": "NINEMALL"})
        self.assertEqual(store.released, [self.account])

    def test_pool_reservation_released_when_claude_subprocess_cannot_launch(self):
        store = FakeStore(self.account)
        args = platform_args(["claude"])
        args.parallel = False
        args.broker = ""
        args.grok_timeout = 40
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "ClaudeEmailAccountStore",
            return_value=store,
        ), patch.object(
            register_three_platforms,
            "run_platform",
            side_effect=register_three_platforms.PlatformLaunchError("launch failed"),
        ):
            selected = register_three_platforms.next_pool_account(args)
            with self.assertRaises(register_three_platforms.PlatformLaunchError):
                asyncio.run(
                    register_three_platforms.process_account(selected, args, {})
                )
        self.assertEqual(store.released, [self.account])

    def test_pool_reservation_released_on_unwrapped_pre_spawn_error(self):
        store = FakeStore(self.account)
        args = platform_args(["claude"])
        args.parallel = False
        args.broker = ""
        args.grok_timeout = 40
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "ClaudeEmailAccountStore",
            return_value=store,
        ), patch.object(
            register_three_platforms,
            "run_platform",
            side_effect=OSError("log directory unavailable"),
        ):
            selected = register_three_platforms.next_pool_account(args)
            with self.assertRaises(OSError):
                asyncio.run(
                    register_three_platforms.process_account(selected, args, {})
                )
        self.assertEqual(store.released, [self.account])

    def test_pool_reservation_released_once_when_processing_is_cancelled(self):
        store = FakeStore(self.account)
        args = platform_args(["claude"])
        args.parallel = False
        args.broker = ""
        args.grok_timeout = 40
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "ClaudeEmailAccountStore",
            return_value=store,
        ), patch.object(
            register_three_platforms,
            "run_platform",
            side_effect=asyncio.CancelledError,
        ):
            selected = register_three_platforms.next_pool_account(args)
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(
                    register_three_platforms.process_account(selected, args, {})
                )
        self.assertEqual(store.released, [self.account])

    def test_pure_claude_ninemail_does_not_release_outlook_broker(self):
        args = platform_args(["claude"])
        args.parallel = False
        args.broker = "http://127.0.0.1:8765"
        args.grok_timeout = 40
        account = (
            "person@example.com",
            "mail-pass",
            "refresh-secret",
            "client-guid",
        )
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "run_platform",
            return_value=("claude", True, 0, "test.log"),
        ), patch.object(register_three_platforms, "broker_release") as release:
            asyncio.run(register_three_platforms.process_account(account, args, {}))
        release.assert_not_called()

    def test_failed_claude_child_releases_pool_reservation(self):
        store = FakeStore(self.account)
        args = platform_args(["claude"])
        args.parallel = False
        args.broker = ""
        args.grok_timeout = 40
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "ClaudeEmailAccountStore",
            return_value=store,
        ), patch.object(
            register_three_platforms,
            "run_platform",
            return_value=("claude", False, 1, "test.log"),
        ):
            selected = register_three_platforms.next_pool_account(args)
            asyncio.run(register_three_platforms.process_account(selected, args, {}))
        self.assertEqual(store.released, [self.account])

    def test_pure_claude_ninemail_main_propagates_child_failure(self):
        argv = [
            "register_three_platforms.py",
            "--email",
            "person@example.com",
            "--password",
            "mail-pass",
            "--token",
            "refresh-secret",
            "--client-id",
            "client-guid",
            "--platforms",
            "claude",
            "--broker",
            "",
        ]
        failed = [("claude", False, 1, "test.log")]
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms.sys, "argv", argv
        ), patch.object(
            register_three_platforms,
            "process_account",
            new=AsyncMock(return_value=failed),
        ):
            rc = asyncio.run(register_three_platforms.main())
        self.assertEqual(rc, 1)

    def test_stage_platform_command_log_does_not_expose_mailbox_secrets(self):
        args = argparse.Namespace(
            platforms=["claude"],
            node="auto",
            platform_timeout=600,
            broker="",
            keep_on_fail=False,
            import_c2a=False,
            codex=False,
            grok_sub2api=False,
            dry_run=True,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = run_full_flow.stage_platforms(
                args,
                {"EMAIL_PROVIDER": "NINEMALL"},
                "person@example.com",
                "mail-pass",
                "refresh-secret",
                "client-guid",
            )
        self.assertEqual(rc, 0)
        logged = output.getvalue()
        for secret in ("mail-pass", "refresh-secret", "client-guid"):
            self.assertNotIn(secret, logged)

    def test_mixed_stage_platform_log_does_not_expose_mailbox_secrets(self):
        args = argparse.Namespace(
            platforms=["claude", "chatgpt"],
            node="auto",
            platform_timeout=600,
            broker="",
            keep_on_fail=False,
            import_c2a=False,
            codex=False,
            grok_sub2api=False,
            dry_run=True,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = run_full_flow.stage_platforms(
                args,
                {"EMAIL_PROVIDER": "NINEMALL"},
                "legacy@example.com",
                "mail-pass",
                "refresh-secret",
                "client-guid",
            )
        self.assertEqual(rc, 0)
        logged = output.getvalue()
        for secret in ("mail-pass", "refresh-secret", "client-guid"):
            self.assertNotIn(secret, logged)

    def test_webui_command_preview_redacts_mailbox_secrets(self):
        script = scripts.script_by_id("run_full_flow")
        values = {
            "--platforms": ["claude"],
            "--skip-email": True,
            "--email": "person@example.com",
            "--password": "mail-pass",
            "--token": "refresh-secret",
            "--client-id": "client-guid",
        }
        command = server._build_cmd(script, values)
        preview = " ".join(server._redact_cmd(script, command))
        for secret in ("mail-pass", "refresh-secret", "client-guid"):
            self.assertNotIn(secret, preview)
        self.assertIn("***", preview)

    def test_all_webui_mailbox_credential_fields_are_secret(self):
        credential_flags = {
            "--password",
            "--token",
            "--refresh-token",
            "--client-id",
        }
        exposed = []
        for script in scripts.SCRIPTS:
            for item in script["args"]:
                if item["flag"] in credential_flags and not item.get("secret"):
                    exposed.append((script["id"], item["flag"]))
        self.assertEqual(exposed, [])

    def test_webui_exposes_client_id_and_ninemail_env(self):
        claude_flags = {
            item["flag"]
            for item in scripts.script_by_id("register_claude")["args"]
        }
        self.assertIn("--client-id", claude_flags)
        full_flow_flags = {
            item["flag"]
            for item in scripts.script_by_id("run_full_flow")["args"]
        }
        self.assertTrue({"--token", "--client-id"}.issubset(full_flow_flags))
        env_keys = set(scripts.env_keys())
        self.assertTrue(
            {
                "EMAIL_PROVIDER",
                "NINEMALL_EMAIL_FILE",
                "NINEMALL_API_BASE",
                "NINEMALL_API_PASSWORD",
                "NINEMALL_HTTP_TIMEOUT",
                "NINEMALL_POLL_INTERVAL",
            }.issubset(env_keys)
        )
        env_items = {
            item["key"]: item
            for group in scripts.ENV_SCHEMA
            for item in group["items"]
        }
        self.assertEqual(env_items["EMAIL_PROVIDER"]["default"], "NINEMALL")
        self.assertEqual(
            env_items["EMAIL_PROVIDER"]["choices"], ["NINEMALL", "OUTLOOK"]
        )
        self.assertEqual(env_items["NINEMALL_EMAIL_FILE"]["default"], "mail.txt")
        self.assertEqual(
            env_items["NINEMALL_API_BASE"]["default"],
            "https://www.appleemail.top",
        )
        self.assertTrue(env_items["NINEMALL_API_PASSWORD"]["secret"])
        self.assertNotIn("default", env_items["NINEMALL_API_PASSWORD"])
        for key, default in (
            ("NINEMALL_HTTP_TIMEOUT", 30),
            ("NINEMALL_POLL_INTERVAL", 5),
        ):
            self.assertEqual(env_items[key]["type"], "int")
            self.assertEqual(env_items[key]["default"], default)


class ClaudeChildLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.account = ClaudeEmailAccount(
            "NINEMALL",
            "person@example.com",
            "mail-pass",
            "client-guid",
            "refresh-secret",
        )

    def platform_args(self):
        args = platform_args(["claude"])
        args.parallel = False
        args.broker = ""
        args.grok_timeout = 40
        return args

    async def test_async_cancellation_waits_for_child_shutdown_before_release(self):
        events = []
        store = FakeStore(self.account, events)
        reserved = register_three_platforms._ReservedPoolAccount(
            self.account, store
        )
        stdout = ControllableAsyncStdout(events)
        process = ControllableAsyncProcess(stdout, events)

        with patch.object(
            register_three_platforms, "LOG_DIR", self.tmp.name
        ), patch.object(
            register_three_platforms.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            task = asyncio.create_task(
                register_three_platforms.process_account(
                    reserved, self.platform_args(), {}
                )
            )
            await stdout.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(events, ["read", "terminate", "wait", "release"])
        self.assertEqual(store.released, [self.account])

    async def test_async_runtime_error_waits_for_child_shutdown_before_release(self):
        events = []
        store = FakeStore(self.account, events)
        reserved = register_three_platforms._ReservedPoolAccount(
            self.account, store
        )
        stdout = ControllableAsyncStdout(
            events, RuntimeError("post-spawn failure")
        )
        process = ControllableAsyncProcess(stdout, events)

        with patch.object(
            register_three_platforms, "LOG_DIR", self.tmp.name
        ), patch.object(
            register_three_platforms.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ), self.assertRaisesRegex(RuntimeError, "post-spawn failure"):
            await register_three_platforms.process_account(
                reserved, self.platform_args(), {}
            )

        self.assertEqual(events, ["read", "terminate", "wait", "release"])
        self.assertEqual(store.released, [self.account])

    async def test_async_unconfirmed_shutdown_keeps_reservation(self):
        events = []
        store = FakeStore(self.account, events)
        reserved = register_three_platforms._ReservedPoolAccount(
            self.account, store
        )
        stdout = ControllableAsyncStdout(
            events, RuntimeError("post-spawn failure")
        )
        process = ControllableAsyncProcess(
            stdout,
            events,
            shutdown_results=[asyncio.TimeoutError(), asyncio.TimeoutError()],
        )

        with patch.object(
            register_three_platforms, "LOG_DIR", self.tmp.name
        ), patch.object(
            register_three_platforms.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ), self.assertRaisesRegex(RuntimeError, "post-spawn failure"):
            await register_three_platforms.process_account(
                reserved, self.platform_args(), {}
            )

        self.assertEqual(events, ["read", "terminate", "wait", "kill", "wait"])
        self.assertEqual(store.released, [])

    async def test_full_flow_runtime_error_waits_for_shutdown_before_release(self):
        events = []
        store = FakeStore(self.account, events)
        process = ControllableSyncProcess(
            events, RuntimeError("post-spawn failure")
        )

        with patch.object(
            run_full_flow, "ClaudeEmailAccountStore", return_value=store
        ), patch.object(
            run_full_flow.subprocess, "Popen", return_value=process
        ), self.assertRaisesRegex(RuntimeError, "post-spawn failure"):
            run_full_flow.run_once(
                full_flow_args(), {"EMAIL_PROVIDER": "NINEMALL"}
            )

        self.assertEqual(
            events,
            ["initial_wait", "terminate", "wait", "release"],
        )
        self.assertEqual(store.released, [self.account])

    async def test_full_flow_keyboard_interrupt_waits_for_shutdown_before_release(self):
        events = []
        store = FakeStore(self.account, events)
        process = ControllableSyncProcess(events, KeyboardInterrupt())

        with patch.object(
            run_full_flow, "ClaudeEmailAccountStore", return_value=store
        ), patch.object(
            run_full_flow.subprocess, "Popen", return_value=process
        ), self.assertRaises(KeyboardInterrupt):
            run_full_flow.run_once(
                full_flow_args(), {"EMAIL_PROVIDER": "NINEMALL"}
            )

        self.assertEqual(
            events,
            ["initial_wait", "terminate", "wait", "release"],
        )
        self.assertEqual(store.released, [self.account])

    async def test_full_flow_unconfirmed_shutdown_keeps_reservation(self):
        events = []
        store = FakeStore(self.account, events)
        process = ControllableSyncProcess(
            events,
            RuntimeError("post-spawn failure"),
            shutdown_results=[
                run_full_flow.subprocess.TimeoutExpired("child", 5),
                RuntimeError("cleanup wait failed"),
            ],
        )

        with patch.object(
            run_full_flow, "ClaudeEmailAccountStore", return_value=store
        ), patch.object(
            run_full_flow.subprocess, "Popen", return_value=process
        ), self.assertRaisesRegex(RuntimeError, "post-spawn failure"):
            run_full_flow.run_once(
                full_flow_args(), {"EMAIL_PROVIDER": "NINEMALL"}
            )

        self.assertEqual(
            events,
            ["initial_wait", "terminate", "wait", "kill", "wait"],
        )
        self.assertEqual(store.released, [])

    async def test_windows_async_tree_must_be_confirmed_before_release(self):
        events = []
        process = ControllableAsyncProcess(
            ControllableAsyncStdout(events), events, shutdown_results=[0, 0]
        )
        process.pid = 4242

        with patch.object(
            process_lifecycle.sys, "platform", "win32"
        ), patch.object(
            process_lifecycle,
            "_windows_taskkill_tree",
            side_effect=[False, False],
        ) as tree:
            confirmed = await process_lifecycle.shutdown_async_process(process)

        self.assertFalse(confirmed)
        self.assertEqual(events, ["terminate", "wait", "kill", "wait"])
        self.assertEqual(
            [call.kwargs["force"] for call in tree.call_args_list],
            [False, True],
        )

    async def test_windows_sync_tree_must_be_confirmed_before_release(self):
        events = []
        process = ControllableSyncProcess(
            events,
            RuntimeError("unused"),
            shutdown_results=[0, 0],
        )
        process.pid = 4242

        with patch.object(
            process_lifecycle.sys, "platform", "win32"
        ), patch.object(
            process_lifecycle,
            "_windows_taskkill_tree",
            side_effect=[False, False],
        ) as tree:
            confirmed = process_lifecycle.shutdown_sync_process(process)

        self.assertFalse(confirmed)
        self.assertEqual(events, ["terminate", "wait", "kill", "wait"])
        self.assertEqual(
            [call.kwargs["force"] for call in tree.call_args_list],
            [False, True],
        )

    async def test_windows_exited_parent_without_tree_confirmation_is_conservative(self):
        process = Mock(pid=4242, returncode=1)

        with patch.object(
            process_lifecycle.sys, "platform", "win32"
        ), patch.object(
            process_lifecycle,
            "_windows_taskkill_tree",
            return_value=False,
        ) as tree:
            sync_confirmed = process_lifecycle.shutdown_sync_process(process)
            async_confirmed = await process_lifecycle.shutdown_async_process(
                process
            )

        self.assertFalse(sync_confirmed)
        self.assertFalse(async_confirmed)
        self.assertEqual(len(tree.call_args_list), 2)
        self.assertTrue(all(call.kwargs["force"] for call in tree.call_args_list))


if __name__ == "__main__":
    unittest.main()
