from urllib.parse import urlparse
import asyncio
import time

from common.claude_platform_mailbox import validate_claude_platform_magic_link
from common.claude_platform_session import save_claude_platform_session


PLATFORM_URL = "https://platform.claude.com/"


class ClaudeApiRegistrationError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


async def apply_verification_artifact(page, artifact):
    try:
        code_input = page.locator('[data-testid="code"]')
        code_visible = (
            await code_input.count() == 1
            and await code_input.is_visible()
        )
        if artifact.code and code_visible:
            await code_input.fill(artifact.code)
            submit = page.locator('button[data-testid="continue"]')
            if await submit.count() != 1:
                raise ClaudeApiRegistrationError("verification_rejected")
            await submit.click()
            return "code"

        if artifact.magic_link:
            magic_link = validate_claude_platform_magic_link(artifact.magic_link)
            if not magic_link:
                raise ClaudeApiRegistrationError("verification_rejected")
            await page.goto(magic_link, timeout=60000)
            return "magic_link"

        if artifact.code:
            enter = page.locator('button[data-testid="enter-code"]')
            if await enter.count() != 1:
                raise ClaudeApiRegistrationError("verification_rejected")
            await enter.click()
            code_input = page.locator('[data-testid="code"]')
            if await code_input.count() != 1:
                raise ClaudeApiRegistrationError("verification_rejected")
            await code_input.fill(artifact.code)
            submit = page.locator('button[data-testid="continue"]')
            if await submit.count() != 1:
                raise ClaudeApiRegistrationError("verification_rejected")
            await submit.click()
            return "code"

        raise ClaudeApiRegistrationError("verification_artifact_not_found")
    except ClaudeApiRegistrationError:
        raise
    except Exception:
        raise ClaudeApiRegistrationError("verification_rejected") from None


async def select_personal_account(page):
    try:
        for name in ("Personal account", "Personal"):
            candidate = page.get_by_role("button", name=name, exact=True)
            count = await candidate.count()
            if count == 1:
                await candidate.click()
                return True
            if count > 1:
                raise ClaudeApiRegistrationError("personal_account_not_available")

        organization_input = page.locator(
            'input[name="organizationName"], input[placeholder*="organization"]'
        )
        if await organization_input.count() > 0:
            raise ClaudeApiRegistrationError("personal_account_not_available")
        headings = page.locator("h1, h2")
        heading_text = " ".join(await headings.all_text_contents()).lower()
        if (
            "create an organization" in heading_text
            or "create your organization" in heading_text
        ):
            raise ClaudeApiRegistrationError("personal_account_not_available")
        return False
    except ClaudeApiRegistrationError:
        raise
    except Exception:
        raise ClaudeApiRegistrationError("console_not_reached") from None


async def is_console_ready(page):
    try:
        parsed = urlparse(page.url)
        if parsed.hostname != "platform.claude.com":
            return False
        if parsed.path.startswith(("/login", "/magic-link")):
            return False
        selectors = (
            'a[href*="/settings/keys"]',
            'a[href*="/workbench"]',
            '[data-testid="workspace-switcher"]',
        )
        for selector in selectors:
            locator = page.locator(selector)
            if await locator.count() == 1 and await locator.is_visible():
                return True
        return False
    except ClaudeApiRegistrationError:
        raise
    except Exception:
        raise ClaudeApiRegistrationError("console_not_reached") from None


async def _verification_rejected(page):
    selectors = (
        '[role="alert"]',
        '[data-testid="verification-error"]',
        '[data-testid="code"][aria-invalid="true"]',
    )
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count() == 1 and await locator.is_visible():
            return True
    return False


async def _wait_for_post_verification_state(page, method, timeout):
    deadline = time.monotonic() + max(0.0, float(timeout))
    personal_selected = False
    while True:
        if await is_console_ready(page):
            return
        if await _verification_rejected(page):
            raise ClaudeApiRegistrationError("verification_rejected")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            code_input = page.locator('[data-testid="code"]')
            code_still_visible = (
                method == "code"
                and await code_input.count() == 1
                and await code_input.is_visible()
            )
            if code_still_visible:
                raise ClaudeApiRegistrationError("verification_rejected")
            raise ClaudeApiRegistrationError("console_not_reached")

        if not personal_selected:
            personal_selected = await select_personal_account(page)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClaudeApiRegistrationError("console_not_reached")
        await asyncio.sleep(min(0.05, remaining))


async def run_claude_platform_flow(
    page,
    context,
    account,
    fetch_verification,
    max_wait,
    output_dir="cookies/claude_api",
):
    try:
        await page.goto(PLATFORM_URL, timeout=60000)
        email = page.locator('[data-testid="email"]')
        submit = page.locator('button[data-testid="continue"]')
        if await email.count() != 1 or await submit.count() != 1:
            raise ClaudeApiRegistrationError("registration_error")
        await email.fill(account.email)
        requested_at = time.time()
        await submit.click()
    except ClaudeApiRegistrationError:
        raise
    except Exception:
        raise ClaudeApiRegistrationError("registration_error") from None

    try:
        artifact = await fetch_verification(
            context,
            account,
            max_wait,
            requested_at,
        )
        if artifact is None:
            resend = page.get_by_role("button", name="Resend email", exact=True)
            if await resend.count() != 1:
                raise ClaudeApiRegistrationError("mail_timeout")
            requested_at = time.time()
            await resend.click()
            artifact = await fetch_verification(
                context,
                account,
                max_wait,
                requested_at,
            )
        if artifact is None:
            raise ClaudeApiRegistrationError("verification_artifact_not_found")
    except ClaudeApiRegistrationError:
        raise
    except Exception:
        raise ClaudeApiRegistrationError("mail_timeout") from None

    try:
        method = await apply_verification_artifact(page, artifact)
        await _wait_for_post_verification_state(page, method, max_wait)
    except ClaudeApiRegistrationError:
        raise
    except Exception:
        raise ClaudeApiRegistrationError("console_not_reached") from None

    try:
        return await save_claude_platform_session(
            context,
            account.email,
            output_dir,
        )
    except ClaudeApiRegistrationError:
        raise
    except Exception:
        raise ClaudeApiRegistrationError("console_not_reached") from None
