import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
    ):
        self.state = state
        self.personal_label = personal_label
        self.magic_link_target_state = magic_link_target_state
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
        return FakeLocator(self, selector)

    def get_by_role(self, role, *, name, exact=False):
        return FakeLocator(self, "", role=role, name=name, exact=exact)

    async def goto(self, url, timeout):
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
            return int(self.state == "code")
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
                self.state = "authenticated"
                self.url = "https://platform.claude.com/workbench"
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


class FakeBrowserContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return list(self._cookies)


class ClaudeApiRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account = ClaudeEmailAccount(
            "OUTLOOK", "person@example.com", "mail-secret", "client-secret", "refresh-secret"
        )

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


if __name__ == "__main__":
    unittest.main()
