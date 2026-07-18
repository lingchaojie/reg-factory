import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import traceback
import unittest
from unittest.mock import patch
from urllib.parse import quote

from common.claude_email_accounts import ClaudeEmailAccount
from common.claude_platform_mailbox import ClaudePlatformVerification
import register_claude_api


class FakeLocator:
    def __init__(self, page, selector, role=None, name=None, exact=False):
        self.page = page
        self.selector = selector
        self.role = role
        self.name = name
        self.exact = exact

    def _kind(self):
        if self.role == "button":
            return ("button", self.name, self.exact)
        return ("selector", self.selector)

    async def count(self):
        return self.page.element_count(self._kind())

    async def is_visible(self):
        return self.page.element_visible(self._kind())

    async def fill(self, value):
        self.page.fill_element(self._kind(), value)

    async def click(self):
        self.page.click_element(self._kind())

    async def all_text_contents(self):
        return self.page.element_texts(self._kind())


class FakePlatformPage:
    def __init__(
        self,
        state="start",
        *,
        personal_label="Personal account",
        magic_link_target_state="authenticated",
        code_submit_target_state="authenticated",
        fail_navigation="",
        fail_locator="",
    ):
        self.state = state
        self.personal_label = personal_label
        self.magic_link_target_state = magic_link_target_state
        self.code_submit_target_state = code_submit_target_state
        self.fail_navigation = fail_navigation
        self.fail_locator = fail_locator
        self.url = self._url_for_state(state)
        self.email_value = ""
        self.code_value = ""
        self.goto_calls = []
        self.submissions = []
        self.resend_count = 0
        self.load_waits = []

    @staticmethod
    def _url_for_state(state):
        if state == "authenticated":
            return "https://platform.claude.com/workbench"
        if state == "foreign_authenticated":
            return "https://example.com/workbench"
        if state == "magic_link":
            return "https://platform.claude.com/magic-link?code=abc"
        return "https://platform.claude.com/login"

    def locator(self, selector):
        if self.fail_locator:
            raise RuntimeError(self.fail_locator)
        return FakeLocator(self, selector)

    def get_by_role(self, role, *, name, exact=False):
        return FakeLocator(self, "", role=role, name=name, exact=exact)

    async def goto(self, url, timeout):
        if self.fail_navigation == "initial" and url == register_claude_api.PLATFORM_URL:
            raise RuntimeError(self.fail_navigation_secret)
        if self.fail_navigation == "verification" and url != register_claude_api.PLATFORM_URL:
            raise RuntimeError(self.fail_navigation_secret)
        self.goto_calls.append((url, timeout))
        self.url = url
        if url == register_claude_api.PLATFORM_URL:
            self.state = "start"
            self.url = "https://platform.claude.com/login"
        elif url.startswith("https://platform.claude.com/magic-link"):
            self.state = self.magic_link_target_state
            self.url = (
                "https://platform.claude.com/workbench"
                if self.state == "authenticated"
                else "https://platform.claude.com/onboarding"
            )

    async def wait_for_load_state(self, state, timeout):
        self.load_waits.append((state, timeout))

    def element_count(self, kind):
        if kind == ("selector", '[data-testid="email"]'):
            return int(self.state == "start")
        if kind == ("selector", 'button[data-testid="continue"]'):
            return int(self.state in {"start", "code", "organization"})
        if kind == ("selector", '[data-testid="code"]'):
            return int(self.state in {"code", "invalid_code"})
        if kind in {
            ("selector", '[role="alert"]'),
            ("selector", '[data-testid="verification-error"]'),
            ("selector", '[data-testid="code"][aria-invalid="true"]'),
        }:
            return int(self.state == "invalid_code")
        if kind == ("selector", 'button[data-testid="enter-code"]'):
            return int(self.state == "email_sent")
        if kind == (
            "selector",
            'input[name="organizationName"], input[placeholder*="organization"]',
        ):
            return int(self.state == "organization")
        if kind == ("selector", "h1, h2"):
            return int(self.state == "organization")
        if kind in {
            ("selector", 'a[href*="/settings/keys"]'),
            ("selector", 'a[href*="/workbench"]'),
            ("selector", '[data-testid="workspace-switcher"]'),
        }:
            return int(self.state in {"authenticated", "foreign_authenticated"})
        if kind[0] == "button":
            _role, name, exact = kind
            if name == "Resend email" and exact:
                return int(self.state == "email_sent")
            if name == self.personal_label and exact:
                return int(self.state == "personal")
        return 0

    def element_visible(self, kind):
        return self.element_count(kind) == 1

    def fill_element(self, kind, value):
        if kind == ("selector", '[data-testid="email"]'):
            self.email_value = value
            return
        if kind == ("selector", '[data-testid="code"]'):
            self.code_value = value
            return
        raise AssertionError(f"cannot fill {kind} in state {self.state}")

    def click_element(self, kind):
        if kind == ("selector", 'button[data-testid="continue"]'):
            self.submissions.append(self.state)
            if self.state == "start":
                self.state = "email_sent"
            elif self.state == "code":
                self.state = self.code_submit_target_state
                self.url = (
                    "https://platform.claude.com/workbench"
                    if self.state == "authenticated"
                    else "https://platform.claude.com/onboarding"
                )
            return
        if kind == ("selector", 'button[data-testid="enter-code"]'):
            self.state = "code"
            return
        if kind == ("button", "Resend email", True):
            self.resend_count += 1
            return
        if kind == ("button", self.personal_label, True):
            self.state = "authenticated"
            self.url = "https://platform.claude.com/workbench"
            return
        raise AssertionError(f"cannot click {kind} in state {self.state}")

    def element_texts(self, kind):
        if kind == ("selector", "h1, h2") and self.state == "organization":
            return ["Create your organization"]
        return []


class DelayedPlatformPage(FakePlatformPage):
    def __init__(self, *args, transition_after=2, transition_to="authenticated", **kwargs):
        super().__init__(*args, **kwargs)
        self.transition_after = transition_after
        self.transition_to = transition_to
        self.state_probes = 0

    def locator(self, selector):
        if selector == 'a[href*="/settings/keys"]' and self.state == "pending":
            self.state_probes += 1
            if self.state_probes >= self.transition_after:
                self.state = self.transition_to
                self.url = (
                    "https://platform.claude.com/workbench"
                    if self.state == "authenticated"
                    else "https://platform.claude.com/onboarding"
                )
        return super().locator(selector)


class FakeBrowserContext:
    def __init__(self, cookies, *, failure=""):
        self._cookies = cookies
        self.failure = failure

    async def cookies(self):
        if self.failure:
            raise RuntimeError(self.failure)
        return list(self._cookies)


class ClaudeApiRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account = ClaudeEmailAccount(
            "OUTLOOK", "person@example.com", "mail-secret", "client-secret", "refresh-secret"
        )
        self.secret_error = (
            "person@example.com mail-secret 482731 "
            "https://platform.claude.com/magic-link?code=link-secret"
        )

    def assert_safe_terminal_error(self, error, code):
        self.assertEqual(error.code, code)
        rendered = "".join(traceback.format_exception(error))
        for secret in (
            "person@example.com",
            "mail-secret",
            "482731",
            "link-secret",
        ):
            self.assertNotIn(secret, rendered)
        self.assertTrue(error.__suppress_context__)

    async def test_code_only_artifact_opens_code_ui_and_submits(self):
        page = FakePlatformPage(state="email_sent")
        artifact = ClaudePlatformVerification(code="482731")

        result = await register_claude_api.apply_verification_artifact(page, artifact)

        self.assertEqual(result, "code")
        self.assertEqual(page.code_value, "482731")
        self.assertEqual(page.state, "authenticated")

    async def test_link_only_artifact_navigates_to_valid_link(self):
        page = FakePlatformPage(state="email_sent")
        link = "https://platform.claude.com/magic-link?code=abc"

        result = await register_claude_api.apply_verification_artifact(
            page, ClaudePlatformVerification(magic_link=link)
        )

        self.assertEqual(result, "magic_link")
        self.assertEqual(page.goto_calls, [(link, 60000)])
        self.assertEqual(page.state, "authenticated")

    async def test_safelink_artifact_navigates_only_to_decoded_platform_target(self):
        page = FakePlatformPage(state="email_sent")
        direct = "https://platform.claude.com/magic-link?code=abc"
        safelink = (
            "https://nam01.safelinks.protection.outlook.com/?url="
            + quote(direct, safe="")
        )

        await register_claude_api.apply_verification_artifact(
            page, ClaudePlatformVerification(magic_link=safelink)
        )

        self.assertEqual(page.goto_calls, [(direct, 60000)])

    async def test_broker_shaped_raw_invalid_link_is_rejected_before_navigation(self):
        page = FakePlatformPage(state="email_sent")
        raw_broker_artifact = ClaudePlatformVerification(
            magic_link="https://platform.claude.com.evil.example/magic-link?code=mail-secret",
            received_at=2_000_000_001.0,
        )

        with self.assertRaisesRegex(
            register_claude_api.ClaudeApiRegistrationError,
            "^verification_rejected$",
        ):
            await register_claude_api.apply_verification_artifact(
                page, raw_broker_artifact
            )

        self.assertEqual(page.goto_calls, [])

    async def test_both_artifacts_use_code_when_code_input_is_visible(self):
        page = FakePlatformPage(state="code")
        artifact = ClaudePlatformVerification(
            magic_link="https://platform.claude.com/magic-link?code=abc",
            code="482731",
        )

        await register_claude_api.apply_verification_artifact(page, artifact)

        self.assertEqual(page.code_value, "482731")
        self.assertEqual(page.goto_calls, [])

    async def test_both_artifacts_use_link_when_code_input_is_not_visible(self):
        page = FakePlatformPage(state="email_sent")
        link = "https://platform.claude.com/magic-link?code=abc"
        artifact = ClaudePlatformVerification(magic_link=link, code="482731")

        await register_claude_api.apply_verification_artifact(page, artifact)

        self.assertEqual(page.goto_calls, [(link, 60000)])
        self.assertEqual(page.code_value, "")

    async def test_personal_account_option_is_selected_by_exact_name(self):
        page = FakePlatformPage(state="personal", personal_label="Personal")

        selected = await register_claude_api.select_personal_account(page)

        self.assertTrue(selected)
        self.assertEqual(page.state, "authenticated")

    async def test_organization_form_is_never_submitted(self):
        page = FakePlatformPage(state="organization")

        with self.assertRaisesRegex(
            register_claude_api.ClaudeApiRegistrationError,
            "personal_account_not_available",
        ):
            await register_claude_api.select_personal_account(page)

        self.assertEqual(page.submissions, [])

    async def test_console_ready_requires_platform_url_and_visible_console_element(self):
        self.assertTrue(
            await register_claude_api.is_console_ready(
                FakePlatformPage(state="authenticated")
            )
        )
        self.assertFalse(
            await register_claude_api.is_console_ready(
                FakePlatformPage(state="foreign_authenticated")
            )
        )
        login = FakePlatformPage(state="start")
        login.state = "authenticated"
        login.url = "https://platform.claude.com/login"
        self.assertFalse(await register_claude_api.is_console_ready(login))

    async def test_console_locator_failure_is_safe_console_error(self):
        page = FakePlatformPage(
            state="authenticated",
            fail_locator=self.secret_error,
        )

        try:
            await register_claude_api.is_console_ready(page)
        except register_claude_api.ClaudeApiRegistrationError as error:
            self.assert_safe_terminal_error(error, "console_not_reached")
        else:
            self.fail("expected a safe console locator error")

    async def test_flow_resends_once_then_exports_authenticated_session(self):
        page = FakePlatformPage()
        context = FakeBrowserContext([
            {"name": "session", "value": "browser-secret", "domain": ".claude.com"},
        ])
        artifacts = [None, ClaudePlatformVerification(code="482731")]
        fetches = []

        async def fetch_verification(*args):
            fetches.append(args)
            return artifacts.pop(0)

        with TemporaryDirectory() as temp_dir:
            cookie_path = await register_claude_api.run_claude_platform_flow(
                page, context, self.account, fetch_verification, 7, temp_dir
            )
            self.assertTrue(cookie_path.exists())

        self.assertEqual(page.email_value, self.account.email)
        self.assertEqual(page.resend_count, 1)
        self.assertEqual(len(fetches), 2)
        self.assertIs(fetches[0][0], context)
        self.assertEqual(fetches[0][1:3], (self.account, 7))
        self.assertLessEqual(fetches[0][3], fetches[1][3])

    async def test_flow_never_resends_more_than_once(self):
        page = FakePlatformPage()
        context = FakeBrowserContext([])
        fetch_count = 0

        async def fetch_verification(*_args):
            nonlocal fetch_count
            fetch_count += 1
            return None

        with self.assertRaisesRegex(
            register_claude_api.ClaudeApiRegistrationError,
            "verification_artifact_not_found",
        ):
            await register_claude_api.run_claude_platform_flow(
                page, context, self.account, fetch_verification, 1
            )

        self.assertEqual(fetch_count, 2)
        self.assertEqual(page.resend_count, 1)

    async def test_initial_navigation_failure_is_safe_registration_error(self):
        page = FakePlatformPage(fail_navigation="initial")
        page.fail_navigation_secret = self.secret_error

        async def fetch_verification(*_args):
            return None

        try:
            await register_claude_api.run_claude_platform_flow(
                page, FakeBrowserContext([]), self.account, fetch_verification, 1
            )
        except register_claude_api.ClaudeApiRegistrationError as error:
            self.assert_safe_terminal_error(error, "registration_error")
        else:
            self.fail("expected a stable registration error")

    async def test_mail_fetch_failure_is_safe_mail_timeout(self):
        async def fetch_verification(*_args):
            raise RuntimeError(self.secret_error)

        try:
            await register_claude_api.run_claude_platform_flow(
                FakePlatformPage(),
                FakeBrowserContext([]),
                self.account,
                fetch_verification,
                1,
            )
        except register_claude_api.ClaudeApiRegistrationError as error:
            self.assert_safe_terminal_error(error, "mail_timeout")
        else:
            self.fail("expected a stable mail timeout")

    async def test_verification_navigation_failure_is_safe_rejection(self):
        page = FakePlatformPage(fail_navigation="verification")
        page.fail_navigation_secret = self.secret_error

        async def fetch_verification(*_args):
            return ClaudePlatformVerification(
                magic_link="https://platform.claude.com/magic-link?code=abc"
            )

        try:
            await register_claude_api.run_claude_platform_flow(
                page,
                FakeBrowserContext([]),
                self.account,
                fetch_verification,
                1,
            )
        except register_claude_api.ClaudeApiRegistrationError as error:
            self.assert_safe_terminal_error(error, "verification_rejected")
        else:
            self.fail("expected a stable verification rejection")

    async def test_cookie_context_failure_is_safe_console_error(self):
        async def fetch_verification(*_args):
            return ClaudePlatformVerification(code="482731")

        try:
            await register_claude_api.run_claude_platform_flow(
                FakePlatformPage(),
                FakeBrowserContext([], failure=self.secret_error),
                self.account,
                fetch_verification,
                1,
            )
        except register_claude_api.ClaudeApiRegistrationError as error:
            self.assert_safe_terminal_error(error, "console_not_reached")
        else:
            self.fail("expected a stable console error")

    async def test_filesystem_failure_is_safe_console_error(self):
        async def fetch_verification(*_args):
            return ClaudePlatformVerification(code="482731")

        with patch(
            "common.claude_platform_session.Path.mkdir",
            side_effect=OSError(self.secret_error),
        ):
            try:
                await register_claude_api.run_claude_platform_flow(
                    FakePlatformPage(),
                    FakeBrowserContext([
                        {"name": "session", "value": "cookie", "domain": ".claude.com"},
                    ]),
                    self.account,
                    fetch_verification,
                    1,
                )
            except register_claude_api.ClaudeApiRegistrationError as error:
                self.assert_safe_terminal_error(error, "console_not_reached")
            else:
                self.fail("expected a stable filesystem error")

    async def test_flow_refuses_organization_onboarding(self):
        page = FakePlatformPage(magic_link_target_state="organization")
        context = FakeBrowserContext([
            {"name": "session", "value": "browser-secret", "domain": ".claude.com"},
        ])

        async def fetch_verification(*_args):
            return ClaudePlatformVerification(
                magic_link="https://platform.claude.com/magic-link?code=abc"
            )

        with self.assertRaisesRegex(
            register_claude_api.ClaudeApiRegistrationError,
            "personal_account_not_available",
        ):
            await register_claude_api.run_claude_platform_flow(
                page, context, self.account, fetch_verification, 1
            )
        self.assertEqual(page.submissions, ["start"])

    async def test_rejected_code_alert_is_verification_rejected(self):
        page = FakePlatformPage(code_submit_target_state="invalid_code")

        async def fetch_verification(*_args):
            return ClaudePlatformVerification(code="482731")

        with self.assertRaisesRegex(
            register_claude_api.ClaudeApiRegistrationError,
            "^verification_rejected$",
        ):
            await register_claude_api.run_claude_platform_flow(
                page,
                FakeBrowserContext([]),
                self.account,
                fetch_verification,
                0.1,
            )

    async def test_code_screen_remaining_until_timeout_is_verification_rejected(self):
        page = FakePlatformPage(code_submit_target_state="code")

        async def fetch_verification(*_args):
            return ClaudePlatformVerification(code="482731")

        with self.assertRaisesRegex(
            register_claude_api.ClaudeApiRegistrationError,
            "^verification_rejected$",
        ):
            await register_claude_api.run_claude_platform_flow(
                page,
                FakeBrowserContext([]),
                self.account,
                fetch_verification,
                0.02,
            )

    async def test_flow_waits_for_delayed_console_transition(self):
        page = DelayedPlatformPage(
            code_submit_target_state="pending",
            transition_after=3,
            transition_to="authenticated",
        )

        async def fetch_verification(*_args):
            return ClaudePlatformVerification(code="482731")

        with TemporaryDirectory() as temp_dir:
            path = await register_claude_api.run_claude_platform_flow(
                page,
                FakeBrowserContext([
                    {"name": "session", "value": "cookie", "domain": ".claude.com"},
                ]),
                self.account,
                fetch_verification,
                0.3,
                temp_dir,
            )

        self.assertEqual(page.state, "authenticated")
        self.assertGreaterEqual(page.state_probes, 3)
        self.assertEqual(path.parent, Path(temp_dir))

    async def test_flow_waits_for_delayed_personal_option_then_selects_it(self):
        page = DelayedPlatformPage(
            magic_link_target_state="pending",
            transition_after=3,
            transition_to="personal",
        )

        async def fetch_verification(*_args):
            return ClaudePlatformVerification(
                magic_link="https://platform.claude.com/magic-link?code=abc"
            )

        with TemporaryDirectory() as temp_dir:
            await register_claude_api.run_claude_platform_flow(
                page,
                FakeBrowserContext([
                    {"name": "session", "value": "cookie", "domain": ".claude.com"},
                ]),
                self.account,
                fetch_verification,
                0.3,
                temp_dir,
            )

        self.assertEqual(page.state, "authenticated")
        self.assertGreaterEqual(page.state_probes, 3)

    async def test_unknown_post_verification_state_times_out_as_console_not_reached(self):
        page = FakePlatformPage(magic_link_target_state="pending")

        async def fetch_verification(*_args):
            return ClaudePlatformVerification(
                magic_link="https://platform.claude.com/magic-link?code=abc"
            )

        with self.assertRaisesRegex(
            register_claude_api.ClaudeApiRegistrationError,
            "^console_not_reached$",
        ):
            await register_claude_api.run_claude_platform_flow(
                page,
                FakeBrowserContext([]),
                self.account,
                fetch_verification,
                0.02,
            )

    async def test_poll_wait_failure_is_safe_console_error(self):
        page = FakePlatformPage(magic_link_target_state="pending")

        async def fetch_verification(*_args):
            return ClaudePlatformVerification(
                magic_link="https://platform.claude.com/magic-link?code=abc"
            )

        with patch(
            "register_claude_api.asyncio.sleep",
            side_effect=RuntimeError(self.secret_error),
        ):
            try:
                await register_claude_api.run_claude_platform_flow(
                    page,
                    FakeBrowserContext([]),
                    self.account,
                    fetch_verification,
                    0.2,
                )
            except register_claude_api.ClaudeApiRegistrationError as error:
                self.assert_safe_terminal_error(error, "console_not_reached")
            else:
                self.fail("expected a safe polling wait error")

    async def test_session_export_keeps_only_claude_cookies_and_masks_index(self):
        cookies = [
            {"name": "session", "value": "browser-cookie-secret", "domain": ".claude.com"},
            {"name": "mailbox-code-482731", "value": "mailbox-secret", "domain": ".outlook.com"},
        ]
        context = FakeBrowserContext(cookies)

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "cookies" / "claude_api"
            path = await register_claude_api.save_claude_platform_session(
                context, self.account.email, output_dir
            )
            index_path = output_dir / "accounts.jsonl"
            exported = json.loads(path.read_text(encoding="utf-8"))
            index_text = index_path.read_text(encoding="utf-8")

            self.assertEqual(exported, [cookies[0]])
            self.assertNotIn(self.account.email, path.name)
            self.assertNotIn("482731", path.name)
            self.assertNotIn(self.account.email, index_text)
            self.assertNotIn("browser-cookie-secret", index_text)
            self.assertNotIn("mailbox-secret", index_text)
            self.assertNotIn("482731", index_text)
            self.assertEqual(json.loads(index_text)["cookie_file"], path.name)

    async def test_same_email_concurrent_session_saves_have_unique_files_and_records(self):
        context = FakeBrowserContext([
            {"name": "session", "value": "cookie", "domain": ".claude.com"},
        ])

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "cookies" / "claude_api"
            paths = await asyncio.gather(*(
                register_claude_api.save_claude_platform_session(
                    context,
                    self.account.email,
                    output_dir,
                )
                for _ in range(8)
            ))
            records = [
                json.loads(line)
                for line in (output_dir / "accounts.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

            self.assertEqual(len({path.name for path in paths}), 8)
            self.assertTrue(all(path.exists() for path in paths))
            self.assertEqual(len(records), 8)
            self.assertEqual(
                {record["cookie_file"] for record in records},
                {path.name for path in paths},
            )

    async def test_interrupted_cookie_write_removes_partial_temp_and_final_files(self):
        context = FakeBrowserContext([
            {"name": "session", "value": "cookie", "domain": ".claude.com"},
        ])

        def partial_write(path, *_args, **_kwargs):
            path.write_bytes(b"partial-cookie-secret")
            raise OSError("interrupted cookie write")

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "cookies" / "claude_api"
            with patch.object(Path, "write_text", partial_write):
                with self.assertRaises(OSError):
                    await register_claude_api.save_claude_platform_session(
                        context,
                        self.account.email,
                        output_dir,
                    )

            self.assertEqual(list(output_dir.iterdir()), [])

    async def test_index_failure_removes_cookie_and_temporary_files(self):
        context = FakeBrowserContext([
            {"name": "session", "value": "cookie", "domain": ".claude.com"},
        ])

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "cookies" / "claude_api"
            output_dir.mkdir(parents=True)
            (output_dir / "accounts.jsonl").mkdir()

            with self.assertRaises(OSError):
                await register_claude_api.save_claude_platform_session(
                    context,
                    self.account.email,
                    output_dir,
                )

            self.assertEqual(list(output_dir.glob("full_*.json")), [])
            self.assertEqual(list(output_dir.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
