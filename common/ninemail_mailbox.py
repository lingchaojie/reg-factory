from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse
import re
import time

import requests


@dataclass(frozen=True)
class NineMallMessage:
    sender: str
    subject: str
    received: str
    body: str


class NineMallMailboxError(RuntimeError):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


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


def _validated_claude_link(candidate):
    value = unescape(str(candidate or "")).rstrip(".,);]")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host == "claude.ai" and parsed.path.rstrip("/") == "/magic-link" and parsed.fragment:
        return value
    if host.endswith("safelinks.protection.outlook.com"):
        wrapped = parse_qs(parsed.query).get("url", [""])[0]
        if wrapped:
            target = unquote(wrapped)
            target_parsed = urlparse(target)
            if (
                (target_parsed.hostname or "").lower() == "claude.ai"
                and target_parsed.path.rstrip("/") == "/magic-link"
                and target_parsed.fragment
            ):
                return target
    return None


def extract_claude_magic_link(messages, received_after=None):
    candidates = []
    for message in messages:
        sender = message.sender.lower()
        subject = message.subject.lower()
        if not any(key in sender or key in subject for key in ("anthropic", "claude")):
            continue
        received = _received_epoch(message.received)
        if received_after is not None and (
            received is None or received < received_after - 5
        ):
            continue
        candidates.append((received or 0, message))
    for _received, message in sorted(candidates, key=lambda item: item[0], reverse=True):
        text = unescape(" ".join((message.subject, message.body)))
        for candidate in _URL_RE.findall(text):
            link = _validated_claude_link(candidate)
            if link:
                return link
    return None


class NineMallMailboxClient:
    def __init__(
        self,
        base_url,
        api_password="",
        http_timeout=30,
        poll_interval=5,
        session=None,
        sleep=time.sleep,
        clock=time.time,
    ):
        parsed = urlparse(str(base_url or "").strip())
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValueError("NINEMALL_API_BASE must be HTTPS")
        self.url = urljoin(str(base_url).rstrip("/") + "/", "api/mail-all")
        self.api_password = str(api_password or "")
        self.http_timeout = int(http_timeout)
        self.poll_interval = int(poll_interval)
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock

    def _payload(self, account, folder):
        return {
            "refresh_token": account.refresh_token,
            "client_id": account.client_id,
            "email": account.email,
            "mailbox": folder,
            "response_type": "json",
            "password": self.api_password,
        }

    @staticmethod
    def _nothing_to_fetch(payload):
        if not isinstance(payload, dict):
            return False
        data = payload.get("data")
        return isinstance(data, dict) and data.get("error") == "Nothing to fetch"

    @staticmethod
    def _normalize(payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise NineMallMailboxError("invalid_response")
        messages = []
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            messages.append(NineMallMessage(
                sender=str(item.get("send") or ""),
                subject=str(item.get("subject") or ""),
                received=str(item.get("date") or ""),
                body=str(item.get("html") or item.get("text") or ""),
            ))
        return messages

    def fetch_folder(self, account, folder):
        transient_statuses = {429, 500, 502, 503, 504}
        for attempt in range(3):
            try:
                response = self.session.post(
                    self.url,
                    json=self._payload(account, folder),
                    timeout=self.http_timeout,
                )
            except (requests.ConnectionError, requests.Timeout):
                if attempt < 2:
                    self.sleep(attempt + 1)
                    continue
                raise NineMallMailboxError("network_error", retryable=True) from None
            try:
                payload = response.json()
            except (TypeError, ValueError):
                raise NineMallMailboxError("invalid_json") from None
            if response.status_code == 200:
                return self._normalize(payload)
            if response.status_code == 500 and self._nothing_to_fetch(payload):
                return []
            if response.status_code in transient_statuses:
                if attempt < 2:
                    self.sleep(attempt + 1)
                    continue
                raise NineMallMailboxError("transient_http", retryable=True)
            if response.status_code == 400:
                raise NineMallMailboxError("http_400")
            if response.status_code == 401:
                raise NineMallMailboxError("http_401")
            raise NineMallMailboxError("unexpected_http")
        raise NineMallMailboxError("transient_http", retryable=True)

    def poll_magic_link(self, account, max_wait, received_after=None):
        deadline = self.clock() + max(0, max_wait)
        while self.clock() < deadline:
            messages = []
            for folder in ("INBOX", "Junk"):
                try:
                    messages.extend(self.fetch_folder(account, folder))
                except NineMallMailboxError as exc:
                    if not exc.retryable:
                        raise
            link = extract_claude_magic_link(messages, received_after=received_after)
            if link:
                return link
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            self.sleep(min(self.poll_interval, remaining))
        return None
