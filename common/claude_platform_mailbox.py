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


def _received_epoch(value):
    text = str(value or "").strip()
    if not text:
        return None
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
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


_URL_RE = re.compile(r"https://[^\s\"'<>]+", re.IGNORECASE)
_CODE_PATTERNS = (
    re.compile(
        r"(?:verification|login|sign[ -]?in)\s+code\D{0,24}(?<!\d)(\d{4,10})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\d)(\d{4,10})(?!\d)\D{0,24}(?:verification|login|sign[ -]?in)\s+code",
        re.IGNORECASE,
    ),
)


def validate_claude_platform_magic_link(candidate, allow_safelink=True):
    value = unescape(str(candidate or "")).rstrip(".,);]")
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


def _verification_code(subject, body):
    text = " ".join((visible_text(subject), visible_text(body)))
    for pattern in _CODE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def extract_claude_platform_verification(messages, received_after=None):
    candidates = []
    for message in messages:
        identity = f"{message.sender} {message.subject}".lower()
        if not any(key in identity for key in ("anthropic", "claude")):
            continue
        received = _received_epoch(message.received)
        if received_after is not None and (
            received is None or received < received_after - 5
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
                const time = item.querySelector('time[datetime]');
                const received = (time && time.getAttribute('datetime')) ||
                    item.getAttribute('data-received') ||
                    item.getAttribute('data-timestamp') ||
                    item.getAttribute('data-time') || '';
                const stableId = item.getAttribute('data-convid') ||
                    item.getAttribute('data-item-id') ||
                    item.getAttribute('data-id') || item.id || String(index);
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

    timestamped = []
    for item in visible:
        received_epoch = _received_epoch(item.get("received"))
        if received_epoch is not None:
            timestamped.append((received_epoch, item))
    if len(timestamped) == len(visible):
        _received, selected = max(
            timestamped,
            key=lambda item: (
                item[0],
                -int(item[1].get("index") or 0),
            ),
        )
        received = str(selected.get("received") or "")
    else:
        selected = min(
            visible,
            key=lambda item: int(item.get("index") or 0),
        )
        if _received_epoch(selected.get("received")) is None:
            if received_after is not None:
                return None
            received = "1970-01-01T00:00:00+00:00"
        else:
            received = str(selected.get("received") or "")

    opened = await page.evaluate(
        """(index) => {
            const items = Array.from(
                document.querySelectorAll('[role="option"], [role="listitem"]')
            );
            const item = items[index];
            if (!item) return false;
            const text = (item.textContent || '').toLowerCase();
            const style = window.getComputedStyle(item);
            const rect = item.getBoundingClientRect();
            if ((!text.includes('anthropic') && !text.includes('claude')) ||
                style.display === 'none' || style.visibility === 'hidden' ||
                rect.width <= 0 || rect.height <= 0) return false;
            item.click();
            return true;
        }""",
        int(selected.get("index") or 0),
    )
    if not opened:
        return None
    from common.mailbox import _async_sleep_bounded

    if not await _async_sleep_bounded(2, deadline):
        return None
    payload = await page.evaluate("""
        () => {
            const pane = document.querySelector('[role="main"]') || document.body;
            const links = Array.from(pane.querySelectorAll('a[href^="https://"]'))
                .map(a => a.href).join(' ');
            return {
                subject: pane.querySelector('h1, h2, [role="heading"]')?.textContent || '',
                body: `${pane.innerText || ''} ${links}`,
            };
        }
    """)
    if not isinstance(payload, dict):
        return None
    message = ClaudePlatformMessage(
        sender="claude",
        subject=str(payload.get("subject") or ""),
        received=received,
        body=str(payload.get("body") or ""),
    )
    return extract_claude_platform_verification(
        [message],
        received_after=received_after,
    )


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
):
    base = os.environ.get("MAILBOX_BROKER")
    if not base:
        return None
    payload = {
        "email": email,
        "password": password,
        "sender_hint": ["anthropic", "claude"],
        "subject_hint": ["code", "verify", "sign in", "login"],
        "regex": "",
        "kind": "claude_platform",
        "timeout": max_wait,
    }
    timeout = aiohttp.ClientTimeout(total=max_wait + 60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            base.rstrip("/") + "/fetch",
            json=payload,
        ) as response:
            if not 200 <= int(response.status) < 300:
                return None
            data = await response.json()
    if not isinstance(data, dict) or data.get("ok") is not True:
        return None
    value = data.get("value")
    if not isinstance(value, dict):
        return None
    magic_link = str(value.get("magic_link") or "")
    code = str(value.get("code") or "")
    if not magic_link and not code:
        return None
    try:
        received_at = float(value.get("received_at") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(received_at):
        return None
    return ClaudePlatformVerification(
        magic_link=magic_link,
        code=code,
        received_at=received_at,
    )
