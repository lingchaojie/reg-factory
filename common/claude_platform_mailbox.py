from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse
import asyncio
import math
import os
import re
import time

import aiohttp


@dataclass(frozen=True)
class ClaudePlatformMessage:
    sender: str
    subject: str
    received: str
    body: str


@dataclass(frozen=True)
class ClaudePlatformVerification:
    magic_link: str = ""
    code: str = ""
    received_at: float = 0.0


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"style", "script"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"style", "script"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


def visible_text(value):
    parser = _VisibleTextParser()
    parser.feed(str(value or ""))
    return unescape(" ".join(parser.parts))


def _received_epoch(value, *, end_of_precision=False):
    text = str(value or "").strip()
    if not text:
        return None
    minute_precision = False
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None and math.isfinite(numeric) and numeric >= 0:
        if numeric >= 100_000_000_000:
            numeric /= 1000.0
        return numeric
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            accessible = re.sub(r"\s+at\s+", " ", text, flags=re.IGNORECASE)
            for format_string in (
                "%A, %B %d, %Y %I:%M %p",
                "%B %d, %Y %I:%M %p",
                "%m/%d/%Y %I:%M %p",
            ):
                try:
                    parsed = datetime.strptime(accessible, format_string)
                    parsed = parsed.replace(
                        tzinfo=datetime.now().astimezone().tzinfo
                    )
                    minute_precision = True
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    received = parsed.timestamp()
    if minute_precision and end_of_precision:
        received += 59.999999
    return received


_URL_RE = re.compile(r"https://[^\s\"'<>]+", re.IGNORECASE)
_CODE_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s\"'<>]+",
    re.IGNORECASE,
)
_CODE_LABEL = r"(?:verification|login|sign[ -]?in)\s+code"
_CODE_PATTERNS = (
    re.compile(
        rf"\b{_CODE_LABEL}\b(?:\s+(?:is\s+)?|\s*[:=-]\s*)"
        r"(?<![0-9])([0-9]{4,10})(?![0-9])",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<![0-9])([0-9]{{4,10}})(?![0-9])\s+"
        rf"(?:is\s+)?(?:your\s+)?"
        rf"{_CODE_LABEL}\b",
        re.IGNORECASE,
    ),
)
_REJECTED_CODE_PREFIX = re.compile(
    r"\b(?:account[- ]+ending|date|expir(?:ed|es?|y|ation)|reference|phone|order)"
    r"[\s:=-]*$",
    re.IGNORECASE,
)
_RAW_CODE_RE = re.compile(r"[0-9]{4,10}")


def validate_claude_platform_magic_link(candidate, allow_safelink=True):
    value = unescape(str(candidate or "")).rstrip(".,);]")
    if re.search(r"[\x00-\x1f\x7f]", value):
        return ""
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme == "https"
        and parsed.hostname == "platform.claude.com"
        and port is None
        and parsed.username is None
        and parsed.password is None
    ):
        if parsed.path == "/magic-link":
            return value
    safelink_host = "safelinks.protection.outlook.com"
    hostname = parsed.hostname or ""
    if (
        allow_safelink
        and parsed.scheme == "https"
        and (hostname == safelink_host or hostname.endswith("." + safelink_host))
        and port is None
        and parsed.username is None
        and parsed.password is None
    ):
        wrapped = parse_qs(parsed.query).get("url", [""])[0]
        if wrapped:
            return validate_claude_platform_magic_link(
                unquote(wrapped),
                allow_safelink=False,
            )
    return ""


def validate_claude_platform_code(candidate):
    value = str(candidate or "").strip()
    return value if _RAW_CODE_RE.fullmatch(value) else ""


def _verification_code(subject, body):
    text = " ".join((visible_text(subject), visible_text(body)))
    text = _CODE_URL_RE.sub(" ", text)
    for pattern in _CODE_PATTERNS:
        for match in pattern.finditer(text):
            prefix = text[max(0, match.start() - 40):match.start()]
            if _REJECTED_CODE_PREFIX.search(prefix):
                continue
            return match.group(1)
    return ""


def validate_claude_platform_verification(
    magic_link,
    code,
    received_at,
    received_after=None,
):
    received = _received_epoch(received_at)
    if received is None:
        return None
    if received_after is not None:
        try:
            baseline = float(received_after)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(baseline) or baseline < 0:
            return None
        received_latest = _received_epoch(
            received_at, end_of_precision=True
        )
        if received_latest < baseline - 5:
            return None
    link = validate_claude_platform_magic_link(magic_link)
    numeric_code = validate_claude_platform_code(code)
    if not link and not numeric_code:
        return None
    return ClaudePlatformVerification(
        magic_link=link,
        code=numeric_code,
        received_at=received,
    )


def extract_claude_platform_verification(messages, received_after=None):
    candidates = []
    for message in messages:
        identity = f"{message.sender} {message.subject}".lower()
        if not any(key in identity for key in ("anthropic", "claude")):
            continue
        received = _received_epoch(message.received)
        received_latest = _received_epoch(
            message.received, end_of_precision=True
        )
        if received_after is not None and (
            received_latest is None
            or received_latest < received_after - 5
        ):
            continue
        candidates.append((received or 0.0, message))
    for received, message in sorted(
        candidates, reverse=True, key=lambda item: item[0]
    ):
        combined = unescape(f"{message.subject} {message.body}")
        link = ""
        for candidate in _URL_RE.findall(combined):
            link = validate_claude_platform_magic_link(candidate)
            if link:
                break
        code = _verification_code(message.subject, message.body)
        if link or code:
            return ClaudePlatformVerification(link, code, received)
    return None


def get_claude_platform_verification_by_token(
    email,
    refresh_token,
    client_id,
    max_wait=120,
    poll=5,
    received_after=None,
    account_lease=None,
):
    from common import mailbox

    start = time.monotonic()
    deadline = start + max(0.0, float(max_wait))
    token = mailbox._get_access_token(
        refresh_token,
        client_id,
        account_lease=account_lease,
        deadline=deadline,
    )
    if not token or time.monotonic() >= deadline:
        return None
    refreshed = False
    while time.monotonic() < deadline:
        messages = []
        for folder in mailbox.GRAPH_FOLDERS:
            if time.monotonic() >= deadline:
                return None
            for item in mailbox.fetch_messages(
                token,
                folder,
                top=10,
                account_lease=account_lease,
                deadline=deadline,
            ):
                messages.append(ClaudePlatformMessage(
                    sender=item.get("from", ""),
                    subject=item.get("subject", ""),
                    received=item.get("received", ""),
                    body=item.get("body", ""),
                ))
            if time.monotonic() >= deadline:
                return None
        result = extract_claude_platform_verification(
            messages,
            received_after=received_after,
        )
        if result:
            return result
        elapsed = time.monotonic() - start
        print(
            "  [mail] waiting for Claude Platform verification "
            f"(inbox+junk)... ({int(elapsed)}s/{max_wait}s)"
        )
        if not refreshed and elapsed > max_wait / 2:
            refreshed = True
            new_token = mailbox._get_access_token(
                refresh_token,
                client_id,
                account_lease=account_lease,
                deadline=deadline,
            )
            if new_token:
                token = new_token
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(float(poll), remaining))
    return None


async def _scan_claude_platform_folder(
    page,
    received_after=None,
    deadline=None,
    seen_ids=None,
):
    candidates = await page.evaluate("""
        () => {
            const items = Array.from(
                document.querySelectorAll('[role="option"], [role="listitem"]')
            );
            return items.map((item, index) => {
                const text = (item.textContent || '').toLowerCase();
                if (!text.includes('anthropic') && !text.includes('claude')) return null;
                const style = window.getComputedStyle(item);
                const rect = item.getBoundingClientRect();
                const visible = style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    rect.width > 0 && rect.height > 0;
                const time = item.querySelector(
                    'time, [data-testid*="time"], [data-testid*="date"]'
                );
                const received = (time && time.getAttribute('datetime')) ||
                    item.getAttribute('data-received') ||
                    item.getAttribute('data-timestamp') ||
                    item.getAttribute('data-time') ||
                    (time && time.getAttribute('aria-label')) ||
                    (time && time.getAttribute('title')) ||
                    (time && time.textContent) || '';
                const messageId = item.getAttribute('data-item-id') ||
                    item.getAttribute('data-id') || item.id;
                const conversationId = item.getAttribute('data-convid');
                const stableId = messageId || (conversationId
                    ? `${conversationId}|${received}|${text.trim()}`
                    : `${received}|${text.trim()}`);
                return {index, visible, received, stable_id: stableId};
            }).filter(Boolean);
        }
    """)
    if not isinstance(candidates, list):
        return None
    visible = [
        item
        for item in candidates
        if isinstance(item, dict) and item.get("visible")
    ]
    if not visible:
        return None

    inspected = seen_ids if seen_ids is not None else set()
    eligible = []
    for item in visible:
        stable_id = str(item.get("stable_id") or "")
        if stable_id in inspected:
            continue
        received_epoch = _received_epoch(item.get("received"))
        received_latest = _received_epoch(
            item.get("received"), end_of_precision=True
        )
        if received_epoch is None:
            if received_after is not None:
                continue
        elif (
            received_after is not None
            and received_latest < float(received_after) - 5
        ):
            continue
        eligible.append((received_epoch, item))
    if not eligible:
        return None
    if all(entry[0] is not None for entry in eligible):
        eligible.sort(
            key=lambda entry: (
                -(entry[0] or 0.0),
                int(entry[1].get("index") or 0),
            )
        )
    else:
        eligible.sort(key=lambda entry: int(entry[1].get("index") or 0))

    from common.mailbox import _async_sleep_bounded

    for received_epoch, selected in eligible:
        selection = {
            "stable_id": str(selected.get("stable_id") or ""),
            "index": int(selected.get("index") or 0),
        }
        opened = await page.evaluate(
            """(selection) => {
            const items = Array.from(
                document.querySelectorAll('[role="option"], [role="listitem"]')
            );
            const identify = (item) => {
                const text = (item.textContent || '').toLowerCase();
                const time = item.querySelector(
                    'time, [data-testid*="time"], [data-testid*="date"]'
                );
                const received = (time && time.getAttribute('datetime')) ||
                    item.getAttribute('data-received') ||
                    item.getAttribute('data-timestamp') ||
                    item.getAttribute('data-time') ||
                    (time && time.getAttribute('aria-label')) ||
                    (time && time.getAttribute('title')) ||
                    (time && time.textContent) || '';
                const messageId = item.getAttribute('data-item-id') ||
                    item.getAttribute('data-id') || item.id;
                const conversationId = item.getAttribute('data-convid');
                const stableId = messageId || (conversationId
                    ? `${conversationId}|${received}|${text.trim()}`
                    : `${received}|${text.trim()}`);
                return {text, received, stable_id: stableId};
            };
            const item = items.find(
                candidate => identify(candidate).stable_id === selection.stable_id
            );
            if (!item) return false;
            const current = identify(item);
            const style = window.getComputedStyle(item);
            const rect = item.getBoundingClientRect();
            if ((!current.text.includes('anthropic') &&
                !current.text.includes('claude')) ||
                style.display === 'none' || style.visibility === 'hidden' ||
                rect.width <= 0 || rect.height <= 0) return false;
            item.click();
            return {
                stable_id: current.stable_id,
                received: current.received,
            };
        }""",
            selection,
        )
        if (
            not isinstance(opened, dict)
            or str(opened.get("stable_id") or "") != selection["stable_id"]
        ):
            continue
        inspected.add(selection["stable_id"])
        clicked_received = str(opened.get("received") or "")
        clicked_epoch = _received_epoch(clicked_received)
        clicked_latest = _received_epoch(
            clicked_received, end_of_precision=True
        )
        if received_after is not None and (
            clicked_latest is None
            or clicked_latest < float(received_after) - 5
        ):
            continue
        if not await _async_sleep_bounded(2, deadline):
            return None
        payload = await page.evaluate("""
        () => {
            const pane = document.querySelector(
                '[aria-label="Reading Pane"], [data-app-section="ReadingPane"], '
                + '[data-testid="reading-pane"], [role="main"]'
            ) || document.body;
            const links = Array.from(pane.querySelectorAll('a[href^="https://"]'))
                .map(a => a.href).join(' ');
            return {
                subject: pane.querySelector('h1, h2, [role="heading"]')?.textContent || '',
                body: `${pane.innerText || ''} ${links}`,
            };
        }
        """)
        if not isinstance(payload, dict):
            continue
        received = (
            clicked_received
            if clicked_epoch is not None
            else "1970-01-01T00:00:00+00:00"
        )
        message = ClaudePlatformMessage(
            sender="claude",
            subject=str(payload.get("subject") or ""),
            received=received,
            body=str(payload.get("body") or ""),
        )
        result = extract_claude_platform_verification(
            [message],
            received_after=received_after,
        )
        if result:
            # Keep minute-only freshness intact after numeric broker JSON.
            if (
                clicked_latest is not None
                and clicked_latest > result.received_at
            ):
                return ClaudePlatformVerification(
                    magic_link=result.magic_link,
                    code=result.code,
                    received_at=clicked_latest,
                )
            return result
    return None


async def get_claude_platform_verification_outlook_pw(
    page,
    email,
    password,
    max_wait=120,
    received_after=None,
):
    from common.mailbox import (
        _click_folder,
        _outlook_login,
        INBOX_NAMES,
        JUNK_NAMES,
        _async_sleep_bounded,
    )

    deadline = time.monotonic() + max(0.0, float(max_wait))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        logged_in = await asyncio.wait_for(
            _outlook_login(page, email, password, deadline=deadline),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        return None
    if not logged_in:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    navigation_timeout = max(1, min(60000, int(remaining * 1000)))
    try:
        await asyncio.wait_for(
            page.goto(
                "https://outlook.live.com/mail/0/",
                timeout=navigation_timeout,
            ),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        return None
    seen_ids = set()
    while time.monotonic() < deadline:
        for names in (INBOX_NAMES, JUNK_NAMES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(
                    _click_folder(page, names),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return None
            if not await _async_sleep_bounded(2, deadline):
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                result = await asyncio.wait_for(
                    _scan_claude_platform_folder(
                        page,
                        received_after=received_after,
                        deadline=deadline,
                        seen_ids=seen_ids,
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return None
            if result:
                return result
        if not await _async_sleep_bounded(5, deadline):
            break
    return None


async def fetch_claude_platform_from_broker(
    email,
    password,
    max_wait=120,
    received_after=None,
):
    base = os.environ.get("MAILBOX_BROKER")
    if not base:
        return None
    try:
        wait_budget = float(max_wait)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(wait_budget) or wait_budget <= 0:
        return None
    payload = {
        "email": email,
        "password": password,
        "sender_hint": ["anthropic", "claude"],
        "subject_hint": ["code", "verify", "sign in", "login"],
        "regex": "",
        "kind": "claude_platform",
        "timeout": max_wait,
        "received_after": received_after,
    }
    timeout = aiohttp.ClientTimeout(total=wait_budget)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                base.rstrip("/") + "/fetch",
                json=payload,
            ) as response:
                if not 200 <= int(response.status) < 300:
                    return None
                data = await response.json()
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("ok") is not True:
        return None
    value = data.get("value")
    if not isinstance(value, dict):
        return None
    return validate_claude_platform_verification(
        value.get("magic_link"),
        value.get("code"),
        value.get("received_at"),
        received_after=received_after,
    )
