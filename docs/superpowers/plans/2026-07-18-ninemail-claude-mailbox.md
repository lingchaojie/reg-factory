# NINEMALL Claude Mailbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make NINEMALL the default Claude mailbox channel, consume its four-column `mail.txt` format, and fetch Claude magic links through AppleEmail's hosted POST API without opening Outlook.

**Architecture:** Add a typed Claude mailbox account/store module and an isolated NINEMALL HTTP client. Route every Claude magic-link read through one helper in `register.py`; preserve the existing Outlook path and restrict NINEMALL pool consumption in orchestrators to Claude-only runs.

**Tech Stack:** Python 3, `requests`, `asyncio`, `unittest`, existing FastAPI WebUI metadata.

## Global Constraints

- `EMAIL_PROVIDER` is unset/empty-safe and defaults to the exact value `NINEMALL`; the only other accepted value is `OUTLOOK`.
- NINEMALL source rows are exactly `email----password----client_id----refresh_token`.
- OUTLOOK source rows remain `email----password----refresh_token----client_id`.
- NINEMALL uses HTTPS POST to `{NINEMALL_API_BASE}/api/mail-all`; never put credentials in a URL.
- `mail.txt` is read-only. Ignore `new_refresh_token` in every API response and never rewrite the source file.
- A NINEMALL Claude run never opens Outlook, invokes Graph mailbox reads, invokes `mailbox_broker`, or falls back to a browser mailbox.
- Only Claude changes. ChatGPT, Grok, GitHub, Gmail, and mixed-platform mailbox behavior remain unchanged.
- Never print or persist passwords, client IDs, refresh tokens, API passwords, request bodies, full API responses, or complete magic links.
- Automated tests must use temporary files and fake responses; they must not call AppleEmail, Microsoft, a proxy provider, or any live account.
- Add no dependency; use the standard library plus the repository's existing `requests` dependency.

---

### Task 1: Claude Mailbox Configuration And Typed Account Store

**Files:**
- Create: `common/claude_email_accounts.py`
- Create: `tests/test_claude_email_accounts.py`
- Modify: `config.py:74-84`
- Modify: `.env.example:46-50`
- Modify: `.gitignore:20-44`

**Interfaces:**
- Produces: `normalize_email_provider(value: str | None) -> str`
- Produces: `ClaudeEmailAccount(provider, email, password, client_id, refresh_token, source_file, source_line)`
- Produces: `ClaudeEmailAccountStore(provider=None, source_file=None, root_dir=None)`
- Produces: `ClaudeEmailAccountStore.reserve_many(limit: int | None) -> list[ClaudeEmailAccount]`
- Produces: `ClaudeEmailAccountStore.reserve_one() -> ClaudeEmailAccount | None`
- Produces: `ClaudeEmailAccountStore.mark_used(account) -> None`
- Produces: `ClaudeEmailAccountStore.mark_error(account, reason) -> None`
- Produces configuration constants: `EMAIL_PROVIDER`, `NINEMALL_EMAIL_FILE`, `NINEMALL_API_BASE`, `NINEMALL_API_PASSWORD`, `NINEMALL_HTTP_TIMEOUT`, `NINEMALL_POLL_INTERVAL`

- [ ] **Step 1: Write failing provider, parsing, reservation, immutability, and secret-safety tests**

Create `tests/test_claude_email_accounts.py` with focused cases using `tempfile.TemporaryDirectory`:

```python
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common.claude_email_accounts import (
    AccountFormatError,
    ClaudeEmailAccountStore,
    normalize_email_provider,
)


NINEMALL_ROW = "person@example.com----MailboxPass1!----client-guid----refresh-secret"
OUTLOOK_ROW = "legacy@example.com----MailboxPass2!----refresh-old----client-old"


class ClaudeEmailAccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_provider_defaults_and_validation(self):
        self.assertEqual(normalize_email_provider(None), "NINEMALL")
        self.assertEqual(normalize_email_provider(""), "NINEMALL")
        self.assertEqual(normalize_email_provider("outlook"), "OUTLOOK")
        with self.assertRaisesRegex(ValueError, "unsupported email provider"):
            normalize_email_provider("unknown")

    def test_ninemail_column_order(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = store.reserve_one()
        self.assertEqual(account.email, "person@example.com")
        self.assertEqual(account.client_id, "client-guid")
        self.assertEqual(account.refresh_token, "refresh-secret")

    def test_outlook_column_order(self):
        source = self.write("emails.txt", OUTLOOK_ROW + "\n")
        store = ClaudeEmailAccountStore("OUTLOOK", source, self.root)
        account = store.reserve_one()
        self.assertEqual(account.client_id, "client-old")
        self.assertEqual(account.refresh_token, "refresh-old")

    def test_ninemail_requires_exactly_four_nonempty_fields(self):
        with self.assertRaises(AccountFormatError) as caught:
            ClaudeEmailAccountStore.parse_line(
                "person@example.com----password----client-only", "NINEMALL", 7
            )
        self.assertIn("line 7", str(caught.exception))
        self.assertNotIn("password", str(caught.exception))
        self.assertNotIn("client-only", str(caught.exception))

    def test_reservation_never_writes_secrets_or_source(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        before = source.read_bytes()
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = store.reserve_one()
        self.assertEqual(source.read_bytes(), before)
        state = (self.root / "mail_used_claude.txt").read_text(encoding="utf-8")
        self.assertIn("person@example.com", state)
        self.assertNotIn(account.password, state)
        self.assertNotIn(account.client_id, state)
        self.assertNotIn(account.refresh_token, state)

    def test_concurrent_reservations_are_distinct(self):
        rows = "\n".join(
            f"user{i}@example.com----pass{i}----client{i}----refresh{i}"
            for i in range(8)
        )
        source = self.write("mail.txt", rows + "\n")
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        with ThreadPoolExecutor(max_workers=8) as pool:
            accounts = list(pool.map(lambda _i: store.reserve_one(), range(8)))
        self.assertEqual(len({account.email for account in accounts}), 8)

    def test_mark_error_sanitizes_reason(self):
        source = self.write("mail.txt", NINEMALL_ROW + "\n")
        store = ClaudeEmailAccountStore("NINEMALL", source, self.root)
        account = store.reserve_one()
        store.mark_error(account, "HTTP 401 refresh-secret")
        state = (self.root / "mail_error_claude.txt").read_text(encoding="utf-8")
        self.assertIn("http_401", state)
        self.assertNotIn("refresh-secret", state)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```powershell
python -m unittest tests.test_claude_email_accounts -v
```

Expected: import failure for `common.claude_email_accounts`.

- [ ] **Step 3: Add configuration constants and safe ignore rules**

Add this block to `config.py` after the existing domain-mail settings:

```python
# ---------------------------------------------------------------- Claude 邮箱渠道
EMAIL_PROVIDER = (_env("EMAIL_PROVIDER", "NINEMALL").strip().upper() or "NINEMALL")
NINEMALL_EMAIL_FILE = _env("NINEMALL_EMAIL_FILE", "mail.txt").strip() or "mail.txt"
NINEMALL_API_BASE = _env("NINEMALL_API_BASE", "https://www.appleemail.top").strip()
NINEMALL_API_PASSWORD = _env("NINEMALL_API_PASSWORD", "")
NINEMALL_HTTP_TIMEOUT = int(_env("NINEMALL_HTTP_TIMEOUT", "30") or "30")
NINEMALL_POLL_INTERVAL = int(_env("NINEMALL_POLL_INTERVAL", "5") or "5")
```

Add the matching documented values to `.env.example` and add these exact ignore entries to `.gitignore`:

```gitignore
mail.txt
mail_used_claude.txt
mail_error_claude.txt
```

Do not stage either existing `mail.txt` file.

- [ ] **Step 4: Implement the typed account store**

Create `common/claude_email_accounts.py`. Use a module-level `threading.Lock`, a frozen dataclass, repository-root relative path resolution, and channel-specific sidecar files. The implementation must follow these rules exactly:

```python
from dataclasses import dataclass
from pathlib import Path
import re
import threading

import config


_POOL_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_ROOT = Path(__file__).resolve().parent.parent
_SAFE_REASONS = {
    "http_400",
    "http_401",
    "invalid_json",
    "invalid_response",
    "magic_link_timeout",
    "network_error",
    "no_session_key",
    "onboarding_stuck",
    "phone_verify_failed",
    "registration_error",
    "timeout",
    "transient_http",
    "unexpected_http",
}


class AccountFormatError(ValueError):
    pass


def normalize_email_provider(value):
    provider = str(value or "NINEMALL").strip().upper() or "NINEMALL"
    if provider not in {"NINEMALL", "OUTLOOK"}:
        raise ValueError(f"unsupported email provider: {provider}")
    return provider


@dataclass(frozen=True)
class ClaudeEmailAccount:
    provider: str
    email: str
    password: str
    client_id: str
    refresh_token: str
    source_file: str = ""
    source_line: int = 0


class ClaudeEmailAccountStore:
    def __init__(self, provider=None, source_file=None, root_dir=None):
        self.provider = normalize_email_provider(provider or config.EMAIL_PROVIDER)
        self.root_dir = Path(root_dir or _ROOT).resolve()
        default_name = config.NINEMALL_EMAIL_FILE if self.provider == "NINEMALL" else "emails.txt"
        raw_source = Path(source_file or default_name)
        self.source_file = raw_source if raw_source.is_absolute() else self.root_dir / raw_source
        if self.provider == "NINEMALL":
            self.used_file = self.root_dir / "mail_used_claude.txt"
            self.error_file = self.root_dir / "mail_error_claude.txt"
        else:
            self.used_file = self.root_dir / "emails_used.txt"
            self.error_file = self.root_dir / "emails_error.txt"

    @staticmethod
    def parse_line(line, provider, line_number=0, source_file=""):
        provider = normalize_email_provider(provider)
        parts = [part.strip() for part in line.strip().split("----")]
        if provider == "NINEMALL":
            valid = len(parts) == 4 and all(parts)
            if not valid:
                raise AccountFormatError(f"invalid NINEMALL account at line {line_number}")
            email, password, client_id, refresh_token = parts
        else:
            if len(parts) < 2 or not parts[0] or not parts[1]:
                raise AccountFormatError(f"invalid OUTLOOK account at line {line_number}")
            email, password = parts[:2]
            refresh_token = parts[2] if len(parts) >= 3 else ""
            client_id = parts[3] if len(parts) >= 4 else ""
        if not _EMAIL_RE.match(email):
            raise AccountFormatError(f"invalid email address at line {line_number}")
        return ClaudeEmailAccount(
            provider, email, password, client_id, refresh_token,
            str(source_file), line_number,
        )

    def _blocked(self):
        blocked = set()
        for path in (self.used_file, self.error_file):
            if not path.exists():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                value = raw.strip()
                if value and not value.startswith("#"):
                    blocked.add(value.split("----", 1)[0].strip().lower())
        return blocked

    def _append_state(self, path, account, status):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            if self.provider == "OUTLOOK":
                handle.write(f"{account.email}----{account.password}----{status}\n")
            else:
                handle.write(f"{account.email}----{status}\n")

    def reserve_many(self, limit=None):
        with _POOL_LOCK:
            if not self.source_file.exists():
                return []
            blocked = self._blocked()
            selected = []
            for line_number, raw in enumerate(
                self.source_file.read_text(encoding="utf-8").splitlines(), 1
            ):
                value = raw.strip()
                if not value or value.startswith("#"):
                    continue
                try:
                    account = self.parse_line(
                        value, self.provider, line_number, self.source_file
                    )
                except AccountFormatError as exc:
                    print(f"  [email-file] {exc}")
                    continue
                if account.email.lower() in blocked:
                    continue
                self._append_state(self.used_file, account, "reserved")
                blocked.add(account.email.lower())
                selected.append(account)
                if limit is not None and len(selected) >= limit:
                    break
            return selected

    def reserve_one(self):
        selected = self.reserve_many(limit=1)
        return selected[0] if selected else None

    def mark_used(self, account):
        with _POOL_LOCK:
            self._append_state(self.used_file, account, "ok")

    def mark_error(self, account, reason):
        raw = str(reason or "unknown").lower()
        if "401" in raw:
            safe_reason = "http_401"
        elif "400" in raw:
            safe_reason = "http_400"
        elif raw in _SAFE_REASONS:
            safe_reason = raw
        else:
            safe_reason = "registration_error"
        with _POOL_LOCK:
            self._append_state(self.error_file, account, safe_reason)
```

- [ ] **Step 5: Run the account-store tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_claude_email_accounts -v
```

Expected: all account-store tests pass and the test output contains no fixture secrets.

- [ ] **Step 6: Commit the configuration and account-store slice**

```powershell
git add .gitignore .env.example config.py common/claude_email_accounts.py tests/test_claude_email_accounts.py
git commit -m "feat: add Claude mailbox account channels"
```

---

### Task 2: NINEMALL AppleEmail API Client And Magic-Link Extraction

**Files:**
- Create: `common/ninemail_mailbox.py`
- Create: `tests/test_ninemail_mailbox.py`

**Interfaces:**
- Consumes: `ClaudeEmailAccount`
- Produces: `NineMallMessage(sender, subject, received, body)`
- Produces: `NineMallMailboxError(code: str, retryable: bool)`
- Produces: `extract_claude_magic_link(messages, received_after=None) -> str | None`
- Produces: `NineMallMailboxClient.fetch_folder(account, folder) -> list[NineMallMessage]`
- Produces: `NineMallMailboxClient.poll_magic_link(account, max_wait, received_after=None) -> str | None`

- [ ] **Step 1: Write failing API contract and extraction tests**

Create `tests/test_ninemail_mailbox.py` with the following fake response/session; do not use `requests_mock` because it is not installed:

```python
import unittest

from common.claude_email_accounts import ClaudeEmailAccount
from common.ninemail_mailbox import (
    NineMallMailboxClient,
    NineMallMailboxError,
    NineMallMessage,
    extract_claude_magic_link,
)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        return self.responses.pop(0)


class FakeClock:
    def __init__(self, value=2_000_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def account():
    return ClaudeEmailAccount(
        provider="NINEMALL",
        email="person@example.com",
        password="mail-pass",
        client_id="client-guid",
        refresh_token="refresh-secret",
    )


def message(body, date="2033-05-18T03:33:25Z", sender="no-reply@claude.ai"):
    return {
        "send": sender,
        "subject": "Your Claude sign-in link",
        "date": date,
        "html": body,
        "text": "",
    }


class NineMallMailboxTests(unittest.TestCase):
    def client(self, responses):
        self.clock = FakeClock()
        self.session = FakeSession(responses)
        return NineMallMailboxClient(
            base_url="https://www.appleemail.top",
            api_password="service-pass",
            http_timeout=17,
            poll_interval=5,
            session=self.session,
            sleep=self.clock.sleep,
            clock=self.clock,
        )

    def test_post_contract_keeps_credentials_out_of_url(self):
        client = self.client([FakeResponse(200, {"data": []})])
        client.fetch_folder(account(), "INBOX")
        url, payload, timeout = self.session.calls[0]
        self.assertEqual(url, "https://www.appleemail.top/api/mail-all")
        self.assertEqual(timeout, 17)
        self.assertEqual(payload, {
            "refresh_token": "refresh-secret",
            "client_id": "client-guid",
            "email": "person@example.com",
            "mailbox": "INBOX",
            "response_type": "json",
            "password": "service-pass",
        })
        self.assertNotIn("refresh-secret", url)
        self.assertNotIn("client-guid", url)

    def test_inbox_then_junk_finds_direct_magic_link(self):
        client = self.client([
            FakeResponse(200, {"data": []}),
            FakeResponse(200, {"data": [message(
                '<a href="https://claude.ai/magic-link#direct-token">Sign in</a>'
            )]}),
        ])
        result = client.poll_magic_link(account(), max_wait=20)
        self.assertEqual(result, "https://claude.ai/magic-link#direct-token")
        self.assertEqual(
            [call[1]["mailbox"] for call in self.session.calls],
            ["INBOX", "Junk"],
        )

    def test_safelinks_target_is_decoded_and_validated(self):
        good = (
            "https://nam01.safelinks.protection.outlook.com/"
            "?url=https%3A%2F%2Fclaude.ai%2Fmagic-link%23safe-token"
        )
        bad = (
            "https://nam01.safelinks.protection.outlook.com/"
            "?url=https%3A%2F%2Fexample.invalid%2Fmagic-link%23bad-token"
        )
        messages = [
            NineMallMessage("no-reply@claude.ai", "Claude login", "2033-05-18T03:33:25Z", bad),
            NineMallMessage("no-reply@claude.ai", "Claude login", "2033-05-18T03:33:26Z", good),
        ]
        self.assertEqual(
            extract_claude_magic_link(messages),
            "https://claude.ai/magic-link#safe-token",
        )

    def test_stale_message_is_rejected_after_resend(self):
        messages = [NineMallMessage(
            "no-reply@claude.ai",
            "Claude login",
            "2020-01-01T00:00:00Z",
            "https://claude.ai/magic-link#stale-token",
        )]
        self.assertIsNone(extract_claude_magic_link(messages, received_after=2_000_000_000))

    def test_nothing_to_fetch_is_empty_result(self):
        client = self.client([FakeResponse(500, {"data": {"error": "Nothing to fetch"}})])
        self.assertEqual(client.fetch_folder(account(), "INBOX"), [])

    def test_429_and_5xx_retry_three_times(self):
        client = self.client([
            FakeResponse(429, {}),
            FakeResponse(503, {}),
            FakeResponse(200, {"data": []}),
        ])
        self.assertEqual(client.fetch_folder(account(), "INBOX"), [])
        self.assertEqual(len(self.session.calls), 3)

    def test_401_is_non_retryable_and_secret_safe(self):
        client = self.client([FakeResponse(401, {"error": "refresh-secret rejected"})])
        with self.assertRaises(NineMallMailboxError) as caught:
            client.fetch_folder(account(), "INBOX")
        self.assertEqual(caught.exception.code, "http_401")
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("refresh-secret", str(caught.exception))
        self.assertEqual(len(self.session.calls), 1)

    def test_new_refresh_token_is_ignored(self):
        client = self.client([
            FakeResponse(200, {"data": [], "new_refresh_token": "replacement-secret"}),
            FakeResponse(200, {"data": [message(
                "https://claude.ai/magic-link#original-token"
            )]}),
        ])
        self.assertEqual(
            client.poll_magic_link(account(), max_wait=20),
            "https://claude.ai/magic-link#original-token",
        )
        self.assertEqual(
            {call[1]["refresh_token"] for call in self.session.calls},
            {"refresh-secret"},
        )

    def test_non_https_base_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            NineMallMailboxClient(base_url="http://www.appleemail.top")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the API-client tests and verify RED**

Run:

```powershell
python -m unittest tests.test_ninemail_mailbox -v
```

Expected: import failure for `common.ninemail_mailbox`.

- [ ] **Step 3: Implement normalized messages and safe URL extraction**

Create `common/ninemail_mailbox.py` with these concrete helpers:

```python
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
```

- [ ] **Step 4: Implement POST requests, response normalization, retry, and polling**

Implement `NineMallMailboxClient` with constructor injection for `session`, `sleep`, and `clock` so tests remain instant. Add this complete class below the extraction helpers:

```python
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
```

Do not add code that reads or applies `new_refresh_token`; the class always rebuilds each request body from the immutable account object.

- [ ] **Step 5: Run the API tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_ninemail_mailbox -v
```

Expected: all NINEMALL client and extraction tests pass.

- [ ] **Step 6: Commit the API-client slice**

```powershell
git add common/ninemail_mailbox.py tests/test_ninemail_mailbox.py
git commit -m "feat: fetch Claude mail through NINEMALL"
```

---

### Task 3: Central Claude Magic-Link Routing

**Files:**
- Create: `tests/test_claude_mailbox_routing.py`
- Modify: `register.py:1627-1804`
- Modify: `register.py:3423-3474`
- Modify: `register.py:3788-3815`

**Interfaces:**
- Consumes: `ClaudeEmailAccount`, `NineMallMailboxClient`
- Produces: `build_ninemail_client() -> NineMallMailboxClient`
- Produces: `fetch_claude_magic_link(context, account, max_wait, received_after=None, account_lease=None, ninemail_client=None) -> Awaitable[str | None]`

- [ ] **Step 1: Write failing routing tests**

Create `tests/test_claude_mailbox_routing.py` with complete branch coverage for the routing helper:

```python
import unittest
from unittest.mock import AsyncMock, Mock, patch

import register
from common.claude_email_accounts import ClaudeEmailAccount


def mailbox_account(provider):
    return ClaudeEmailAccount(
        provider=provider,
        email="person@example.com",
        password="mail-pass",
        client_id="client-guid",
        refresh_token="refresh-secret",
    )


class ClaudeMailboxRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ninemail_uses_only_hosted_client(self):
        context = Mock()
        context.new_page = AsyncMock(side_effect=AssertionError("Outlook page opened"))
        client = Mock()
        client.poll_magic_link.return_value = "https://claude.ai/magic-link#hosted-token"
        with patch.object(register, "get_magic_link_by_token") as graph, patch.object(
            register, "get_magic_link_outlook_pw", new=AsyncMock()
        ) as browser:
            result = await register.fetch_claude_magic_link(
                context, mailbox_account("NINEMALL"), 60, ninemail_client=client
            )
        self.assertEqual(result, "https://claude.ai/magic-link#hosted-token")
        graph.assert_not_called()
        browser.assert_not_awaited()
        context.new_page.assert_not_awaited()

    async def test_ninemail_failure_never_opens_outlook(self):
        context = Mock()
        context.new_page = AsyncMock(side_effect=AssertionError("Outlook page opened"))
        client = Mock()
        client.poll_magic_link.return_value = None
        result = await register.fetch_claude_magic_link(
            context, mailbox_account("NINEMALL"), 60, ninemail_client=client
        )
        self.assertIsNone(result)
        context.new_page.assert_not_awaited()

    async def test_outlook_token_path_receives_account_client_id(self):
        context = Mock()
        context.new_page = AsyncMock()
        with patch.object(
            register,
            "get_magic_link_by_token",
            return_value="https://claude.ai/magic-link#graph-token",
        ) as graph:
            result = await register.fetch_claude_magic_link(
                context,
                mailbox_account("OUTLOOK"),
                45,
                account_lease="lease-object",
            )
        self.assertEqual(result, "https://claude.ai/magic-link#graph-token")
        graph.assert_called_once_with(
            "person@example.com",
            "refresh-secret",
            client_id="client-guid",
            max_wait=45,
            account_lease="lease-object",
        )
        context.new_page.assert_not_awaited()

    async def test_outlook_browser_fallback_closes_page(self):
        page = Mock()
        page.close = AsyncMock()
        context = Mock()
        context.new_page = AsyncMock(return_value=page)
        with patch.object(
            register, "get_magic_link_by_token", return_value=None
        ), patch.object(
            register,
            "get_magic_link_outlook_pw",
            new=AsyncMock(return_value="https://claude.ai/magic-link#browser-token"),
        ) as browser:
            result = await register.fetch_claude_magic_link(
                context, mailbox_account("OUTLOOK"), 30
            )
        self.assertEqual(result, "https://claude.ai/magic-link#browser-token")
        browser.assert_awaited_once_with(
            page, "person@example.com", "mail-pass", max_wait=30
        )
        page.close.assert_awaited_once_with()

    async def test_ninemail_received_after_is_forwarded(self):
        client = Mock()
        client.poll_magic_link.return_value = None
        await register.fetch_claude_magic_link(
            Mock(),
            mailbox_account("NINEMALL"),
            25,
            received_after=1_234.5,
            ninemail_client=client,
        )
        client.poll_magic_link.assert_called_once_with(
            mailbox_account("NINEMALL"), 25, 1_234.5
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the routing tests and verify RED**

Run:

```powershell
python -m unittest tests.test_claude_mailbox_routing -v
```

Expected: `register.fetch_claude_magic_link` is missing.

- [ ] **Step 3: Add one routing helper and safe client construction**

Import the new configuration, account, and client types in `register.py`. Implement:

```python
def build_ninemail_client():
    return NineMallMailboxClient(
        base_url=NINEMALL_API_BASE,
        api_password=NINEMALL_API_PASSWORD,
        http_timeout=NINEMALL_HTTP_TIMEOUT,
        poll_interval=NINEMALL_POLL_INTERVAL,
    )


async def fetch_claude_magic_link(
    context,
    account,
    max_wait,
    received_after=None,
    account_lease=None,
    ninemail_client=None,
):
    if account.provider == "NINEMALL":
        client = ninemail_client or build_ninemail_client()
        return await asyncio.to_thread(
            client.poll_magic_link,
            account,
            max_wait,
            received_after,
        )
    link = None
    if account.refresh_token:
        fetch_token_link = functools.partial(
            get_magic_link_by_token,
            account.email,
            account.refresh_token,
            client_id=account.client_id or "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
            max_wait=max_wait,
            account_lease=account_lease,
        )
        link = await asyncio.to_thread(fetch_token_link)
    if link:
        return link
    outlook_page = await context.new_page()
    try:
        return await get_magic_link_outlook_pw(
            outlook_page, account.email, account.password, max_wait=max_wait
        )
    finally:
        await outlook_page.close()
```

Import `functools` and use the `functools.partial` form above so the test can assert the exact `client_id`, `max_wait`, and `account_lease` values.

- [ ] **Step 4: Replace all three Claude mailbox call sites**

At the initial request, record `magic_requested_at = time.time()` immediately before submitting the email. Replace the token/browser branch with:

```python
magic_link = await fetch_claude_magic_link(
    context,
    account,
    max_wait=60,
    received_after=magic_requested_at,
    account_lease=account_lease,
)
```

For the existing resend, set `resend_requested_at = time.time()` before clicking Continue and call the same helper with that timestamp. For onboarding session recovery, set a new request timestamp and call the same helper instead of creating `re_outlook`.

Delete conditional `outlook_page.close()` calls from these call sites; page ownership now belongs entirely to `fetch_claude_magic_link`. This also removes the current unbound-`outlook_page` risk when a token path succeeds.

- [ ] **Step 5: Run routing and existing mailbox proxy tests**

Run:

```powershell
python -m unittest tests.test_claude_mailbox_routing tests.test_mailbox_account_proxy -v
```

Expected: all tests pass; NINEMALL tests observe no browser or Graph calls.

- [ ] **Step 6: Commit the routing slice**

```powershell
git add register.py tests/test_claude_mailbox_routing.py
git commit -m "refactor: centralize Claude mailbox routing"
```

---

### Task 4: Direct Claude CLI And Batch Account Lifecycle

**Files:**
- Create: `tests/test_claude_ninemail_cli.py`
- Modify: `register.py:188-397`
- Modify: `register.py:3215-3222`
- Modify: `register.py:3365-3378`
- Modify: `register.py:3763-3876`
- Modify: `register.py:3892-4060`

**Interfaces:**
- Produces: `prepare_email_accounts(args, provider=None, store_factory=ClaudeEmailAccountStore) -> tuple[list[ClaudeEmailAccount | None], ClaudeEmailAccountStore]`
- Produces: `mark_claude_account_used(account, account_store) -> None`
- Produces: `mark_claude_account_error(account, account_store, reason) -> None`
- Changes: `register(profile_id, account=None, account_store=None, account_lease=None)`
- Adds CLI option: `--client-id`

- [ ] **Step 1: Write failing CLI selection and lifecycle tests**

Create `tests/test_claude_ninemail_cli.py` with complete account-selection and state-routing tests:

```python
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import register
from common.claude_email_accounts import (
    ClaudeEmailAccount,
    ClaudeEmailAccountStore,
)


def args(**overrides):
    values = {
        "count": 1,
        "emails": None,
        "email": None,
        "password": "",
        "token": "",
        "client_id": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ClaudeNineMallCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def factory(self, **kwargs):
        return ClaudeEmailAccountStore(root_dir=self.root, **kwargs)

    def test_default_ninemail_reserves_count_from_mail_file(self):
        (self.root / "mail.txt").write_text(
            "a@example.com----pa----client-a----refresh-a\n"
            "b@example.com----pb----client-b----refresh-b\n",
            encoding="utf-8",
        )
        accounts, _store = register.prepare_email_accounts(
            args(count=2), provider="NINEMALL", store_factory=self.factory
        )
        self.assertEqual([item.email for item in accounts], ["a@example.com", "b@example.com"])
        self.assertEqual([item.client_id for item in accounts], ["client-a", "client-b"])

    def test_ninemail_explicit_account_requires_token_and_client_id(self):
        with self.assertRaisesRegex(SystemExit, "requires --token and --client-id"):
            register.prepare_email_accounts(
                args(email="a@example.com", token="refresh-a"),
                provider="NINEMALL",
                store_factory=self.factory,
            )

    def test_ninemail_emails_override_uses_new_column_order(self):
        source = self.root / "custom.txt"
        source.write_text(
            "a@example.com----pa----client-a----refresh-a\n",
            encoding="utf-8",
        )
        accounts, _store = register.prepare_email_accounts(
            args(emails=str(source)),
            provider="NINEMALL",
            store_factory=self.factory,
        )
        self.assertEqual(accounts[0].client_id, "client-a")
        self.assertEqual(accounts[0].refresh_token, "refresh-a")

    def test_outlook_without_explicit_accounts_keeps_self_registration_slots(self):
        accounts, _store = register.prepare_email_accounts(
            args(count=2), provider="OUTLOOK", store_factory=self.factory
        )
        self.assertEqual(accounts, [None, None])

    def test_explicit_ninemail_account_keeps_all_four_fields(self):
        accounts, _store = register.prepare_email_accounts(
            args(
                email="a@example.com",
                password="pa",
                token="refresh-a",
                client_id="client-a",
            ),
            provider="NINEMALL",
            store_factory=self.factory,
        )
        self.assertEqual(accounts[0], ClaudeEmailAccount(
            provider="NINEMALL",
            email="a@example.com",
            password="pa",
            client_id="client-a",
            refresh_token="refresh-a",
        ))

    def test_state_helpers_delegate_without_legacy_password_writes(self):
        account = ClaudeEmailAccount(
            "NINEMALL", "a@example.com", "pa", "client-a", "refresh-a"
        )
        store = Mock()
        with patch.object(register, "mark_email_used") as legacy_used, patch.object(
            register, "mark_email_error"
        ) as legacy_error:
            register.mark_claude_account_used(account, store)
            register.mark_claude_account_error(account, store, "http_401")
        store.mark_used.assert_called_once_with(account)
        store.mark_error.assert_called_once_with(account, "http_401")
        legacy_used.assert_not_called()
        legacy_error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

Update `tests/test_claude_ipmart_proxy.py` fixtures to pass `--client-id client-a` for explicit NINEMALL accounts or explicitly patch `EMAIL_PROVIDER=OUTLOOK` when the test is validating legacy tuple behavior. Update assertions to inspect `ClaudeEmailAccount.email` instead of assuming the email is positional argument 1 after the `register` signature changes.

- [ ] **Step 2: Run CLI and IPMart tests and verify RED**

Run:

```powershell
python -m unittest tests.test_claude_ninemail_cli tests.test_claude_ipmart_proxy -v
```

Expected: failures for missing `prepare_email_accounts`, `--client-id`, and the new typed `register` interface.

- [ ] **Step 3: Implement deterministic account preparation**

Add `--client-id` to the CLI. Implement `prepare_email_accounts` with these branches:

```python
def prepare_email_accounts(args, provider=None, store_factory=ClaudeEmailAccountStore):
    provider = normalize_email_provider(provider or EMAIL_PROVIDER)
    source = args.emails or (NINEMALL_EMAIL_FILE if provider == "NINEMALL" else "emails.txt")
    store = store_factory(provider=provider, source_file=source)
    if args.email:
        if provider == "NINEMALL" and (not args.token or not args.client_id):
            raise SystemExit("NINEMALL --email requires --token and --client-id")
        account = ClaudeEmailAccount(
            provider=provider,
            email=args.email.strip(),
            password=(args.password or "").strip(),
            client_id=(args.client_id or "").strip(),
            refresh_token=(args.token or "").strip(),
        )
        return [account], store
    if args.emails:
        accounts = store.reserve_many(limit=None)
        return accounts, store
    if provider == "NINEMALL":
        return store.reserve_many(limit=args.count), store
    return [None] * args.count, store
```

If NINEMALL returns fewer accounts than `--count`, print the available count and run only those accounts; if it returns zero, exit before creating BitBrowser profiles.

- [ ] **Step 4: Pass typed accounts through the registration function**

Change `register` to accept `account` and `account_store`. Derive local display variables only after an account exists:

```python
email = account.email if account else ""
email_password = account.password if account else ""
```

For an OUTLOOK `None` slot, preserve current self-registration and legacy pool fallback with this exact shape:

```python
if account is None:
    registered_email, registered_password = await register_outlook(page)
    if registered_email:
        account = ClaudeEmailAccount(
            provider="OUTLOOK",
            email=registered_email,
            password=registered_password,
            client_id="9e5f94bc-e8a4-4e73-b8be-63364c29d753",
            refresh_token="",
        )
    else:
        account = account_store.reserve_one()
        if account is None:
            raise RuntimeError("no email available")
```

For a NINEMALL account, skip this entire Outlook self-registration/fallback block.

Replace every direct `mark_email_used`/`mark_email_error` inside `register` with local helpers that delegate to `account_store` when a typed account came from the store. Keep the legacy functions for OUTLOOK compatibility. NINEMALL error reasons must be fixed sanitized codes such as `phone_verify_failed`, `onboarding_stuck`, `no_session_key`, `timeout`, or the `NineMallMailboxError.code`; never pass `str(exception)` into a NINEMALL state file.

Implement the helpers as:

```python
def mark_claude_account_used(account, account_store):
    if account is not None and account.provider == "NINEMALL" and account_store is not None:
        account_store.mark_used(account)
        return
    if account is not None:
        mark_email_used(account.email, account.password)


def mark_claude_account_error(account, account_store, reason):
    if account is not None and account.provider == "NINEMALL" and account_store is not None:
        account_store.mark_error(account, reason)
        return
    if account is not None:
        mark_email_error(account.email, account.password, reason)
```

- [ ] **Step 5: Update main-loop account/profile pairing**

Call `prepare_email_accounts` before creating profiles. Each `run_one(i)` receives one typed account or `None`, passes it to `register`, and keeps the existing per-account proxy lease logic. Do not reserve all rows when only `--count N` were requested.

Preserve the existing OUTLOOK behavior and update the IPMart tests to prove each typed account still receives its corresponding lease.

- [ ] **Step 6: Run direct CLI, routing, and IPMart tests**

Run:

```powershell
python -m unittest tests.test_claude_ninemail_cli tests.test_claude_mailbox_routing tests.test_claude_ipmart_proxy -v
```

Expected: all tests pass and no fixture credential appears in output.

- [ ] **Step 7: Commit the direct CLI slice**

```powershell
git add register.py tests/test_claude_ninemail_cli.py tests/test_claude_ipmart_proxy.py
git commit -m "feat: consume NINEMALL accounts in Claude registration"
```

---

### Task 5: Claude-Only Orchestrators And WebUI Propagation

**Files:**
- Create: `tests/test_claude_ninemail_entrypoints.py`
- Modify: `register_three_platforms.py:35-66`
- Modify: `register_three_platforms.py:132-147`
- Modify: `register_three_platforms.py:223-280`
- Modify: `run_full_flow.py:210-271`
- Modify: `run_full_flow.py:280-327`
- Modify: `webui/scripts.py:18-67`
- Modify: `webui/scripts.py:130-145`
- Modify: `webui/scripts.py:276-348`

**Interfaces:**
- Produces: `register_three_platforms.next_pool_account(args) -> tuple | None`
- Produces: `run_full_flow.is_ninemail_claude_only(args, env=None) -> bool`
- Produces: `run_full_flow.acquire_stage_account(args, env, stage_email_fn=stage_email, store_factory=ClaudeEmailAccountStore) -> tuple | None`
- Adds `--client-id` propagation to Claude subprocess commands.
- Adds NINEMALL configuration controls to `ENV_SCHEMA`.

- [ ] **Step 1: Write failing entry-point tests**

Create `tests/test_claude_ninemail_entrypoints.py` with complete pure-Claude and mixed-platform assertions:

```python
import argparse
import os
import unittest
from unittest.mock import Mock, patch

import register_three_platforms
import run_full_flow
from common.claude_email_accounts import ClaudeEmailAccount
from webui import scripts


def platform_args(platforms):
    return argparse.Namespace(
        platforms=platforms,
        timeout=600,
        node="auto",
        keep_on_fail=False,
        import_c2a=False,
        codex=False,
        codex_group=None,
        codex_manual_phone=False,
        grok_sub2api=False,
        grok_sub2api_group=None,
    )


class FakeStore:
    def __init__(self, account):
        self.account = account

    def reserve_one(self):
        return self.account


class ClaudeNineMallEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.account = ClaudeEmailAccount(
            "NINEMALL",
            "person@example.com",
            "mail-pass",
            "client-guid",
            "refresh-secret",
        )

    def test_claude_command_passes_token_and_client_id(self):
        command = register_three_platforms.build_command(
            "claude",
            platform_args(["claude"]),
            ("person@example.com", "mail-pass", "refresh-secret", "client-guid"),
        )
        self.assertEqual(command[command.index("--token") + 1], "refresh-secret")
        self.assertEqual(command[command.index("--client-id") + 1], "client-guid")

    def test_pure_claude_from_pool_uses_ninemail_store(self):
        args = platform_args(["claude"])
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms,
            "ClaudeEmailAccountStore",
            return_value=FakeStore(self.account),
        ):
            selected = register_three_platforms.next_pool_account(args)
        self.assertEqual(
            selected,
            ("person@example.com", "mail-pass", "refresh-secret", "client-guid"),
        )

    def test_mixed_platform_from_pool_uses_legacy_email_pool(self):
        args = platform_args(["claude", "chatgpt"])
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "NINEMALL"}), patch.object(
            register_three_platforms, "ClaudeEmailAccountStore"
        ) as store, patch.object(
            register_three_platforms.email_pool,
            "next_email",
            return_value=("legacy@example.com", "pw", "rt", "cid"),
        ) as legacy:
            selected = register_three_platforms.next_pool_account(args)
        self.assertEqual(selected[0], "legacy@example.com")
        store.assert_not_called()
        legacy.assert_called_once_with("tri")

    def test_full_flow_pure_claude_bypasses_stage_email(self):
        args = argparse.Namespace(platforms=["claude"])
        stage = Mock(side_effect=AssertionError("Outlook Stage A ran"))
        selected = run_full_flow.acquire_stage_account(
            args,
            {"EMAIL_PROVIDER": "NINEMALL"},
            stage_email_fn=stage,
            store_factory=lambda **_kwargs: FakeStore(self.account),
        )
        self.assertEqual(
            selected,
            ("person@example.com", "mail-pass", "refresh-secret", "client-guid"),
        )
        stage.assert_not_called()

    def test_full_flow_mixed_platform_keeps_stage_email(self):
        args = argparse.Namespace(platforms=["claude", "chatgpt"])
        expected = ("legacy@example.com", "pw", "rt", "cid")
        stage = Mock(return_value=expected)
        selected = run_full_flow.acquire_stage_account(
            args,
            {"EMAIL_PROVIDER": "NINEMALL"},
            stage_email_fn=stage,
        )
        self.assertEqual(selected, expected)
        stage.assert_called_once_with(args, {"EMAIL_PROVIDER": "NINEMALL"})

    def test_webui_exposes_client_id_and_ninemail_env(self):
        claude_flags = {item["flag"] for item in scripts.script_by_id("register_claude")["args"]}
        self.assertIn("--client-id", claude_flags)
        env_keys = set(scripts.env_keys())
        self.assertTrue({
            "EMAIL_PROVIDER",
            "NINEMALL_EMAIL_FILE",
            "NINEMALL_API_BASE",
            "NINEMALL_API_PASSWORD",
            "NINEMALL_HTTP_TIMEOUT",
            "NINEMALL_POLL_INTERVAL",
        }.issubset(env_keys))


if __name__ == "__main__":
    unittest.main()
```

Retain and run the existing `tests/test_platform_proxy_env.py` and `tests/test_full_flow_ipmart_proxy.py` because environment and lease routing must not regress.

- [ ] **Step 2: Run entry-point tests and verify RED**

Run:

```powershell
python -m unittest tests.test_claude_ninemail_entrypoints tests.test_platform_proxy_env tests.test_full_flow_ipmart_proxy -v
```

Expected: new entry-point assertions fail while existing proxy tests retain their baseline result.

- [ ] **Step 3: Add provider-aware pool selection to `register_three_platforms.py`**

Add one helper used by both `parse_account` and the loop:

```python
def next_pool_account(args):
    provider = normalize_email_provider(os.environ.get("EMAIL_PROVIDER"))
    if provider == "NINEMALL" and set(args.platforms) == {"claude"}:
        account = ClaudeEmailAccountStore(provider="NINEMALL").reserve_one()
        if account is None:
            return None
        return (
            account.email,
            account.password,
            account.refresh_token,
            account.client_id,
        )
    return email_pool.next_email("tri")
```

Use it everywhere the file currently calls `email_pool.next_email("tri")`. In `build_command`, add `--client-id` for Claude when non-empty. Do not change ChatGPT or Grok command construction.

- [ ] **Step 4: Bypass Outlook Stage A only for pure Claude NINEMALL runs**

Implement:

```python
def is_ninemail_claude_only(args, env=None):
    env = os.environ if env is None else env
    return (
        normalize_email_provider(env.get("EMAIL_PROVIDER")) == "NINEMALL"
        and set(args.platforms) == {"claude"}
    )
```

Add the stage-selection helper used by `run_once`:

```python
def acquire_stage_account(
    args,
    env,
    stage_email_fn=stage_email,
    store_factory=ClaudeEmailAccountStore,
):
    if is_ninemail_claude_only(args, env):
        account = store_factory(provider="NINEMALL").reserve_one()
        if account is None:
            return None
        return (
            account.email,
            account.password,
            account.refresh_token,
            account.client_id,
        )
    return stage_email_fn(args, env)
```

In `run_once`, before `stage_email`, reserve one typed NINEMALL account when this helper is true and `--skip-email` is false. Pass its email, password, refresh token, and client ID into `stage_platforms`. Mixed and non-Claude runs must execute the unchanged Stage A branch.

Add `--token` and `--client-id` parser options for explicit `--skip-email` use and include them in the WebUI `run_full_flow` schema.

- [ ] **Step 5: Update WebUI metadata**

Add `--client-id` to the standalone Claude form. Add this group to `ENV_SCHEMA`:

```python
{"group": "Claude 邮箱渠道", "items": [
    {"key": "EMAIL_PROVIDER", "type": "choice", "choices": ["NINEMALL", "OUTLOOK"],
     "default": "NINEMALL", "help": "Claude 邮箱渠道；默认 NINEMALL API 取信"},
    {"key": "NINEMALL_EMAIL_FILE", "default": "mail.txt", "help": "NINEMALL 四列账号文件"},
    {"key": "NINEMALL_API_BASE", "default": "https://www.appleemail.top", "help": "NINEMALL 取信 API 根地址"},
    {"key": "NINEMALL_API_PASSWORD", "secret": True, "help": "小苹果服务访问密码；未启用时留空"},
    {"key": "NINEMALL_HTTP_TIMEOUT", "type": "int", "default": 30, "help": "单次取信请求超时秒数"},
    {"key": "NINEMALL_POLL_INTERVAL", "type": "int", "default": 5, "help": "Claude 邮件轮询间隔秒数"},
]},
```

- [ ] **Step 6: Run entry-point and proxy regression tests**

Run:

```powershell
python -m unittest tests.test_claude_ninemail_entrypoints tests.test_platform_proxy_env tests.test_full_flow_ipmart_proxy -v
```

Expected: all tests pass; mixed-platform behavior remains on the legacy pool.

- [ ] **Step 7: Commit the entry-point slice**

```powershell
git add register_three_platforms.py run_full_flow.py webui/scripts.py tests/test_claude_ninemail_entrypoints.py
git commit -m "feat: route Claude-only entry points to NINEMALL"
```

---

### Task 6: Documentation, Static Validation, And Full Regression

**Files:**
- Modify: `README.md:228-323`
- Modify: `CHANGELOG.md:1-20`
- Test: all Python tests under `tests/`

**Interfaces:**
- Documents the exact environment variables, formats, provider scope, safe execution commands, and no-browser guarantee.
- Produces no new runtime interface.

- [ ] **Step 1: Update user documentation**

Add a Claude mailbox-channel section to `README.md` containing these safe examples:

```dotenv
EMAIL_PROVIDER=NINEMALL
NINEMALL_EMAIL_FILE=mail.txt
NINEMALL_API_BASE=https://www.appleemail.top
NINEMALL_API_PASSWORD=
NINEMALL_HTTP_TIMEOUT=30
NINEMALL_POLL_INTERVAL=5
```

Document both row formats and state explicitly:

```text
NINEMALL: email----password----client_id----refresh_token
OUTLOOK:  email----password----refresh_token----client_id
```

Document that NINEMALL is Claude-only, uses POST, scans INBOX and Junk, never updates `mail.txt`, ignores `new_refresh_token`, and never falls back to Outlook browser login. Document `EMAIL_PROVIDER=OUTLOOK` as the compatibility switch. Include commands:

```powershell
python register.py --count 1
python register_three_platforms.py --from-pool --platforms claude
python run_full_flow.py --platforms claude --rounds 1
```

Do not include a real account, token, client ID, or API password.

- [ ] **Step 2: Add a concise changelog entry**

Record the new Claude-only NINEMALL provider, the new account order, AppleEmail POST polling, strict no-browser behavior, and unchanged non-Claude behavior.

- [ ] **Step 3: Run syntax compilation**

Run:

```powershell
python -m py_compile config.py common/claude_email_accounts.py common/ninemail_mailbox.py register.py register_three_platforms.py run_full_flow.py webui/scripts.py
```

Expected: exit code 0 with no output.

- [ ] **Step 4: Run focused NINEMALL and Claude suites**

Run:

```powershell
python -m unittest tests.test_claude_email_accounts tests.test_ninemail_mailbox tests.test_claude_mailbox_routing tests.test_claude_ninemail_cli tests.test_claude_ninemail_entrypoints -v
```

Expected: all focused tests pass with zero live network calls.

- [ ] **Step 5: Run the complete Python test suite**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: all tests pass. If an existing live/integration test is intentionally environment-gated, it must report `skipped`; no test may consume a real mailbox, proxy allocation, or external account.

- [ ] **Step 6: Verify secrets and source immutability**

Run:

```powershell
git status --short
git diff --check
git diff --name-only
git check-ignore -v mail.txt
git check-ignore -v mail_used_claude.txt
git check-ignore -v mail_error_claude.txt
```

Expected:

- `mail.txt` is ignored and absent from `git diff --name-only`.
- Neither `_outlook_pool/mail.txt` nor any credential/state file is staged.
- `git diff --check` exits 0.

Also inspect changed source and tests for secret-like literals. Fixture values must remain obvious non-working strings such as `refresh-secret` and `client-guid`; no token from the conversation may appear.

- [ ] **Step 7: Commit docs after verification**

```powershell
git add README.md CHANGELOG.md
git commit -m "docs: describe NINEMALL Claude mailbox setup"
```

- [ ] **Step 8: Inspect the final branch**

Run:

```powershell
git status --short
git log --oneline --decorate -8
git diff main...HEAD --stat
```

Expected: only intentionally ignored local credential files remain outside Git; the branch contains the design and plan commits plus the six implementation commits above.
