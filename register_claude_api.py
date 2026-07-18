from urllib.parse import urlparse
import argparse
import asyncio
import functools
import os
import sys
import threading
import time

from playwright.async_api import async_playwright

from bitbrowser import BitBrowser
from common.account_proxy import (
    bitbrowser_proxy_fields,
    lease_from_env,
    strip_http_proxy_env,
)
from common.claude_email_accounts import (
    ClaudeEmailAccount,
    ClaudeEmailAccountStore,
    normalize_email_provider,
)
from common.claude_platform_mailbox import validate_claude_platform_magic_link
from common.claude_platform_mailbox import (
    fetch_claude_platform_from_broker,
    get_claude_platform_verification_by_token,
    get_claude_platform_verification_outlook_pw,
)
from common.claude_platform_session import save_claude_platform_session
from common.ipmart_proxy import IPMartProxyError, acquire_proxy, settings_from_env
from common.ninemail_mailbox import NineMallMailboxClient, NineMallMailboxError
from config import (
    EMAIL_PROVIDER,
    NINEMALL_API_BASE,
    NINEMALL_API_PASSWORD,
    NINEMALL_EMAIL_FILE,
    NINEMALL_HTTP_TIMEOUT,
    NINEMALL_POLL_INTERVAL,
)


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


def build_ninemail_client():
    return NineMallMailboxClient(
        base_url=NINEMALL_API_BASE,
        api_password=NINEMALL_API_PASSWORD,
        http_timeout=NINEMALL_HTTP_TIMEOUT,
        poll_interval=NINEMALL_POLL_INTERVAL,
    )


async def confirm_worker_stopped(worker, cancel_event):
    cancel_event.set()
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel_event.set()
        except BaseException:
            break
    if worker.done():
        try:
            worker.result()
        except BaseException:
            pass


async def fetch_platform_verification(
    context,
    account,
    max_wait,
    received_after,
    account_lease=None,
    ninemail_client=None,
):
    if account.provider == "NINEMALL":
        client = ninemail_client or build_ninemail_client()
        cancel_event = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(
            client.poll_claude_platform_verification,
            account,
            max_wait,
            received_after,
            cancel_event=cancel_event,
        ))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel_event.set()
            await confirm_worker_stopped(worker, cancel_event)
            raise

    if account.refresh_token:
        result = await asyncio.to_thread(
            get_claude_platform_verification_by_token,
            account.email,
            account.refresh_token,
            account.client_id,
            max_wait,
            5,
            received_after,
            account_lease,
        )
        if result:
            return result
    if os.environ.get("MAILBOX_BROKER"):
        result = await fetch_claude_platform_from_broker(
            account.email,
            account.password,
            max_wait,
        )
        if result:
            return result
    outlook_page = await context.new_page()
    try:
        return await get_claude_platform_verification_outlook_pw(
            outlook_page,
            account.email,
            account.password,
            max_wait=max_wait,
            received_after=received_after,
        )
    finally:
        await outlook_page.close()


def log_flow_error(code, error=None, *, account=None):
    del error, account
    value = str(code or "registration_error").strip().lower()
    if not value or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in value
    ):
        value = "registration_error"
    print(value)


async def register_one(
    bb,
    account,
    account_store,
    timeout,
    account_lease=None,
):
    profile_id = None
    browser = None
    try:
        proxy_fields = (
            bitbrowser_proxy_fields(account_lease) if account_lease else {}
        )
        profile_id = bb.create_browser(
            name=f"claude_api_{int(time.time())}",
            **proxy_fields,
        )
        opened = bb.open_browser(profile_id)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(opened["ws"])
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            cookie_path = await asyncio.wait_for(
                run_claude_platform_flow(
                    page,
                    context,
                    account,
                    functools.partial(
                        fetch_platform_verification,
                        account_lease=account_lease,
                    ),
                    max_wait=min(120, timeout),
                ),
                timeout=timeout,
            )
        account_store.mark_used(account)
        return cookie_path
    except asyncio.TimeoutError:
        account_store.mark_error(account, "timeout")
        return None
    except (ClaudeApiRegistrationError, NineMallMailboxError) as exc:
        account_store.mark_error(account, exc.code)
        return None
    except asyncio.CancelledError:
        account_store.release(account)
        raise
    except BaseException:
        if profile_id is None:
            account_store.release(account)
        else:
            account_store.mark_error(account, "registration_error")
        raise
    finally:
        try:
            if browser is not None:
                await browser.close()
        finally:
            if profile_id is not None:
                try:
                    bb.close_browser(profile_id)
                finally:
                    bb.delete_browser(profile_id)


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Claude Platform registration"
    )
    parser.add_argument("--count", "-n", type=int, default=1)
    parser.add_argument("--concurrency", "-c", type=int, default=1)
    parser.add_argument("--timeout", "-t", type=int, default=480)
    parser.add_argument("--emails", "-e")
    parser.add_argument("--email")
    parser.add_argument("--password", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--client-id", default="")
    parser.add_argument("--node", default="none")
    parser.add_argument("--proxy-port", default="7897")
    return parser


def _prepare_accounts(args, provider):
    source = args.emails or (
        NINEMALL_EMAIL_FILE if provider == "NINEMALL" else "emails.txt"
    )
    account_store = ClaudeEmailAccountStore(
        provider=provider,
        source_file=source,
        purpose="claude_api",
    )
    if args.email:
        if provider == "NINEMALL" and (
            not args.token or not args.client_id
        ):
            raise SystemExit(
                "NINEMALL --email requires --token and --client-id"
            )
        selected = ClaudeEmailAccount(
            provider=provider,
            email=args.email.strip(),
            password=(args.password or "").strip(),
            client_id=(args.client_id or "").strip(),
            refresh_token=(args.token or "").strip(),
        )
        return [selected], account_store
    limit = None if args.emails else args.count
    return account_store.reserve_many(limit=limit), account_store


async def main():
    parser = _build_parser()
    args = parser.parse_args()
    if args.count <= 0:
        parser.error("--count must be greater than zero")
    if args.concurrency <= 0:
        parser.error("--concurrency must be greater than zero")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    provider = normalize_email_provider(EMAIL_PROVIDER)
    accounts, account_store = _prepare_accounts(args, provider)
    inherited_lease = None
    ipmart_settings = None
    try:
        inherited_lease = lease_from_env()
        ipmart_settings = settings_from_env()
        if inherited_lease is not None or ipmart_settings.enabled:
            strip_http_proxy_env(os.environ)
        bb = BitBrowser()
    except BaseException as exc:
        for selected in accounts:
            account_store.release(selected)
        log_flow_error("browser_initialization_failed", exc)
        print(f"success: 0/{len(accounts)}")
        return 1

    semaphore = asyncio.Semaphore(args.concurrency)

    async def run_selected(selected):
        async with semaphore:
            account_lease = inherited_lease
            if account_lease is None and ipmart_settings.enabled:
                try:
                    account_lease = await asyncio.to_thread(acquire_proxy)
                except IPMartProxyError as exc:
                    account_store.release(selected)
                    log_flow_error("proxy_unavailable", exc, account=selected)
                    return None
                except BaseException as exc:
                    account_store.release(selected)
                    log_flow_error(
                        "proxy_acquisition_failed", exc, account=selected
                    )
                    return None
            try:
                return await register_one(
                    bb,
                    selected,
                    account_store,
                    args.timeout,
                    account_lease=account_lease,
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                log_flow_error("registration_error", exc, account=selected)
                return None

    tasks = [asyncio.create_task(run_selected(selected)) for selected in accounts]
    try:
        results = await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        for selected in accounts:
            account_store.release(selected)

    success_count = sum(result is not None for result in results)
    print(f"success: {success_count}/{len(accounts)}")
    return 0 if accounts and success_count == len(accounts) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
