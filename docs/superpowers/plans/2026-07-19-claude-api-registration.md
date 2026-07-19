# Claude API Personal Registration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Completed steps use checked task boxes.

**Status:** Implemented; final lifecycle, durability, routing, and redaction corrections incorporated on 2026-07-19.

**Goal:** Add an independent `claude_api` flow that authenticates at `platform.claude.com` with either the email magic link or numeric code, selects a personal account, exports the console session, and leaves recharge for a later feature.

**Architecture:** Keep Claude.ai registration in `register.py` unchanged and add a focused `register_claude_api.py`. Extend the existing Claude mailbox account store with purpose-specific ledgers, add a pure Claude Platform verification-artifact parser used by NINEMALL and OUTLOOK channels, and expose the new flow through both orchestrators and the WebUI.

**Tech Stack:** Python 3, `asyncio`, Playwright over the existing BitBrowser/AdsPower provider, `requests`, Microsoft Graph helpers, `aiohttp` mailbox broker, `unittest`, FastAPI WebUI metadata.

## Global Constraints

- `claude` and `claude_api` are independent platform choices; existing Claude.ai behavior must not change.
- NINEMALL remains the default Claude-family email provider and must never fall back to Graph, mailbox broker, Outlook browser, or IMAP.
- NINEMALL continues reading `email----password----client_id----refresh_token`; OUTLOOK continues reading `email----password----refresh_token----client_id`.
- The same mailbox may be used once for `claude` and once for `claude_api`; ledgers are independent.
- Every mailbox poll checks both Platform magic links and numeric codes; there is no global link-first rule.
- A direct magic link must be HTTPS on `platform.claude.com` with path `/magic-link`; SafeLinks targets must pass the same validation after decoding.
- A code candidate is 4-10 digits and must be adjacent to explicit login-code or verification-code wording in visible message text.
- Only an explicit personal-account option may be selected. Never submit organization or team creation.
- Success requires both an authenticated Platform URL and a stable console-only element.
- Save full Platform cookies under `cookies/claude_api/`; never persist mailbox passwords, client IDs, refresh tokens, API passwords, codes, or magic links in logs or indexes.
- Serialize every ledger mutation across processes. Shared-purpose reservation must be transactional and crash-recoverable; a partial append may not consume only one purpose.
- Start one monotonic account deadline before optional IPMart acquisition. Proxy acquisition, profile create/open, CDP connection, registration, and session export consume only its remaining budget.
- Serialize synchronous profile operations in a daemon owner. Never submit delete until close has completed successfully, and never finalize a ledger while async ownership or cleanup is unconfirmed.
- Publish session cookies before their index record, using fsynced private files, atomic replacement, and an interprocess index lock.
- Do not create API keys, add credit, bind payment, or perform recharge in this plan.
- Automated tests must mock AppleEmail, Microsoft, Anthropic, proxy, browser, and account-creation I/O.

---

## File Structure

- `common/claude_email_accounts.py`: provider parsing, purpose-specific state files, shared Claude-family reservation.
- `common/interprocess_lock.py`: cross-platform advisory lock shared by durable ledgers and session indexes.
- `common/claude_platform_mailbox.py`: pure message model, Platform magic-link/code extraction, Graph and Outlook-browser polling.
- `common/ninemail_mailbox.py`: AppleEmail transport and Platform dual-artifact polling; existing Claude.ai magic-link API remains intact.
- `common/claude_platform_session.py`: console-success predicate and secret-safe full-cookie export.
- `mailbox_broker.py`: one-pass Platform artifact extraction for a shared Outlook session.
- `register_claude_api.py`: Platform page state machine, provider dispatch, browser/proxy lifecycle, CLI, result accounting.
- `register_three_platforms.py`: `claude_api` child command and Claude-family NINEMALL routing.
- `run_full_flow.py`: end-to-end platform choice, lease rules, and Claude-family NINEMALL account acquisition.
- `webui/scripts.py`: standalone and orchestrated UI choices.
- `README.md`, `CHANGELOG.md`, `.gitignore`: user-facing behavior and generated-file exclusions.
- `tests/test_claude_email_accounts.py`: ledger namespace and shared-reservation tests.
- `tests/test_claude_platform_mailbox.py`: pure extractor, Graph, Outlook, and broker contract tests.
- `tests/test_ninemail_mailbox.py`: NINEMALL Platform polling tests and Claude.ai regressions.
- `tests/test_claude_api_registration.py`: page state machine and session export tests.
- `tests/test_claude_api_cli.py`: CLI lifecycle, cleanup, redaction, and result-marker tests.
- `tests/test_claude_api_entrypoints.py`: orchestrator and WebUI integration tests.

---

### Task 1: Purpose-Specific Claude Mailbox Ledgers

**Files:**
- Modify: `common/claude_email_accounts.py:24-240`
- Modify: `.gitignore`
- Test: `tests/test_claude_email_accounts.py`

**Interfaces:**
- Consumes: existing `ClaudeEmailAccount` and `ClaudeEmailAccountStore` parsing and locking.
- Produces: `ClaudeEmailAccountStore(provider=None, source_file=None, root_dir=None, purpose="claude")`; `reserve_shared_claude_account(provider, purposes, source_file=None, root_dir=None) -> tuple[ClaudeEmailAccount, dict[str, ClaudeEmailAccountStore]] | None`.

- [x] **Step 1: Write failing namespace and shared-reservation tests**

Append tests that prove one source row can be terminal in the Claude.ai ledger and still be reserved for Claude Platform, and that a shared reservation selects an address unblocked in every requested ledger:

```python
from common.claude_email_accounts import reserve_shared_claude_account

def test_claude_and_claude_api_ledgers_are_independent(self):
    source = self.write("mail.txt", NINEMALL_ROW + "\n")
    claude = ClaudeEmailAccountStore(
        "NINEMALL", source, self.root, purpose="claude"
    )
    account = claude.reserve_one()
    claude.mark_used(account)

    api = ClaudeEmailAccountStore(
        "NINEMALL", source, self.root, purpose="claude_api"
    )
    selected = api.reserve_one()

    self.assertEqual(selected.email, account.email)
    self.assertTrue((self.root / "mail_used_claude.txt").exists())
    self.assertTrue((self.root / "mail_used_claude_api.txt").exists())

def test_shared_reservation_skips_address_blocked_for_one_purpose(self):
    source = self.write(
        "mail.txt",
        NINEMALL_ROW + "\n"
        "second@example.com----pass----client-2----refresh-2\n",
    )
    blocked = ClaudeEmailAccountStore(
        "NINEMALL", source, self.root, purpose="claude"
    )
    first = blocked.reserve_one()
    blocked.mark_used(first)

    result = reserve_shared_claude_account(
        "NINEMALL", ("claude", "claude_api"), source, self.root
    )

    account, stores = result
    self.assertEqual(account.email, "second@example.com")
    self.assertEqual(set(stores), {"claude", "claude_api"})
```

Also assert OUTLOOK `purpose="claude_api"` writes
`emails_used_claude_api.txt` / `emails_error_claude_api.txt` and does not alter
`emails_used.txt`.

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_claude_email_accounts -v
```

Expected: FAIL because `purpose` and `reserve_shared_claude_account` do not exist.

- [x] **Step 3: Implement explicit state namespaces without breaking positional callers**

Keep the first three constructor positions unchanged and add `purpose` last:

```python
_PURPOSES = {"claude", "claude_api"}

def normalize_purpose(value):
    purpose = str(value or "claude").strip().lower() or "claude"
    if purpose not in _PURPOSES:
        raise ValueError(f"unsupported Claude email purpose: {purpose}")
    return purpose

class ClaudeEmailAccountStore:
    def __init__(
        self,
        provider=None,
        source_file=None,
        root_dir=None,
        purpose="claude",
    ):
        self.provider = normalize_email_provider(provider or config.EMAIL_PROVIDER)
        self.purpose = normalize_purpose(purpose)
        self.root_dir = Path(root_dir or _ROOT).resolve()
        default_name = (
            config.NINEMALL_EMAIL_FILE
            if self.provider == "NINEMALL"
            else "emails.txt"
        )
        raw_source = Path(source_file or default_name)
        self.source_file = (
            raw_source if raw_source.is_absolute() else self.root_dir / raw_source
        )
        if self.provider == "NINEMALL":
            suffix = "claude" if self.purpose == "claude" else "claude_api"
            self.used_file = self.root_dir / f"mail_used_{suffix}.txt"
            self.error_file = self.root_dir / f"mail_error_{suffix}.txt"
        elif self.purpose == "claude_api":
            self.used_file = self.root_dir / "emails_used_claude_api.txt"
            self.error_file = self.root_dir / "emails_error_claude_api.txt"
        else:
            self.used_file = self.root_dir / "emails_used.txt"
            self.error_file = self.root_dir / "emails_error.txt"
        self._active_reservations = set()
```

Add the new stable error codes to `_SAFE_REASONS`:

```python
_SAFE_REASONS.update({
    "mail_timeout",
    "verification_artifact_not_found",
    "magic_link_invalid",
    "verification_rejected",
    "personal_account_not_available",
    "console_not_reached",
})
```

Implement shared selection under `_locked_pool(root_dir)`, which combines the
in-process mutex with a per-root interprocess advisory lock. Before appending
to either purpose ledger, write and fsync a credential-free recovery journal
containing only each target filename, whether it existed, and its original
size. Roll back every target on any append failure; the next lock holder also
performs the same recovery after an interrupted process:

```python
def reserve_shared_claude_account(
    provider,
    purposes,
    source_file=None,
    root_dir=None,
):
    requested = tuple(dict.fromkeys(normalize_purpose(p) for p in purposes))
    if not requested:
        raise ValueError("at least one Claude email purpose is required")
    stores = {
        purpose: ClaudeEmailAccountStore(
            provider=provider,
            source_file=source_file,
            root_dir=root_dir,
            purpose=purpose,
        )
        for purpose in requested
    }
    base = stores[requested[0]]
    with _locked_pool(base.root_dir):
        blocked = {purpose: store._blocked() for purpose, store in stores.items()}
        for account in base._load_accounts():
            email = account.email.lower()
            if any(email in blocked[purpose] for purpose in requested):
                continue
            _prepare_pool_transaction(
                base.root_dir,
                [store.used_file for store in stores.values()],
            )
            try:
                for store in stores.values():
                    store._append_state(store.used_file, account, "reserved")
            except BaseException:
                _recover_pool_transaction(base.root_dir)
                raise
            _finish_pool_transaction(base.root_dir)
            for store in stores.values():
                store._active_reservations.add(email)
            return account, stores
    return None
```

Append these exact ignore entries:

```gitignore
mail_used_claude_api.txt
mail_error_claude_api.txt
emails_used_claude_api.txt
emails_error_claude_api.txt
.claude_email_pool.lock
.claude_email_pool.journal
```

- [x] **Step 4: Run focused and existing account-store tests**

Run:

```powershell
python -m unittest tests.test_claude_email_accounts -v
```

Expected: all account-store tests PASS, including unchanged default Claude.ai filenames.

- [x] **Step 5: Commit the ledger boundary**

```powershell
git add common/claude_email_accounts.py tests/test_claude_email_accounts.py .gitignore
git commit -m "feat: isolate Claude API mailbox state"
```

---

### Task 2: Claude Platform Verification Artifact and NINEMALL Polling

**Files:**
- Create: `common/claude_platform_mailbox.py`
- Modify: `common/ninemail_mailbox.py:1-307`
- Test: `tests/test_claude_platform_mailbox.py`
- Test: `tests/test_ninemail_mailbox.py`

**Interfaces:**
- Consumes: message objects with `sender`, `subject`, `received`, and `body` attributes; existing `NineMallMailboxClient.fetch_folder()`.
- Produces: `ClaudePlatformVerification(magic_link: str = "", code: str = "", received_at: float = 0.0)`; `extract_claude_platform_verification(messages, received_after=None)`; `NineMallMailboxClient.poll_claude_platform_verification(account, max_wait, received_after=None, *, cancel_event=None)`.

- [x] **Step 1: Write failing pure-extractor tests**

Create `tests/test_claude_platform_mailbox.py` with direct-link, SafeLinks,
code-only, both-artifacts, stale-message, and false-number coverage:

```python
import unittest
from common.claude_platform_mailbox import (
    ClaudePlatformMessage,
    extract_claude_platform_verification,
)

class ClaudePlatformMailboxTests(unittest.TestCase):
    def message(self, subject, body, received="2033-05-18T03:33:25Z"):
        return ClaudePlatformMessage(
            sender="no-reply@claude.com",
            subject=subject,
            received=received,
            body=body,
        )

    def test_code_only_message_returns_code_without_waiting_for_link(self):
        result = extract_claude_platform_verification([
            self.message("Your Claude verification code is 482731", "Sign in")
        ])
        self.assertEqual(result.code, "482731")
        self.assertEqual(result.magic_link, "")

    def test_magic_link_only_message_returns_validated_platform_link(self):
        result = extract_claude_platform_verification([
            self.message(
                "Sign in to Claude Platform",
                '<a href="https://platform.claude.com/magic-link?code=abc">Continue</a>',
            )
        ])
        self.assertEqual(
            result.magic_link,
            "https://platform.claude.com/magic-link?code=abc",
        )

    def test_both_artifacts_are_returned_without_global_priority(self):
        result = extract_claude_platform_verification([
            self.message(
                "Verification code: 482731",
                "https://platform.claude.com/magic-link?code=abc",
            )
        ])
        self.assertEqual(result.code, "482731")
        self.assertTrue(result.magic_link)

    def test_dates_css_and_unrelated_numbers_are_rejected(self):
        result = extract_claude_platform_verification([
            self.message(
                "Claude notice 20260719",
                '<style>.x{color:#482731}</style><p>Invoice 123456</p>',
            )
        ])
        self.assertIsNone(result)
```

Add NINEMALL tests showing INBOX/Junk polling returns code-only immediately and
does not change `poll_magic_link()` behavior.

- [x] **Step 2: Run the new tests and verify RED**

Run:

```powershell
python -m unittest tests.test_claude_platform_mailbox tests.test_ninemail_mailbox -v
```

Expected: FAIL because the Platform module and polling method do not exist.

- [x] **Step 3: Implement the pure parser**

Create `common/claude_platform_mailbox.py` with these public types and rules:

```python
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

_URL_RE = re.compile(r"https://[^\s\"'<>]+", re.IGNORECASE)
_CODE_PATTERNS = (
    re.compile(
        r"(?:verification|login|sign[ -]?in)\s+code\D{0,24}(\d{4,10})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d{4,10})\D{0,24}(?:verification|login|sign[ -]?in)\s+code",
        re.IGNORECASE,
    ),
)
```

Implement `_received_epoch`, SafeLinks decoding, and direct URL validation so
only HTTPS `platform.claude.com/magic-link` targets are returned. Filter sender
or subject by `anthropic` or `claude`, reject stale or unparseable received
times when `received_after` is supplied, sort newest first, and return one
`ClaudePlatformVerification` for the first message containing either artifact.

Use these concrete helpers in that module:

```python
def _validated_platform_link(candidate, allow_safelink=True):
    value = unescape(str(candidate or "")).rstrip(".,);]")
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.hostname == "platform.claude.com":
        if parsed.path.rstrip("/") == "/magic-link":
            return value
    if allow_safelink and (parsed.hostname or "").endswith("safelinks.protection.outlook.com"):
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
    for received, message in sorted(candidates, reverse=True, key=lambda item: item[0]):
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
```

- [x] **Step 4: Add NINEMALL Platform polling without changing Claude.ai polling**

Import the pure extractor and add this method to `NineMallMailboxClient`:

```python
def poll_claude_platform_verification(
    self,
    account,
    max_wait,
    received_after=None,
    *,
    cancel_event=None,
):
    wait_budget = float(max_wait)
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
        result = extract_claude_platform_verification(
            messages,
            received_after=received_after,
        )
        if result:
            return result
        if not self._sleep_bounded(
            self.poll_interval,
            deadline,
            cancel_event,
        ):
            break
    return None
```

Do not log either returned field. Do not edit `_validated_claude_link`,
`extract_claude_magic_link`, or `poll_magic_link` except for safe imports.

- [x] **Step 5: Run focused and Claude.ai NINEMALL regression tests**

Run:

```powershell
python -m unittest tests.test_claude_platform_mailbox tests.test_ninemail_mailbox tests.test_claude_mailbox_routing -v
```

Expected: all tests PASS; existing Claude.ai fragment-token magic-link tests remain green.

- [x] **Step 6: Commit the artifact parser and NINEMALL adapter**

```powershell
git add common/claude_platform_mailbox.py common/ninemail_mailbox.py tests/test_claude_platform_mailbox.py tests/test_ninemail_mailbox.py
git commit -m "feat: read Claude Platform verification mail"
```

---

### Task 3: OUTLOOK Graph, Broker, and Browser Artifact Channels

**Files:**
- Modify: `common/claude_platform_mailbox.py`
- Modify: `common/mailbox.py:60-590`
- Modify: `mailbox_broker.py:42-330`
- Test: `tests/test_claude_platform_mailbox.py`
- Test: `tests/test_claude_api_entrypoints.py`

**Interfaces:**
- Consumes: `ClaudePlatformVerification`, existing `_get_access_token()`, `fetch_messages()`, Outlook login/folder helpers, and broker `/fetch`.
- Produces: `get_claude_platform_verification_by_token(email, refresh_token, client_id, max_wait=120, poll=5, received_after=None, account_lease=None)`; `get_claude_platform_verification_outlook_pw(page, email, password, max_wait=120, received_after=None)`; `fetch_claude_platform_from_broker(email, password, max_wait=120, received_after=None)`; broker `kind="claude_platform"` returning `{magic_link, code, received_at}`.

- [x] **Step 1: Write failing Graph and broker tests**

Add tests that mock Graph messages and assert one polling pass can return either
artifact, forwards `received_after` and `account_lease`, and never prints the
artifact. Add a broker handler test:

```python
def test_broker_platform_kind_returns_structured_artifact(self):
    broker = mailbox_broker.Broker()
    broker.ensure_session = AsyncMock(return_value=self.session)
    broker._scan_platform_artifact = AsyncMock(return_value={
        "magic_link": "",
        "code": "482731",
        "received_at": 2000000001.0,
    })

    result = asyncio.run(broker.fetch(
        "person@example.com",
        "mail-pass",
        ("anthropic", "claude"),
        ("code", "sign in", "login"),
        "",
        "claude_platform",
        30,
    ))

    self.assertEqual(result["code"], "482731")
```

- [x] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_claude_platform_mailbox tests.test_claude_api_entrypoints -v
```

Expected: FAIL because the three OUTLOOK facade functions and broker kind do not exist.

- [x] **Step 3: Add one-pass Graph polling**

In `common/claude_platform_mailbox.py`, implement:

```python
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
        time.sleep(poll)
    return None
```

Refresh the access token once in the second half of the polling window using
the same rule as `get_code_by_token`. Log only provider/folder/wait time.

- [x] **Step 4: Add browser and broker structured scanning**

Add a browser helper that opens the newest matching Anthropic/Claude message,
returns its sender/subject/body/links as a `ClaudePlatformMessage`, then applies
the pure extractor. Reuse `_outlook_login`, `_click_folder`, INBOX/Junk names,
and the existing new-message baseline behavior.

Promote the existing local folder-name arrays in `common.mailbox` to module
constants so the new helper and existing code path share exactly these values:

```python
INBOX_NAMES = ["收件箱", "Inbox", "受信トレイ"]
JUNK_NAMES = ["垃圾邮件", "Junk Email", "Junk", "迷惑メール"]
```

Expose these exact async implementations in `common/claude_platform_mailbox.py`:

```python
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
    received_after=None,
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
        "received_after": received_after,
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
    return validate_claude_platform_verification(
        magic_link,
        code,
        value.get("received_at"),
        received_after=received_after,
    )
```

Implement `_scan_claude_platform_folder(page, received_after=None)` immediately
above these functions. It must click the newest visible list item whose text
contains `anthropic` or `claude`, wait for the reading pane, collect its visible
text plus all HTTPS hrefs in that pane, construct one `ClaudePlatformMessage`
with the current UTC time, and call
`extract_claude_platform_verification([message], received_after)`. The broker
payload uses `kind="claude_platform"`, and `fetch_from_broker()` must stop
slicing/logging structured values.

Use this implementation so the browser and broker paths share the pure parser:

```python
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
```

In `mailbox_broker.py`, branch on `kind == "claude_platform"` in both fresh
login and new-message paths, call `_scan_platform_artifact()`, store a tuple of
the artifact fields in `s.seen`, and log only that an artifact was found.

Mask mailbox addresses in broker output while touching these log paths:

```python
def _masked_email(email):
    local, separator, domain = str(email or "").partition("@")
    if not separator:
        return "***"
    return f"{local[:2]}***@{domain}"
```

Use `display_email = _masked_email(email)` in `ensure_session()`, `fetch()`,
`_close_session()`, and their error output; never interpolate the raw address,
artifact value, password, client ID, or refresh token.

- [x] **Step 5: Run mailbox and broker regressions**

Run:

```powershell
python -m unittest tests.test_claude_platform_mailbox tests.test_mailbox_account_proxy tests.test_claude_mailbox_routing tests.test_claude_api_entrypoints -v
```

Expected: all tests PASS; existing code/link broker calls still return strings.

- [x] **Step 6: Commit the OUTLOOK channels**

```powershell
git add common/claude_platform_mailbox.py common/mailbox.py mailbox_broker.py tests/test_claude_platform_mailbox.py tests/test_claude_api_entrypoints.py
git commit -m "feat: fetch Claude Platform mail from Outlook"
```

---

### Task 4: Claude Platform Page State Machine and Session Export

**Files:**
- Create: `common/claude_platform_session.py`
- Create: `register_claude_api.py`
- Create: `tests/test_claude_api_registration.py`

**Interfaces:**
- Consumes: `ClaudeEmailAccount`, `ClaudePlatformVerification`, a Playwright page/context, and an async verification fetch callback.
- Produces: `apply_verification_artifact(page, artifact)`; `select_personal_account(page)`; `is_console_ready(page)`; `save_claude_platform_session(context, email, output_dir="cookies/claude_api", *, operation_timeout=30.0)`; `run_claude_platform_flow(page, context, account, fetch_verification, max_wait, output_dir="cookies/claude_api")`.

- [x] **Step 1: Write failing state-machine tests with fake page objects**

Cover code-only, link-only, both-artifacts/current-code-screen, one resend,
personal selection, organization refusal, console success, and false success:

```python
class ClaudeApiRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_code_only_artifact_opens_code_ui_and_submits(self):
        page = FakePlatformPage(state="email_sent")
        artifact = ClaudePlatformVerification(code="482731")

        await register_claude_api.apply_verification_artifact(page, artifact)

        self.assertEqual(page.code_value, "482731")
        self.assertEqual(page.state, "authenticated")

    async def test_both_artifacts_use_code_when_code_input_is_visible(self):
        page = FakePlatformPage(state="code")
        artifact = ClaudePlatformVerification(
            magic_link="https://platform.claude.com/magic-link?code=abc",
            code="482731",
        )

        await register_claude_api.apply_verification_artifact(page, artifact)

        self.assertEqual(page.code_value, "482731")
        self.assertEqual(page.goto_calls, [])

    async def test_organization_form_is_never_submitted(self):
        page = FakePlatformPage(state="organization")
        with self.assertRaisesRegex(
            register_claude_api.ClaudeApiRegistrationError,
            "personal_account_not_available",
        ):
            await register_claude_api.select_personal_account(page)
        self.assertEqual(page.submissions, [])
```

Test session export with a temporary directory and cookies containing obvious
mailbox secrets; assert only browser cookies and masked metadata are written.

- [x] **Step 2: Run the state-machine tests and verify RED**

Run:

```powershell
python -m unittest tests.test_claude_api_registration -v
```

Expected: FAIL because `register_claude_api.py` and session helpers do not exist.

- [x] **Step 3: Implement secret-safe session export**

Create `common/claude_platform_session.py`:

```python
def _persist_claude_platform_session(platform_cookies, email, output_dir):
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    email_key = _email_key(email)
    path = target / (
        f"full_{email_key}_{datetime.now():%Y%m%d_%H%M%S_%f}_"
        f"{uuid.uuid4().hex}.json"
    )
    temporary = target / f".{path.name}.{uuid.uuid4().hex}.tmp"
    final_created = False
    try:
        _write_fsynced(
            temporary,
            json.dumps(platform_cookies, ensure_ascii=False, indent=2)
            .encode("utf-8"),
        )
        os.replace(temporary, path)
        final_created = True
        _fsync_directory(target)
        _replace_index(
            target / "accounts.jsonl",
            {"email_key": email_key, "cookie_file": path.name},
        )
    except _IndexPublicationUnconfirmed:
        # Index replacement is visible. Keep the referenced cookie so the
        # current filesystem state cannot contain a dangling index record.
        raise
    except Exception:
        temporary.unlink(missing_ok=True)
        if final_created:
            path.unlink(missing_ok=True)
            _fsync_directory(target)
        raise
    return path

async def save_claude_platform_session(
    context,
    email,
    output_dir="cookies/claude_api",
    *,
    operation_timeout=DEFAULT_SESSION_PERSIST_TIMEOUT,
):
    cookies = await context.cookies()
    platform_cookies = [
        cookie for cookie in cookies
        if _is_claude_domain(cookie.get("domain"))
    ]
    if not platform_cookies:
        raise RuntimeError("console_not_reached")
    return await run_daemon_call(
        lambda: _persist_claude_platform_session(
            platform_cookies, email, output_dir
        ),
        operation_timeout,
        name="claude-api-session-owner",
    )
```

`_write_fsynced()` creates private mode-`0600` files, handles short writes, and
fsyncs before publication. `_replace_index()` holds an interprocess advisory
lock while it copies the previous index plus one record into a private,
fsynced temporary file, atomically replaces `accounts.jsonl`, and fsyncs the
containing directory after replacement. Publish the cookie before the record.
Remove the cookie when index publication fails before replacement; if the
replacement is already visible but its directory fsync is unconfirmed, retain
the referenced cookie and propagate the durability error. Do not write raw
email, mailbox credentials, artifact values, or cookie values to
`accounts.jsonl`. Session persistence uses a dedicated daemon owner so an
unconfirmed write cannot make `asyncio.run()` wait beyond the account deadline.

- [x] **Step 4: Implement exact page-state helpers**

In `register_claude_api.py`, define:

```python
PLATFORM_URL = "https://platform.claude.com/"

class ClaudeApiRegistrationError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code

async def apply_verification_artifact(page, artifact):
    code_input = page.locator('[data-testid="code"]')
    code_visible = await code_input.count() == 1 and await code_input.is_visible()
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
        await submit.click()
        return "code"
    raise ClaudeApiRegistrationError("verification_artifact_not_found")
```

Use only unique locators. For account selection, accept exact accessible names
`Personal account` and `Personal`; reject pages containing an organization-name
input or headings `Create an organization` / `Create your organization` when no
personal option exists. Do not click generic `Continue` on an organization
page.

Implement the selection helper exactly as a finite list of unique candidates:

```python
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
    if "create an organization" in heading_text or "create your organization" in heading_text:
        raise ClaudeApiRegistrationError("personal_account_not_available")
    return False
```

Implement console readiness as both conditions:

```python
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
```

- [x] **Step 5: Implement the orchestration function with one resend**

`run_claude_platform_flow()` must record the send timestamp, call the supplied
fetch callback, apply whichever artifact was returned, retry once only after
clicking the exact `Resend email` control, select personal, verify console, and
save the session. It returns the cookie path; it raises a stable
`ClaudeApiRegistrationError` for every terminal failure.

Implement it with this control flow:

```python
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
    return await _await_external_by_deadline(
        save_claude_platform_session(
            context,
            account.email,
            output_dir,
            operation_timeout=_remaining(deadline),
        ),
        deadline,
    )
```

- [x] **Step 6: Run state-machine tests**

Run:

```powershell
python -m unittest tests.test_claude_api_registration -v
```

Expected: all tests PASS and no test opens a real browser or network connection.

- [x] **Step 7: Commit the page flow and session exporter**

```powershell
git add common/claude_platform_session.py register_claude_api.py tests/test_claude_api_registration.py
git commit -m "feat: automate Claude Platform personal signup"
```

---

### Task 5: Standalone CLI, Provider Dispatch, Proxy, and Cleanup

**Files:**
- Modify: `register_claude_api.py`
- Create: `tests/test_claude_api_cli.py`

**Interfaces:**
- Consumes: `ClaudeEmailAccountStore(purpose="claude_api")`, NINEMALL and OUTLOOK artifact functions, existing account lease/browser provider/lifecycle helpers.
- Produces: `fetch_platform_verification(context, account, max_wait, received_after, account_lease=None, ninemail_client=None)`; `register_one(bb, account, account_store, timeout, account_lease=None)`; async `main()` with the repository success marker `success: X/Y`.

- [x] **Step 1: Write failing provider and lifecycle tests**

Test that NINEMALL calls only `poll_claude_platform_verification`, OUTLOOK uses
Graph then broker/browser, cancellation sets the NINEMALL event and awaits the
worker, profile creation receives the inherited IPMart lease, success marks the
API ledger, failure marks a safe code, and launch failure releases reservations.

Include a no-secret logging assertion:

```python
def test_ninemail_failure_output_redacts_credentials(self):
    output = io.StringIO()
    account = ClaudeEmailAccount(
        "NINEMALL", "person@example.com", "mail-pass",
        "client-guid", "refresh-secret",
    )
    with redirect_stdout(output):
        register_claude_api.log_flow_error(
            "registration_error", account=account
        )
    text = output.getvalue()
    self.assertNotIn("mail-pass", text)
    self.assertNotIn("client-guid", text)
    self.assertNotIn("refresh-secret", text)
```

- [x] **Step 2: Run CLI tests and verify RED**

Run:

```powershell
python -m unittest tests.test_claude_api_cli -v
```

Expected: FAIL because provider dispatch and CLI lifecycle are incomplete.

- [x] **Step 3: Implement strict provider dispatch**

Add this public async facade:

```python
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
        return await run_daemon_call(
            lambda: client.poll_claude_platform_verification(
                account,
                max_wait,
                received_after,
                cancel_event=cancel_event,
            ),
            max_wait,
            name="claude-api-ninemail-owner",
            on_cancel=cancel_event.set,
            cancel_grace=TASK_CANCEL_GRACE,
        )

    deadline = _clock() + max_wait
    channels = (["graph"] if account.refresh_token else [])
    if os.environ.get("MAILBOX_BROKER"):
        channels.append("broker")
    channels.append("browser")

    for index, channel in enumerate(channels):
        remaining = _remaining(deadline)
        if remaining <= 0:
            return None
        # A failed early channel cannot consume the later channels' shares.
        channel_budget = remaining / (len(channels) - index)
        channel_deadline = _clock() + channel_budget
        try:
            if channel == "graph":
                result = await run_daemon_call(
                    lambda: get_claude_platform_verification_by_token(
                        account.email,
                        account.refresh_token,
                        account.client_id,
                        channel_budget,
                        5,
                        received_after,
                        account_lease,
                    ),
                    channel_budget,
                    name="claude-api-graph-owner",
                )
            elif channel == "broker":
                result = await _await_external_by_deadline(
                    fetch_claude_platform_from_broker(
                        account.email,
                        account.password,
                        channel_budget,
                        received_after,
                    ),
                    channel_deadline,
                )
            else:
                outlook_page = None
                outlook_owner_unconfirmed = False
                try:
                    outlook_page = await _await_external_by_deadline(
                        context.new_page(), channel_deadline
                    )
                    result = await _await_external_by_deadline(
                        get_claude_platform_verification_outlook_pw(
                            outlook_page,
                            account.email,
                            account.password,
                            max_wait=max(0.0, channel_deadline - _clock()),
                            received_after=received_after,
                        ),
                        channel_deadline,
                    )
                except (
                    _OperationUnconfirmed,
                    _CancellationUnconfirmed,
                ):
                    outlook_owner_unconfirmed = True
                    raise
                finally:
                    if outlook_page is not None and not outlook_owner_unconfirmed:
                        close_awaitable = outlook_page.close()
                        if _remaining(deadline) > 0:
                            await _await_external_by_deadline(
                                close_awaitable, deadline
                            )
                        else:
                            _close_unawaited(close_awaitable)
            if result:
                return result
        except asyncio.TimeoutError:
            continue
    return None
```

The NINEMALL branch returns or raises before any OUTLOOK call. OUTLOOK divides
the one remaining mailbox budget across Graph, broker, and browser channels;
every channel receives the same `received_after` freshness baseline. A broker
response is revalidated locally for artifact shape, host/path, finite timestamp,
and freshness before it is accepted.

Every nested mailbox or Playwright await uses an owned bounded wait. If repeated
cancellation does not confirm that the nested operation stopped, it raises
`OperationUnconfirmed`/`CancellationUnconfirmed` rather than an ordinary
timeout. That outcome bypasses channel fallback and terminal ledger updates.
Graph and NINEMALL synchronous calls use daemon owners, never asyncio's default
executor. Because asyncio normally collapses a task that raises a
`CancellationUnconfirmed` subclass into generic cancelled state, each owned
wait also records its explicit ownership outcome on the task. An outer deadline
checks that outcome after bounded cancellation: it maps nested unconfirmed
cancellation to `OperationUnconfirmed`, while caller cancellation remains
`CancellationUnconfirmed`.

The standalone entry point uses a dedicated `_run_cli_event_loop(main())`
boundary rather than `asyncio.run(main())`. Python's standard runner cancels
and then joins every pending task without a timeout; that would hang forever
when an owned Playwright/mailbox task has already rejected bounded
cancellation. The CLI runner first cancels and boundedly drains ordinary tasks
and, when no live owner remains, boundedly closes async generators. It closes
the loop without rejoining tasks retained as unconfirmed. Their mailbox
reservations remain reserved and their profiles remain undeleted for operator
recovery at process exit.

Construct the NINEMALL client with this concrete helper:

```python
def build_ninemail_client():
    return NineMallMailboxClient(
        base_url=NINEMALL_API_BASE,
        api_password=NINEMALL_API_PASSWORD,
        http_timeout=NINEMALL_HTTP_TIMEOUT,
        poll_interval=NINEMALL_POLL_INTERVAL,
    )

```

- [x] **Step 4: Implement CLI and browser lifecycle**

Support these arguments exactly:

```text
--count/-n
--concurrency/-c
--timeout/-t
--emails/-e
--email
--password
--token
--client-id
--node
--proxy-port
```

Use `ClaudeEmailAccountStore(provider=provider, source_file=source, purpose="claude_api")`. For explicit
NINEMALL email, require token and client ID. Reuse account-lease parsing,
IPMart verification, `bitbrowser_proxy_fields`, browser provider creation,
Playwright CDP connection, and `common.process_lifecycle` shutdown confirmation
patterns already used by `register.py` and the NINEMALL entry points.

`register_one()` must clean up its temporary profile before publishing a
terminal ledger entry. A timed-out or cancelled operation that may still own a
browser/profile is not a completed failure: keep the reservation in its
`reserved` state for conservative recovery.

Start `account_deadline` in the account worker before optional IPMart
acquisition and pass it into `register_one()`. Use a daemon-backed serialized
owner for blocking BitBrowser operations so a hung provider call cannot block
CLI shutdown or race a later close/delete. The lifecycle shape is:

```python
async def register_one(
    bb,
    account,
    account_store,
    timeout,
    account_lease=None,
    deadline=None,
):
    deadline = deadline or (_clock() + timeout)
    operations = _ProfileOperations(bb)
    profile_id = None
    browser = None
    cookie_path = None
    error_code = None
    progress = {}
    async_owner_unconfirmed = False

    async def profile_call(submit):
        remaining = _remaining(deadline)
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await operations.wait(submit(), remaining)

    try:
        profile_id = await profile_call(
            lambda: operations.create(
                f"claude_api_{int(time.time())}",
                bitbrowser_proxy_fields(account_lease)
                if account_lease else {},
            )
        )
        opened = await profile_call(operations.open)

        async def browser_registration():
            nonlocal browser
            async with async_playwright() as playwright:
                browser = await playwright.chromium.connect_over_cdp(opened["ws"])
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()
                available = _remaining(deadline)
                # This margin stays inside the same absolute deadline. It lets
                # the inner state machine unwind with a contextual phase code.
                settle = _deadline_settle_margin(available)
                return await run_claude_platform_flow(
                    page,
                    context,
                    account,
                    functools.partial(
                        fetch_platform_verification,
                        account_lease=account_lease,
                    ),
                    max_wait=max(0.0, available - settle),
                    progress=progress,
                )

        cookie_path = await _run_owned_async(
            browser_registration(),
            _remaining(deadline),
            cancel_grace=TASK_CANCEL_GRACE,
        )
    except _CancellationUnconfirmed as exc:
        cancellation = exc
        async_owner_unconfirmed = True
    except _OperationUnconfirmed:
        error_code = "timeout"
        async_owner_unconfirmed = True
    except asyncio.TimeoutError:
        error_code = {
            "mail": "mail_timeout",
            "code_confirmation": "verification_rejected",
            "console": "console_not_reached",
            "session": "console_not_reached",
        }.get(progress.get("phase"), "timeout")
    except asyncio.CancelledError as exc:
        cancellation = exc
    except (ClaudeApiRegistrationError, NineMallMailboxError) as exc:
        error_code = exc.code
    except BaseException as exc:
        escaped = exc
        error_code = "registration_error"

    cleanup_complete, cleanup_cancellation = await _shield_registration_cleanup(
        _cleanup_registration_resources(
            bb,
            browser,
            profile_id,
            profile_operations=operations,
            async_owner_unconfirmed=async_owner_unconfirmed,
            operation_timeout=min(CLEANUP_OPERATION_TIMEOUT, timeout),
        )
    )
    if cancellation is None:
        cancellation = cleanup_cancellation
    if not cleanup_complete:
        # Ownership is unresolved: preserve cancellation/exception identity,
        # but never append ok/error/released.
        if cancellation is not None:
            raise cancellation
        if escaped is not None:
            raise escaped
        return None
    if cookie_path is not None:
        finalized = _finalize_account_safely(account_store.mark_used, account)
        if cancellation is not None:
            raise cancellation
        return cookie_path if finalized else None
    if cancellation is not None:
        _finalize_account_safely(account_store.release, account)
        raise cancellation
    if escaped is not None:
        _finalize_account_safely(account_store.mark_error, account, error_code)
        raise escaped
    if error_code is not None:
        _finalize_account_safely(account_store.mark_error, account, error_code)
    return None
```

`_cleanup_registration_resources()` first confirms the owned Playwright
operation has stopped, then closes the Playwright browser. It submits
`operations.close()` and awaits completion; only a successfully completed close
may enqueue `operations.delete()`. A close timeout or exception returns
unconfirmed cleanup without issuing delete. `_shield_registration_cleanup()`
records caller cancellation but lets this bounded ownership check finish.

The account-worker deadline setup must share the same budget with proxy
acquisition:

```python
account_deadline = _clock() + args.timeout
if ipmart_settings.enabled:
    account_lease = await _run_sync_call_daemon(
        acquire_proxy,
        _remaining(account_deadline),
        "claude-api-proxy-acquisition",
    )
return await register_one(
    bb,
    account,
    account_store,
    args.timeout,
    account_lease=account_lease,
    deadline=account_deadline,
)
```

On success call `store.mark_used(account)`. On a stable flow/mailbox error and
confirmed cleanup call `store.mark_error(account, error.code)`. Release only a
pre-profile or confirmed-cancellation reservation. Never turn an unconfirmed
owner into success, error, or release.

The obsolete synchronous pattern is intentionally not used: direct
`bb.create_browser()` / `bb.open_browser()` calls can escape the account
deadline, and unconditional close/delete can delete a profile while its open or
Playwright task still owns it.

Print exactly one final line in this format:

```python
print(f"success: {success_count}/{len(accounts)}")
```

Exit zero only when every requested account succeeds.

- [x] **Step 5: Run CLI, proxy, and lifecycle tests**

Run:

```powershell
python -m unittest tests.test_claude_api_cli tests.test_claude_ipmart_proxy tests.test_claude_ninemail_cli -v
```

Expected: all tests PASS; existing Claude CLI behavior remains green.

- [x] **Step 6: Commit the executable flow**

```powershell
git add register_claude_api.py tests/test_claude_api_cli.py
git commit -m "feat: add Claude API registration CLI"
```

---

### Task 6: Orchestrator Integration and Shared Claude-Family Reservation

**Files:**
- Modify: `register_three_platforms.py:83-400`
- Modify: `run_full_flow.py:219-455`
- Create: `tests/test_claude_api_entrypoints.py`
- Modify: `tests/test_claude_ninemail_entrypoints.py`
- Modify: `tests/test_full_flow_ipmart_proxy.py`
- Modify: `tests/test_platform_proxy_env.py`

**Interfaces:**
- Consumes: standalone CLI, `reserve_shared_claude_account`, existing reserved-process owners, IPMart lease environment.
- Produces: `claude_api` command construction; Claude-family predicate; per-platform ledger finalization for shared runs.

- [x] **Step 1: Write failing command and routing tests**

Test these exact expectations:

```python
def test_claude_api_command_forwards_mailbox_credentials(self):
    command = register_three_platforms.build_command(
        "claude_api",
        platform_args(["claude_api"]),
        ("person@example.com", "mail-pass", "refresh-secret", "client-guid"),
    )
    self.assertEqual(command[2], "register_claude_api.py")
    self.assertEqual(command[command.index("--token") + 1], "refresh-secret")
    self.assertEqual(command[command.index("--client-id") + 1], "client-guid")

def test_claude_family_only_predicate_accepts_both_claude_choices(self):
    self.assertTrue(run_full_flow.is_ninemail_claude_family_only(
        argparse.Namespace(platforms=["claude_api"]),
        {"EMAIL_PROVIDER": "NINEMALL"},
    ))
    self.assertTrue(run_full_flow.is_ninemail_claude_family_only(
        argparse.Namespace(platforms=["claude", "claude_api"]),
        {"EMAIL_PROVIDER": "NINEMALL"},
    ))
    self.assertFalse(run_full_flow.is_ninemail_claude_family_only(
        argparse.Namespace(platforms=["claude_api", "chatgpt"]),
        {"EMAIL_PROVIDER": "NINEMALL"},
    ))
```

Also test that both Claude-family children receive the account lease, while
ChatGPT/Grok still have account-proxy variables stripped.

- [x] **Step 2: Run entry-point tests and verify RED**

Run:

```powershell
python -m unittest tests.test_claude_api_entrypoints tests.test_claude_ninemail_entrypoints tests.test_platform_proxy_env tests.test_full_flow_ipmart_proxy -v
```

Expected: FAIL because `claude_api` is not an allowed platform and the family predicate is absent.

- [x] **Step 3: Add command construction and choices**

In `register_three_platforms.build_command()` add:

```python
if platform == "claude_api":
    cmd = [
        sys.executable, "-u", "register_claude_api.py",
        "--count", "1",
        "--concurrency", "1",
        "--timeout", timeout,
        "--email", email,
        "--password", password or "",
        "--node", args.node,
    ]
    if token:
        cmd += ["--token", token]
    if client_id:
        cmd += ["--client-id", client_id]
    return cmd
```

Add `claude_api` to both argparse choices. Treat `claude` and `claude_api` as
Claude-family platforms for IPMart ordering and HTTP-proxy stripping.

- [x] **Step 4: Generalize purpose-ledger routing without leaking it to mixed platforms**

Define:

```python
CLAUDE_FAMILY = {"claude", "claude_api"}

def is_claude_family_only(platforms):
    selected = set(platforms)
    return bool(selected) and selected <= CLAUDE_FAMILY
```

In `register_three_platforms.py --from-pool`, replace exact `{"claude"}` checks
where they govern purpose-ledger selection, broker release, exit status, and
Claude-family child environment. Pure `claude_api` uses the normalized current
provider (NINEMALL or OUTLOOK), and a dual-family run reserves both purposes.
Pure OUTLOOK `claude` retains the legacy `tri` pool. Mixed ChatGPT/Grok runs
also continue using `tri` and force `EMAIL_PROVIDER=OUTLOOK` for the
Claude-family child, preserving existing mailbox behavior.

For a two-child Claude-family run, use `reserve_shared_claude_account()` and
attach both stores to the reserved process owner. After each child result,
leave the child's own terminal state intact; on launch/cancellation before a
terminal mark, release only that child's purpose reservation. Never append an
`ok` mark in the parent based solely on exit code—the child owns semantic
success.

Change `_ReservedPoolAccount` to hold a store mapping and release every still
nonterminal reservation:

```python
class _ReservedPoolAccount(tuple):
    def __new__(cls, account, stores):
        values = (
            account.email,
            account.password,
            account.refresh_token,
            account.client_id,
        )
        instance = super().__new__(cls, values)
        instance.account = account
        instance.stores = dict(stores)
        instance.active = True
        instance.owned_processes = set()
        return instance

    def track_process(self, process):
        self.owned_processes.add(id(process))

    def confirm_process_stopped(self, process, confirmed):
        if confirmed:
            self.owned_processes.discard(id(process))

    def release(self):
        if not self.active:
            return False
        if self.owned_processes:
            return False
        released = False
        for store in self.stores.values():
            released = store.release(self.account) or released
        self.active = False
        return released
```

Build the reservation with:

```python
provider = normalize_email_provider(os.environ.get("EMAIL_PROVIDER"))
if is_claude_family_only(args.platforms):
    purposes = tuple(dict.fromkeys(
        platform for platform in args.platforms if platform in CLAUDE_FAMILY
    ))
    if provider == "OUTLOOK" and purposes == ("claude",):
        return email_pool.next_email("tri", display="masked")
    if len(purposes) > 1:
        result = reserve_shared_claude_account(provider, purposes)
        if result is None:
            return None
        account, stores = result
    else:
        store = ClaudeEmailAccountStore(provider=provider, purpose=purposes[0])
        account = store.reserve_one()
        if account is None:
            return None
        stores = {purposes[0]: store}
    return _ReservedPoolAccount(account, stores)
return email_pool.next_email("tri", display="masked")
```

Keep `active=True` while any tracked child process remains unconfirmed so a
later confirmed shutdown can retry release. Mask complete email addresses in
both the parent summary and relayed child output without modifying command
arguments or account objects. If the run includes `claude_api`, propagate
launch failure, nonzero child exit, or missing success marker as a nonzero
orchestrator exit. Recognize success only when the final nonempty child-output
line is exactly `success: 1/1`; a substring mention is not success. Retain the
historical exit convention for runs that do not include `claude_api`.

- [x] **Step 5: Generalize full-flow lease and platform environment logic**

Rename `is_ninemail_claude_only()` to
`is_ninemail_claude_family_only()`. Include `claude_api` when deciding whether
to acquire and re-verify an account lease. Restore inherited HTTP proxy values
only when at least one requested platform is outside the Claude family.

This full-flow change remains intentionally provider-specific: bypass Stage A
and use purpose-ledger reservation only when `EMAIL_PROVIDER=NINEMALL` and all
selected platforms are in the Claude family. Explicit OUTLOOK continues the
existing Stage A mailbox-registration workflow; do not replace it with the
provider-agnostic `register_three_platforms.py --from-pool` behavior.

Update argparse choices to:

```python
choices=["claude", "claude_api", "chatgpt", "grok"]
```

Use these exact family tests in `run_once()`:

```python
needs_account_lease = (
    not args.skip_email
    or any(platform in CLAUDE_FAMILY for platform in args.platforms)
)

if account_lease is not None and any(
    platform in CLAUDE_FAMILY for platform in args.platforms
):
    verify(
        account_lease,
        expected_exit_ip=account_lease.exit_ip,
        env=round_env,
    )

if account_lease is not None and any(
    platform not in CLAUDE_FAMILY for platform in args.platforms
):
    platform_env = dict(round_env)
    platform_env.update(original_http_proxy_env)
```

- [x] **Step 6: Run focused orchestrator tests and dry-run commands**

Run:

```powershell
python -m unittest tests.test_claude_api_entrypoints tests.test_claude_ninemail_entrypoints tests.test_platform_proxy_env tests.test_full_flow_ipmart_proxy -v
python register_three_platforms.py --email test@example.com --password x --token rt --client-id cid --platforms claude_api --timeout 1 --help
python run_full_flow.py --platforms claude_api --dry-run
```

Expected: tests PASS; help exits zero; dry-run prints redacted orchestration and creates no external account or proxy lease.

- [x] **Step 7: Commit orchestrator integration**

```powershell
git add register_three_platforms.py run_full_flow.py tests/test_claude_api_entrypoints.py tests/test_claude_ninemail_entrypoints.py tests/test_platform_proxy_env.py tests/test_full_flow_ipmart_proxy.py
git commit -m "feat: orchestrate Claude API registration"
```

---

### Task 7: WebUI, Documentation, and Full Regression Verification

**Files:**
- Modify: `webui/scripts.py:16-405`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `.env.example` only if an existing comment must mention Claude Platform
- Modify: `tests/test_claude_api_entrypoints.py`

**Interfaces:**
- Consumes: standalone CLI and orchestrator choices.
- Produces: WebUI card `register_claude_api`; `claude_api` multi-select choices; documented commands, outputs, and recharge boundary.

- [x] **Step 1: Write failing WebUI schema and redaction tests**

Add:

```python
def test_webui_exposes_standalone_claude_api_registration(self):
    item = scripts.script_by_id("register_claude_api")
    self.assertEqual(item["file"], "register_claude_api.py")
    secret_flags = {
        spec["flag"] for spec in item["args"] if spec.get("secret")
    }
    self.assertEqual(
        {"--password", "--token", "--client-id"} <= secret_flags,
        True,
    )

def test_orchestrator_platform_choices_include_claude_api(self):
    for script_id in ("run_full_flow", "register_three_platforms"):
        item = scripts.script_by_id(script_id)
        platforms = next(
            spec for spec in item["args"] if spec["flag"] == "--platforms"
        )
        self.assertIn("claude_api", platforms["choices"])
```

- [x] **Step 2: Run WebUI tests and verify RED**

Run:

```powershell
python -m unittest tests.test_claude_api_entrypoints tests.test_webui_env_reload -v
```

Expected: FAIL because WebUI metadata lacks `claude_api`.

- [x] **Step 3: Add the WebUI card and choices**

Add a `SCRIPTS` entry:

```python
{
    "id": "register_claude_api",
    "file": "register_claude_api.py",
    "category": "单平台注册",
    "title": "Claude API 注册",
    "desc": "注册 platform.claude.com 个人账号并保存登录会话；不创建组织、不充值。",
    "args": [
        {"flag": "--count", "type": "int", "default": 1, "help": "注册数量"},
        {"flag": "--concurrency", "type": "int", "default": 1, "help": "并发数"},
        {"flag": "--timeout", "type": "int", "default": 480, "help": "单号超时(秒)"},
        {"flag": "--email", "type": "str", "default": "", "help": "指定邮箱(调试)"},
        {"flag": "--password", "type": "str", "default": "", "secret": True, "help": "邮箱密码"},
        {"flag": "--token", "type": "str", "default": "", "secret": True, "help": "refresh token"},
        {"flag": "--client-id", "type": "str", "default": "", "secret": True, "help": "OAuth client_id"},
        {"flag": "--node", "type": "str", "default": "none", "help": "Clash 节点(none=不切)"},
    ],
}
```

Add `claude_api` to both existing multi-select choice lists. Rely on the
existing `_redact_cmd()` secret metadata; do not add special-case command
string manipulation.

- [x] **Step 4: Document commands, state files, and non-goals**

Update README examples with:

```powershell
python register_claude_api.py --count 1
python run_full_flow.py --platforms claude_api
python register_three_platforms.py --from-pool --platforms claude claude_api
```

Document NINEMALL dual-artifact behavior, the four new purpose-state files,
`cookies/claude_api/`, strict no-OUTLOOK fallback, personal-only selection,
and that API-key creation/recharge are not yet performed. Add one dated
CHANGELOG section describing the same delivered behavior.

- [x] **Step 5: Run the complete relevant suite**

Run:

```powershell
python -m unittest tests.test_claude_email_accounts tests.test_claude_platform_mailbox tests.test_ninemail_mailbox tests.test_claude_api_registration tests.test_claude_api_cli tests.test_claude_api_entrypoints tests.test_claude_mailbox_routing tests.test_claude_ninemail_cli tests.test_claude_ninemail_entrypoints tests.test_claude_ipmart_proxy tests.test_mailbox_account_proxy tests.test_platform_proxy_env tests.test_full_flow_ipmart_proxy tests.test_webui_env_reload -v
python -m py_compile common/claude_email_accounts.py common/claude_platform_mailbox.py common/ninemail_mailbox.py common/claude_platform_session.py mailbox_broker.py register_claude_api.py register_three_platforms.py run_full_flow.py webui/scripts.py
git diff --check
```

Expected: all listed tests PASS, compilation exits zero, and `git diff --check` prints nothing.

- [x] **Step 6: Perform a no-side-effect smoke check**

Run:

```powershell
python register_claude_api.py --help
python run_full_flow.py --platforms claude_api --dry-run
git status --short
```

Expected: help and dry-run exit zero; no NINEMALL request, Microsoft request,
proxy allocation, browser profile, Anthropic account, API key, or recharge is
created. `git status` lists only intentional implementation and documentation
changes.

- [x] **Step 7: Commit WebUI and documentation**

```powershell
git add webui/scripts.py README.md CHANGELOG.md .env.example tests/test_claude_api_entrypoints.py
git commit -m "docs: expose Claude API registration"
```

- [x] **Step 8: Review the final branch diff**

Run:

```powershell
git status --short
git log --oneline -8
git diff --stat HEAD~7..HEAD
```

Expected: worktree clean; seven focused feature commits after the plan commit;
the diff contains no recharge implementation and no credential/state files.
