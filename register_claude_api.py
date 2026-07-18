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
from common import proxy_switch
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
EMERGENCY_TIMEOUT_CUSHION = 5.0
TASK_CANCEL_GRACE = 0.25
CLEANUP_OPERATION_TIMEOUT = 5.0
RETAINED_TASK_DRAIN_TIMEOUT = 0.25
_RETAINED_BACKGROUND_TASKS = set()
_CLAUDE_CHALLENGE_MARKERS = (
    "app-unavailable-in-region",
    "unavailable in your",
    "just a moment",
    "performing security",
)


class ClaudeApiRegistrationError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


async def apply_verification_artifact(page, artifact, navigation_timeout=60000):
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
            await page.goto(magic_link, timeout=navigation_timeout)
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


def _clock():
    return time.monotonic()


def _remaining(deadline):
    return max(0.0, deadline - _clock())


def _close_unawaited(awaitable):
    close = getattr(awaitable, "close", None)
    if close is not None:
        close()


def _consume_background_task(task):
    _RETAINED_BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


def _retain_background_task(task):
    if task.done():
        _consume_background_task(task)
        return
    _RETAINED_BACKGROUND_TASKS.add(task)
    task.add_done_callback(_consume_background_task)


async def _cancel_task_bounded(task, cancel_grace):
    grace = max(0.0, float(cancel_grace))
    task.cancel()
    try:
        done, _pending = await asyncio.wait({task}, timeout=grace)
        if task in done:
            _consume_background_task(task)
            return
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=grace)
        if task in done:
            _consume_background_task(task)
            return
    except asyncio.CancelledError:
        task.cancel()
        _retain_background_task(task)
        raise
    _retain_background_task(task)


async def _run_bounded(awaitable, timeout, *, cancel_grace=None):
    if cancel_grace is None:
        cancel_grace = TASK_CANCEL_GRACE
    try:
        task = asyncio.ensure_future(awaitable)
    except BaseException:
        _close_unawaited(awaitable)
        raise
    try:
        done, _pending = await asyncio.wait(
            {task}, timeout=max(0.0, float(timeout))
        )
    except asyncio.CancelledError:
        await _cancel_task_bounded(task, cancel_grace)
        raise
    if task in done:
        return task.result()
    await _cancel_task_bounded(task, cancel_grace)
    raise asyncio.TimeoutError


async def _drain_retained_tasks(timeout=RETAINED_TASK_DRAIN_TIMEOUT):
    for task in tuple(_RETAINED_BACKGROUND_TASKS):
        if task.done():
            _consume_background_task(task)
    tasks = {
        task for task in _RETAINED_BACKGROUND_TASKS if not task.done()
    }
    if not tasks:
        return
    done, _pending = await asyncio.wait(
        tasks, timeout=max(0.0, float(timeout))
    )
    for task in done:
        _consume_background_task(task)


async def _await_by_deadline(awaitable, deadline):
    remaining = _remaining(deadline)
    if remaining <= 0:
        _close_unawaited(awaitable)
        raise asyncio.TimeoutError
    return await asyncio.wait_for(awaitable, timeout=remaining)


def _navigation_timeout(deadline):
    return max(1, min(60000, int(_remaining(deadline) * 1000)))


async def _raise_post_verification_timeout(page, method):
    if method == "code":
        try:
            code_input = page.locator('[data-testid="code"]')
            if await code_input.count() == 1 and await code_input.is_visible():
                raise ClaudeApiRegistrationError("verification_rejected")
        except ClaudeApiRegistrationError:
            raise
        except Exception:
            pass
    raise ClaudeApiRegistrationError("console_not_reached")


async def _wait_for_post_verification_state(
    page,
    method,
    timeout=None,
    *,
    deadline=None,
):
    if deadline is None:
        deadline = _clock() + max(0.0, float(timeout or 0.0))
    personal_selected = False
    while True:
        if _remaining(deadline) <= 0:
            await _raise_post_verification_timeout(page, method)
        try:
            console_ready = await _await_by_deadline(
                is_console_ready(page), deadline
            )
        except asyncio.TimeoutError:
            await _raise_post_verification_timeout(page, method)
        if console_ready:
            return
        try:
            rejected = await _await_by_deadline(
                _verification_rejected(page), deadline
            )
        except asyncio.TimeoutError:
            await _raise_post_verification_timeout(page, method)
        if rejected:
            raise ClaudeApiRegistrationError("verification_rejected")

        if not personal_selected:
            try:
                personal_selected = await _await_by_deadline(
                    select_personal_account(page), deadline
                )
            except asyncio.TimeoutError:
                await _raise_post_verification_timeout(page, method)

        remaining = _remaining(deadline)
        if remaining <= 0:
            await _raise_post_verification_timeout(page, method)
        await asyncio.sleep(min(0.05, remaining))


async def run_claude_platform_flow(
    page,
    context,
    account,
    fetch_verification,
    max_wait,
    output_dir="cookies/claude_api",
):
    deadline = _clock() + max(0.0, float(max_wait))

    async def start_registration():
        await page.goto(
            PLATFORM_URL,
            timeout=_navigation_timeout(deadline),
        )
        email = page.locator('[data-testid="email"]')
        submit = page.locator('button[data-testid="continue"]')
        if await email.count() != 1 or await submit.count() != 1:
            raise ClaudeApiRegistrationError("registration_error")
        await email.fill(account.email)
        requested_at = time.time()
        await submit.click()
        return requested_at

    try:
        requested_at = await _await_by_deadline(
            start_registration(), deadline
        )
    except ClaudeApiRegistrationError:
        raise
    except Exception:
        raise ClaudeApiRegistrationError("registration_error") from None

    async def fetch_artifact(received_after):
        remaining = _remaining(deadline)
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await _await_by_deadline(
            fetch_verification(
                context,
                account,
                remaining,
                received_after,
            ),
            deadline,
        )

    try:
        artifact = await fetch_artifact(requested_at)
        if artifact is None:
            resend = page.get_by_role("button", name="Resend email", exact=True)
            if await _await_by_deadline(resend.count(), deadline) != 1:
                raise ClaudeApiRegistrationError("mail_timeout")
            requested_at = time.time()
            await _await_by_deadline(resend.click(), deadline)
            artifact = await fetch_artifact(requested_at)
        if artifact is None:
            raise ClaudeApiRegistrationError("verification_artifact_not_found")
    except ClaudeApiRegistrationError:
        raise
    except Exception:
        raise ClaudeApiRegistrationError("mail_timeout") from None

    try:
        method = await _await_by_deadline(
            apply_verification_artifact(
                page,
                artifact,
                navigation_timeout=_navigation_timeout(deadline),
            ),
            deadline,
        )
        await _wait_for_post_verification_state(
            page,
            method,
            deadline=deadline,
        )
    except ClaudeApiRegistrationError:
        raise
    except Exception:
        raise ClaudeApiRegistrationError("console_not_reached") from None

    try:
        return await _await_by_deadline(
            save_claude_platform_session(
                context,
                account.email,
                output_dir,
            ),
            deadline,
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


async def _cleanup_registration_resources(bb, browser, profile_id):
    cleanup_cancelled = None

    async def attempt(operation, error_code):
        nonlocal cleanup_cancelled
        try:
            awaitable = operation()
            await _run_bounded(
                awaitable,
                CLEANUP_OPERATION_TIMEOUT,
                cancel_grace=TASK_CANCEL_GRACE,
            )
        except asyncio.CancelledError as exc:
            if cleanup_cancelled is None:
                cleanup_cancelled = exc
        except BaseException as exc:
            log_flow_error(error_code, exc)

    if browser is not None:
        await attempt(browser.close, "browser_cleanup_failed")
    if profile_id is not None:
        await attempt(
            lambda: asyncio.to_thread(bb.close_browser, profile_id),
            "profile_close_failed",
        )
        await attempt(
            lambda: asyncio.to_thread(bb.delete_browser, profile_id),
            "profile_delete_failed",
        )
    return cleanup_cancelled


async def register_one(
    bb,
    account,
    account_store,
    timeout,
    account_lease=None,
    browser_proxy_fields=None,
):
    profile_id = None
    browser = None
    cookie_path = None
    error_code = None
    escaped = None
    cancelled = None
    release_on_error = False
    try:
        proxy_fields = (
            bitbrowser_proxy_fields(account_lease)
            if account_lease
            else dict(browser_proxy_fields or {})
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
            cookie_path = await _run_bounded(
                run_claude_platform_flow(
                    page,
                    context,
                    account,
                    functools.partial(
                        fetch_platform_verification,
                        account_lease=account_lease,
                    ),
                    max_wait=timeout,
                ),
                timeout=float(timeout) + EMERGENCY_TIMEOUT_CUSHION,
                cancel_grace=TASK_CANCEL_GRACE,
            )
    except asyncio.TimeoutError:
        error_code = "timeout"
    except (ClaudeApiRegistrationError, NineMallMailboxError) as exc:
        error_code = exc.code
    except asyncio.CancelledError as exc:
        cancelled = exc
    except BaseException as exc:
        escaped = exc
        if profile_id is None:
            release_on_error = True
        else:
            error_code = "registration_error"

    cleanup_cancelled = await _cleanup_registration_resources(
        bb, browser, profile_id
    )
    if cancelled is not None or cleanup_cancelled is not None:
        account_store.release(account)
        raise cancelled or cleanup_cancelled
    if cookie_path is not None:
        account_store.mark_used(account)
        return cookie_path
    if release_on_error:
        account_store.release(account)
    elif error_code is not None:
        account_store.mark_error(account, error_code)
    if escaped is not None:
        raise escaped
    return None


def _proxy_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "--proxy-port must be between 1 and 65535"
        ) from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            "--proxy-port must be between 1 and 65535"
        )
    return port


def _configure_clash_proxy(node, port, *, account_lease, ipmart_enabled):
    if account_lease is not None or ipmart_enabled:
        return {}
    selected = str(node or "none").strip()
    if not selected or selected.lower() == "none":
        return {}
    if selected.lower() == "auto":
        candidates = proxy_switch.concrete_nodes()
        selected = proxy_switch.find_working_node(
            test_url=PLATFORM_URL,
            challenge_markers=_CLAUDE_CHALLENGE_MARKERS,
            candidates=candidates,
            verbose=False,
        )
        if not selected:
            raise RuntimeError("clash_node_unavailable")
    else:
        proxy_switch.set_node(selected)
    return {
        "proxyMethod": 2,
        "proxyType": "http",
        "host": "127.0.0.1",
        "port": str(port),
    }


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
    parser.add_argument("--proxy-port", type=_proxy_port, default=7897)
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
    browser_proxy_fields = {}
    try:
        inherited_lease = lease_from_env()
        ipmart_settings = settings_from_env()
        if inherited_lease is not None or ipmart_settings.enabled:
            strip_http_proxy_env(os.environ)
        browser_proxy_fields = _configure_clash_proxy(
            args.node,
            args.proxy_port,
            account_lease=inherited_lease,
            ipmart_enabled=ipmart_settings.enabled,
        )
        bb = BitBrowser()
    except BaseException as exc:
        for selected in accounts:
            account_store.release(selected)
        log_flow_error("browser_initialization_failed", exc)
        print(f"success: 0/{len(accounts)}")
        return 1

    semaphore = asyncio.Semaphore(args.concurrency)
    ownership = [
        {"handed_to_register": False, "released": False}
        for _selected in accounts
    ]

    def release_once(index, selected):
        state = ownership[index]
        if state["released"] or state["handed_to_register"]:
            return
        state["released"] = True
        account_store.release(selected)

    async def run_selected(index, selected):
        try:
            async with semaphore:
                account_lease = inherited_lease
                if account_lease is None and ipmart_settings.enabled:
                    try:
                        account_lease = await asyncio.to_thread(acquire_proxy)
                    except asyncio.CancelledError:
                        release_once(index, selected)
                        raise
                    except IPMartProxyError as exc:
                        release_once(index, selected)
                        log_flow_error(
                            "proxy_unavailable", exc, account=selected
                        )
                        return None
                    except BaseException as exc:
                        release_once(index, selected)
                        log_flow_error(
                            "proxy_acquisition_failed", exc, account=selected
                        )
                        return None
                ownership[index]["handed_to_register"] = True
                return await register_one(
                    bb,
                    selected,
                    account_store,
                    args.timeout,
                    account_lease=account_lease,
                    browser_proxy_fields=browser_proxy_fields,
                )
        except asyncio.CancelledError:
            release_once(index, selected)
            raise
        except BaseException as exc:
            log_flow_error("registration_error", exc, account=selected)
            return None

    tasks = [
        asyncio.create_task(run_selected(index, selected))
        for index, selected in enumerate(accounts)
    ]
    try:
        results = await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for index, selected in enumerate(accounts):
            release_once(index, selected)
        await _drain_retained_tasks()
        raise

    await _drain_retained_tasks()
    success_count = sum(result is not None for result in results)
    print(f"success: {success_count}/{len(accounts)}")
    return 0 if accounts and success_count == len(accounts) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
