from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse
import re


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
        if parsed.path.rstrip("/") == "/magic-link":
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
