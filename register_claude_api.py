from urllib.parse import urlparse
import time

from common.claude_platform_session import save_claude_platform_session


PLATFORM_URL = "https://platform.claude.com/"


class ClaudeApiRegistrationError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


async def apply_verification_artifact(page, artifact):
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
        await page.goto(artifact.magic_link, timeout=60000)
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


async def select_personal_account(page):
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


async def is_console_ready(page):
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


async def run_claude_platform_flow(
    page,
    context,
    account,
    fetch_verification,
    max_wait,
    output_dir="cookies/claude_api",
):
    await page.goto(PLATFORM_URL, timeout=60000)
    email = page.locator('[data-testid="email"]')
    submit = page.locator('button[data-testid="continue"]')
    if await email.count() != 1 or await submit.count() != 1:
        raise ClaudeApiRegistrationError("registration_error")
    await email.fill(account.email)
    requested_at = time.time()
    await submit.click()

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

    await apply_verification_artifact(page, artifact)
    await page.wait_for_load_state("domcontentloaded", timeout=60000)
    if not await is_console_ready(page):
        selected = await select_personal_account(page)
        if selected:
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
    if not await is_console_ready(page):
        raise ClaudeApiRegistrationError("console_not_reached")

    try:
        return await save_claude_platform_session(
            context,
            account.email,
            output_dir,
        )
    except RuntimeError as exc:
        if str(exc) == "console_not_reached":
            raise ClaudeApiRegistrationError("console_not_reached") from exc
        raise
