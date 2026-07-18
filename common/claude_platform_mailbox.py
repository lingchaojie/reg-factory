from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse
import asyncio
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


def _validated_platform_link(candidate, allow_safelink=True):
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
    if allow_safelink and (parsed.hostname or "").endswith(
        "safelinks.protection.outlook.com"
    ):
        wrapped = parse_qs(parsed.query).get("url", [""])[0]
        if wrapped:
            return _validated_platform_link(unquote(wrapped), allow_safelink=False)
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
            link = _validated_platform_link(candidate)
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

    token = mailbox._get_access_token(
        refresh_token,
        client_id,
        account_lease=account_lease,
    )
    if not token:
        return None
    start = time.time()
    refreshed = False
    while time.time() - start < max_wait:
        messages = []
        for folder in mailbox.GRAPH_FOLDERS:
            for item in mailbox.fetch_messages(
                token,
                folder,
                top=10,
                account_lease=account_lease,
            ):
                messages.append(ClaudePlatformMessage(
                    sender=item.get("from", ""),
                    subject=item.get("subject", ""),
                    received=item.get("received", ""),
                    body=item.get("body", ""),
                ))
        result = extract_claude_platform_verification(
            messages,
            received_after=received_after,
        )
        if result:
            return result
        elapsed = time.time() - start
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
            )
            if new_token:
                token = new_token
        time.sleep(poll)
    return None


async def _scan_claude_platform_folder(page, received_after=None):
    opened = await page.evaluate("""
        () => {
            const items = document.querySelectorAll('[role="option"], [role="listitem"]');
            for (const item of items) {
                const text = (item.textContent || '').toLowerCase();
                if (text.includes('anthropic') || text.includes('claude')) {
                    item.click();
                    return true;
                }
            }
            return false;
        }
    """)
    if not opened:
        return None
    await asyncio.sleep(2)
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
        received=datetime.now(timezone.utc).isoformat(),
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
    )

    if not await _outlook_login(page, email, password):
        return None
    await page.goto("https://outlook.live.com/mail/0/", timeout=60000)
    start = time.time()
    while time.time() - start < max_wait:
        for names in (INBOX_NAMES, JUNK_NAMES):
            await _click_folder(page, names)
            await asyncio.sleep(2)
            result = await _scan_claude_platform_folder(
                page,
                received_after=received_after,
            )
            if result:
                return result
        await asyncio.sleep(5)
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
            data = await response.json()
    value = data.get("value")
    if not isinstance(value, dict):
        return None
    magic_link = str(value.get("magic_link") or "")
    code = str(value.get("code") or "")
    if not magic_link and not code:
        return None
    return ClaudePlatformVerification(
        magic_link=magic_link,
        code=code,
        received_at=float(value.get("received_at") or 0.0),
    )
