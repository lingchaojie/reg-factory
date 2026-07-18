from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse
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
_NINEMALL_HOST = "www.appleemail.top"
_NINEMALL_MAIL_ALL_URL = f"https://{_NINEMALL_HOST}/api/mail-all"


def _validated_claude_link(candidate):
    value = unescape(str(candidate or "")).rstrip(".,);]")
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if host == "claude.ai" and parsed.path.rstrip("/") == "/magic-link" and parsed.fragment:
        return value
    if host.endswith("safelinks.protection.outlook.com"):
        wrapped = parse_qs(parsed.query).get("url", [""])[0]
        if wrapped:
            target = unquote(wrapped)
            try:
                target_parsed = urlparse(target)
                target_host = (target_parsed.hostname or "").lower()
            except ValueError:
                return None
            if (
                target_host == "claude.ai"
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
        max_attempts=3,
        session=None,
        sleep=time.sleep,
        clock=time.time,
    ):
        try:
            parsed = urlparse(str(base_url or "").strip())
            valid_base = (
                parsed.scheme.lower() == "https"
                and (parsed.hostname or "").lower() == _NINEMALL_HOST
                and parsed.port is None
                and parsed.username is None
                and parsed.password is None
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid_base = False
        if not valid_base:
            raise ValueError("NINEMALL_API_BASE must be the exact HTTPS AppleEmail origin")
        self.url = _NINEMALL_MAIL_ALL_URL
        self.api_password = str(api_password or "")
        try:
            self.http_timeout = float(http_timeout)
            self.poll_interval = float(poll_interval)
            self.max_attempts = int(max_attempts)
        except (TypeError, ValueError):
            raise ValueError(
                "NINEMALL timeout, poll interval, and retry count must be positive"
            ) from None
        if (
            self.http_timeout <= 0
            or self.poll_interval <= 0
            or self.max_attempts <= 0
        ):
            raise ValueError(
                "NINEMALL timeout, poll interval, and retry count must be positive"
            )
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

    def _stopped(self, deadline=None, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return True
        return deadline is not None and self.clock() >= deadline

    def _sleep_bounded(self, seconds, deadline=None, cancel_event=None):
        duration = float(seconds)
        if deadline is not None:
            duration = min(duration, max(0.0, deadline - self.clock()))
        if duration <= 0 or self._stopped(deadline, cancel_event):
            return False
        if cancel_event is not None and hasattr(cancel_event, "wait"):
            if cancel_event.wait(duration):
                return False
        else:
            self.sleep(duration)
        return not self._stopped(deadline, cancel_event)

    def fetch_folder(
        self,
        account,
        folder,
        *,
        deadline=None,
        cancel_event=None,
    ):
        for attempt in range(self.max_attempts):
            if self._stopped(deadline, cancel_event):
                return []
            timeout = self.http_timeout
            if deadline is not None:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return []
                timeout = min(timeout, remaining)
            try:
                response = self.session.post(
                    self.url,
                    json=self._payload(account, folder),
                    timeout=timeout,
                    allow_redirects=False,
                )
            except requests.RequestException:
                if self._stopped(deadline, cancel_event):
                    return []
                if attempt + 1 < self.max_attempts and self._sleep_bounded(
                    attempt + 1, deadline, cancel_event
                ):
                    continue
                if self._stopped(deadline, cancel_event):
                    return []
                raise NineMallMailboxError("network_error", retryable=True) from None

            status = int(response.status_code)
            if 300 <= status < 400:
                raise NineMallMailboxError("unexpected_http")
            if status == 400:
                raise NineMallMailboxError("http_400")
            if status == 401:
                raise NineMallMailboxError("http_401")
            if status == 403:
                raise NineMallMailboxError("http_403")
            if status == 429 or 500 <= status < 600:
                if status == 500:
                    try:
                        payload = response.json()
                    except (TypeError, ValueError):
                        payload = None
                    if self._nothing_to_fetch(payload):
                        return []
                if self._stopped(deadline, cancel_event):
                    return []
                if attempt + 1 < self.max_attempts and self._sleep_bounded(
                    attempt + 1, deadline, cancel_event
                ):
                    continue
                if self._stopped(deadline, cancel_event):
                    return []
                raise NineMallMailboxError("transient_http", retryable=True)
            if 200 <= status < 300:
                try:
                    payload = response.json()
                except (TypeError, ValueError):
                    raise NineMallMailboxError("invalid_json") from None
                return self._normalize(payload)
            raise NineMallMailboxError("unexpected_http")
        raise NineMallMailboxError("transient_http", retryable=True)

    def poll_magic_link(
        self,
        account,
        max_wait,
        received_after=None,
        *,
        cancel_event=None,
    ):
        try:
            wait_budget = float(max_wait)
        except (TypeError, ValueError):
            raise ValueError("max_wait must be positive") from None
        if wait_budget <= 0:
            raise ValueError("max_wait must be positive")
        deadline = self.clock() + wait_budget
        while not self._stopped(deadline, cancel_event):
            messages = []
            for folder in ("INBOX", "Junk"):
                if self._stopped(deadline, cancel_event):
                    return None
                try:
                    messages.extend(self.fetch_folder(
                        account,
                        folder,
                        deadline=deadline,
                        cancel_event=cancel_event,
                    ))
                except NineMallMailboxError as exc:
                    if not exc.retryable:
                        raise
                if self._stopped(deadline, cancel_event):
                    return None
            link = extract_claude_magic_link(messages, received_after=received_after)
            if link:
                return link
            if not self._sleep_bounded(
                self.poll_interval, deadline, cancel_event
            ):
                break
        return None
