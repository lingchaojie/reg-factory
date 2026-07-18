# NexaCard OTP Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local HTTP service that uses direct native Chrome automation to return the nearest post-order NexaCardB or 3D-1 payment OTP and recovers expired NexaCard sessions through Gmail OAuth on demand.

**Architecture:** A standalone `nexacard_otp` package owns configuration, Google authorization, Gmail reading, a persistent native-Chrome context, NexaCard login recovery, OTP matching, and a FastAPI endpoint. The existing WebUI stores non-OAuth configuration, launches the service, and exposes Google authorize/reauthorize/status controls; OAuth files and the Chrome profile live in a Git-ignored project-private directory.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, Playwright async API with installed Google Chrome, Google Auth/OAuthlib/Gmail API clients, `zoneinfo`, `unittest`, and the existing vanilla HTML/CSS/JavaScript WebUI.

## Global Constraints

- Use native `chrome.exe`; do not use bundled Chromium, a proxy, or a fingerprint browser.
- Launch Chrome with `--no-proxy-server`, `--proxy-server=direct://`, and `--proxy-bypass-list=*`, and remove all HTTP/HTTPS/ALL proxy variables from its child environment.
- Default `NEXACARD_HEADLESS=true`, `NEXACARD_PAGE_TIMEZONE=Asia/Shanghai`, `NEXACARD_OTP_POLL_INTERVAL_SECONDS=3`, and `NEXACARD_OTP_MAX_ATTEMPTS=100`.
- Read polling configuration once at the start of each request; WebUI saves apply to the next request without service restart.
- Match the full normalized card number and require OTP `created` to be strictly later than `order_created_at`; return the smallest positive time difference.
- Detect NexaCard logout only while handling an OTP request; do not run background login polling.
- Successful `POST /v1/otp` responses contain only `{"otp":"123456"}`.
- Keep `nexacard_otp/private/`, Chrome profiles, credentials, tokens, and OAuth metadata out of Git and logs.
- Never log passwords, full card numbers, Gmail login codes, payment OTPs, OAuth tokens, authorization headers, or NexaCard signatures.
- Use TDD for every behavior change and commit after every independently passing task.

---

## File Structure

Create these focused modules:

- `nexacard_otp/settings.py`: fixed private paths, `.env` parsing, defaults, validation, Chrome discovery, and legacy OAuth-file migration.
- `nexacard_otp/models.py`: canonical card types, parsed lookup input, OTP rows, and authorization status.
- `nexacard_otp/errors.py`: typed domain failures used by browser, Gmail, lookup, and API layers.
- `nexacard_otp/matching.py`: normalization, timestamp parsing, card-route mapping, and nearest-OTP selection.
- `nexacard_otp/gmail_auth.py`: OAuth start/callback, atomic token/meta persistence, authorized-email verification, token refresh, and status classification.
- `nexacard_otp/gmail_reader.py`: raw Gmail query, MIME parsing, and fresh nine-digit NexaCard login-code polling.
- `nexacard_otp/browser.py`: direct-environment construction and the persistent native-Chrome context manager.
- `nexacard_otp/login.py`: logout detection and serialized email-verification login recovery.
- `nexacard_otp/lookup.py`: verification-page search, pagination, bounded polling, and concurrent request isolation.
- `nexacard_otp/app.py`: FastAPI request/response and error mapping.
- `nexacard_otp_service.py`: Uvicorn entry point using current `.env` host/port values.

Modify these existing integration files:

- `requirements.txt`: Google API dependencies.
- `.gitignore`: private NexaCard runtime directory.
- `.env.example`: NexaCard service defaults.
- `webui/scripts.py`: configuration schema and long-running service entry.
- `webui/server.py`: Gmail authorization/status endpoints and NexaCard service connectivity test.
- `webui/static/app.js`: authorization controls and status rendering.
- `webui/static/style.css`: compact OAuth controls and status styling.
- `README.md` and `CHANGELOG.md`: setup, API, security, and release notes.

---

### Task 1: Configuration, Private Paths, and Domain Types

**Files:**
- Create: `nexacard_otp/__init__.py`
- Create: `nexacard_otp/settings.py`
- Create: `nexacard_otp/models.py`
- Create: `nexacard_otp/errors.py`
- Modify: `requirements.txt`
- Modify: `.gitignore`
- Modify: `.env.example`
- Test: `tests/test_nexacard_settings.py`

**Interfaces:**
- Produces: `Settings`, `load_settings(env_path: Path) -> Settings`, `ensure_private_oauth_files() -> None`, `CardType`, `LookupInput`, `OtpRow`, `AuthStatus`, and typed exceptions.
- Consumes: no new project interfaces.

- [ ] **Step 1: Write failing configuration and migration tests**

```python
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nexacard_otp.models import CardType
from nexacard_otp.settings import (
    PRIVATE_CREDENTIALS_PATH,
    PRIVATE_TOKEN_PATH,
    ensure_private_oauth_files,
    load_settings,
)


class NexaCardSettingsTests(unittest.TestCase):
    def test_defaults_are_headless_shanghai_three_seconds_one_hundred(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(Path(directory) / ".env")
        self.assertTrue(settings.headless)
        self.assertEqual(settings.page_timezone.key, "Asia/Shanghai")
        self.assertEqual(settings.poll_interval_seconds, 3.0)
        self.assertEqual(settings.max_attempts, 100)
        self.assertEqual(settings.service_host, "127.0.0.1")

    def test_current_env_file_values_override_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "NEXACARD_ACCOUNT=user1\n"
                "NEXACARD_PASSWORD=secret1\n"
                "NEXACARD_VERIFICATION_EMAIL=mail@example.com\n"
                "NEXACARD_HEADLESS=false\n"
                "NEXACARD_OTP_POLL_INTERVAL_SECONDS=4.5\n"
                "NEXACARD_OTP_MAX_ATTEMPTS=12\n",
                encoding="utf-8",
            )
            settings = load_settings(env_path)
        self.assertEqual(settings.account, "user1")
        self.assertFalse(settings.headless)
        self.assertEqual(settings.poll_interval_seconds, 4.5)
        self.assertEqual(settings.max_attempts, 12)

    def test_changed_env_file_beats_stale_service_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("NEXACARD_OTP_MAX_ATTEMPTS=25\n", encoding="utf-8")
            with patch.dict(os.environ, {"NEXACARD_OTP_MAX_ATTEMPTS": "100"}):
                settings = load_settings(env_path)
        self.assertEqual(settings.max_attempts, 25)

    def test_non_positive_polling_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "NEXACARD_OTP_POLL_INTERVAL_SECONDS=0\n"
                "NEXACARD_OTP_MAX_ATTEMPTS=-1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "positive"):
                load_settings(env_path)

    def test_legacy_oauth_files_copy_only_when_private_files_are_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_credentials = root / "source-credentials.json"
            source_token = root / "source-token.json"
            destination_credentials = root / "private" / "credentials.json"
            destination_token = root / "private" / "token.json"
            source_credentials.write_text('{"installed": {}}', encoding="utf-8")
            source_token.write_text('{"refresh_token": "old"}', encoding="utf-8")
            with patch("nexacard_otp.settings.PRIVATE_CREDENTIALS_PATH", destination_credentials), patch(
                "nexacard_otp.settings.PRIVATE_TOKEN_PATH", destination_token
            ), patch("nexacard_otp.settings.LEGACY_CREDENTIALS_PATH", source_credentials), patch(
                "nexacard_otp.settings.LEGACY_TOKEN_PATH", source_token
            ):
                ensure_private_oauth_files()
                destination_token.write_text('{"refresh_token": "new"}', encoding="utf-8")
                ensure_private_oauth_files()
            self.assertTrue(destination_credentials.exists())
            self.assertEqual(destination_token.read_text(encoding="utf-8"), '{"refresh_token": "new"}')

    def test_card_type_values_are_stable_api_names(self):
        self.assertEqual(CardType.NEXACARD_B.value, "NexaCardB")
        self.assertEqual(CardType.THREE_D_1.value, "3D-1卡")
```

- [ ] **Step 2: Run the tests and verify the package is missing**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_settings -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'nexacard_otp'`.

- [ ] **Step 3: Add domain types and typed failures**

```python
# nexacard_otp/models.py
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class CardType(str, Enum):
    NEXACARD_B = "NexaCardB"
    THREE_D_1 = "3D-1卡"


@dataclass(frozen=True)
class LookupInput:
    card_number: str
    card_type: CardType
    order_created_at: datetime


@dataclass(frozen=True)
class OtpRow:
    record_id: int
    otp: str
    card_number: str
    created_at: datetime


@dataclass(frozen=True)
class AuthStatus:
    state: str
    message: str
    authorized_email: str | None = None
    estimated_expires_at: datetime | None = None
    estimated: bool = False
```

```python
# nexacard_otp/errors.py
class NexaCardOtpError(RuntimeError):
    code = "nexacard_otp_error"


class InvalidLookupInput(NexaCardOtpError):
    code = "invalid_lookup_input"


class GmailAuthorizationRequired(NexaCardOtpError):
    code = "gmail_authorization_required"


class GmailTemporarilyUnavailable(NexaCardOtpError):
    code = "gmail_temporarily_unavailable"


class NexaCardLoginFailed(NexaCardOtpError):
    code = "nexacard_login_failed"


class NexaCardPageError(NexaCardOtpError):
    code = "nexacard_page_error"


class NexaCardTransientError(NexaCardOtpError):
    code = "nexacard_temporarily_unavailable"


class OtpLookupTimedOut(NexaCardOtpError):
    code = "otp_lookup_timed_out"
```

- [ ] **Step 4: Implement validated settings and one-time private-file migration**

```python
# nexacard_otp/settings.py
import os
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIR = ROOT / "nexacard_otp" / "private"
PRIVATE_CREDENTIALS_PATH = PRIVATE_DIR / "credentials.json"
PRIVATE_TOKEN_PATH = PRIVATE_DIR / "token.json"
PRIVATE_TOKEN_META_PATH = PRIVATE_DIR / "token.meta.json"
CHROME_PROFILE_DIR = PRIVATE_DIR / "chrome-profile"
LEGACY_CREDENTIALS_PATH = Path(r"D:\Gmail API李\credentials.json")
LEGACY_TOKEN_PATH = Path(r"D:\Gmail API李\token.json")


@dataclass(frozen=True)
class Settings:
    account: str
    password: str
    verification_email: str
    headless: bool
    chrome_path: Path
    page_timezone: ZoneInfo
    poll_interval_seconds: float
    max_attempts: int
    service_host: str
    service_port: int

    @property
    def browser_fingerprint(self) -> tuple[str, bool, str, str, str]:
        password_digest = sha256(self.password.encode("utf-8")).hexdigest()
        return (
            str(self.chrome_path),
            self.headless,
            self.account,
            self.verification_email,
            password_digest,
        )


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _positive_float(value: str, name: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _positive_int(value: str, name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def discover_chrome(explicit: str = "") -> Path:
    candidates = [
        Path(explicit) if explicit else None,
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise FileNotFoundError("Google Chrome executable was not found")


def load_settings(env_path: Path = ROOT / ".env") -> Settings:
    file_values = _read_env(env_path)
    values = {
        key: value for key, value in os.environ.items() if key.startswith("NEXACARD_")
    }
    values.update(file_values)
    return Settings(
        account=values.get("NEXACARD_ACCOUNT", "").strip(),
        password=values.get("NEXACARD_PASSWORD", ""),
        verification_email=values.get("NEXACARD_VERIFICATION_EMAIL", "").strip().lower(),
        headless=_bool(values.get("NEXACARD_HEADLESS", "true")),
        chrome_path=discover_chrome(values.get("NEXACARD_CHROME_PATH", "")),
        page_timezone=ZoneInfo(values.get("NEXACARD_PAGE_TIMEZONE", "Asia/Shanghai")),
        poll_interval_seconds=_positive_float(
            values.get("NEXACARD_OTP_POLL_INTERVAL_SECONDS", "3"),
            "NEXACARD_OTP_POLL_INTERVAL_SECONDS",
        ),
        max_attempts=_positive_int(
            values.get("NEXACARD_OTP_MAX_ATTEMPTS", "100"),
            "NEXACARD_OTP_MAX_ATTEMPTS",
        ),
        service_host=values.get("NEXACARD_SERVICE_HOST", "127.0.0.1").strip(),
        service_port=_positive_int(values.get("NEXACARD_SERVICE_PORT", "8811"), "NEXACARD_SERVICE_PORT"),
    )


def ensure_private_oauth_files() -> None:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    for source, destination in (
        (LEGACY_CREDENTIALS_PATH, PRIVATE_CREDENTIALS_PATH),
        (LEGACY_TOKEN_PATH, PRIVATE_TOKEN_PATH),
    ):
        if not destination.exists() and source.is_file():
            shutil.copy2(source, destination)
```

- [ ] **Step 5: Add dependencies, defaults, and ignore rules**

Append to `requirements.txt`:

```text
google-api-python-client>=2.100.0
google-auth-httplib2>=0.2.0
google-auth-oauthlib>=1.2.0
```

Append to `.gitignore`:

```text
# NexaCard OTP private OAuth/browser runtime
nexacard_otp/private/
```

Append to `.env.example`:

```text
# NexaCard OTP service
NEXACARD_ACCOUNT=
NEXACARD_PASSWORD=
NEXACARD_VERIFICATION_EMAIL=
NEXACARD_HEADLESS=true
NEXACARD_CHROME_PATH=
NEXACARD_PAGE_TIMEZONE=Asia/Shanghai
NEXACARD_OTP_POLL_INTERVAL_SECONDS=3
NEXACARD_OTP_MAX_ATTEMPTS=100
NEXACARD_SERVICE_HOST=127.0.0.1
NEXACARD_SERVICE_PORT=8811
```

- [ ] **Step 6: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_settings -v`

Expected: all `NexaCardSettingsTests` pass.

- [ ] **Step 7: Commit**

```powershell
git add requirements.txt .gitignore .env.example nexacard_otp tests/test_nexacard_settings.py
git commit -m "feat: add NexaCard OTP configuration core"
```

---

### Task 2: Card Normalization and Nearest-OTP Matching

**Files:**
- Create: `nexacard_otp/matching.py`
- Test: `tests/test_nexacard_matching.py`

**Interfaces:**
- Consumes: `CardType`, `LookupInput`, `OtpRow`, and `InvalidLookupInput` from Task 1.
- Produces: `parse_lookup_input(card_number, card_type, order_created_at, timezone) -> LookupInput`, `route_for(CardType) -> str`, and `select_nearest_otp(rows, lookup) -> OtpRow | None`.

- [ ] **Step 1: Write failing normalization and matching tests**

```python
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from nexacard_otp.matching import parse_lookup_input, route_for, select_nearest_otp
from nexacard_otp.models import CardType, OtpRow


class NexaCardMatchingTests(unittest.TestCase):
    def setUp(self):
        self.zone = ZoneInfo("Asia/Shanghai")

    def test_aliases_map_to_confirmed_routes(self):
        b = parse_lookup_input("6500-0000-0000-0037", "nexacardb", "2026-07-19 03:00:00", self.zone)
        three_d = parse_lookup_input("6500 0000 0000 0037", "3d-1", "2026-07-19 03:00:00", self.zone)
        self.assertEqual(b.card_number, "6500000000000037")
        self.assertEqual(route_for(b.card_type), "/nova-v-card-b/verify-code")
        self.assertEqual(route_for(three_d.card_type), "/3d-1-card/verify-code")

    def test_naive_order_time_uses_page_timezone(self):
        lookup = parse_lookup_input("6500000000000037", "NexaCardB", "2026-07-19 03:00:00", self.zone)
        self.assertEqual(lookup.order_created_at.utcoffset().total_seconds(), 28800)

    def test_aware_order_time_converts_to_page_timezone(self):
        lookup = parse_lookup_input("6500000000000037", "NexaCardB", "2026-07-18T19:00:00Z", self.zone)
        self.assertEqual(lookup.order_created_at.hour, 3)

    def test_equal_time_is_rejected_and_nearest_strictly_later_row_wins(self):
        lookup = parse_lookup_input("6500000000000037", "NexaCardB", "2026-07-19 03:00:00", self.zone)
        rows = [
            OtpRow(1, "111111", "6500000000000037", datetime(2026, 7, 19, 3, 0, tzinfo=self.zone)),
            OtpRow(2, "222222", "6500000000000037", datetime(2026, 7, 19, 3, 0, 3, tzinfo=self.zone)),
            OtpRow(3, "333333", "6500000000000037", datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone)),
            OtpRow(4, "444444", "6500000000009999", datetime(2026, 7, 19, 3, 0, 0, 500000, tzinfo=self.zone)),
        ]
        self.assertEqual(select_nearest_otp(rows, lookup).otp, "333333")

    def test_same_timestamp_prefers_highest_record_id(self):
        lookup = parse_lookup_input("6500000000000037", "3D-1卡", "2026-07-19 03:00:00", self.zone)
        created = datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.zone)
        rows = [
            OtpRow(8, "888888", lookup.card_number, created),
            OtpRow(9, "999999", lookup.card_number, created),
        ]
        self.assertEqual(select_nearest_otp(rows, lookup).otp, "999999")
```

- [ ] **Step 2: Run tests and verify missing module failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_matching -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'nexacard_otp.matching'`.

- [ ] **Step 3: Implement normalization, route mapping, and strict selection**

```python
# nexacard_otp/matching.py
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .errors import InvalidLookupInput
from .models import CardType, LookupInput, OtpRow

ROUTES = {
    CardType.NEXACARD_B: "/nova-v-card-b/verify-code",
    CardType.THREE_D_1: "/3d-1-card/verify-code",
}


def normalize_card_type(value: str) -> CardType:
    key = re.sub(r"[\s_\-卡]+", "", value).lower()
    aliases = {
        "nexacardb": CardType.NEXACARD_B,
        "b": CardType.NEXACARD_B,
        "3d1": CardType.THREE_D_1,
        "3done": CardType.THREE_D_1,
    }
    try:
        return aliases[key]
    except KeyError as exc:
        raise InvalidLookupInput("unsupported card_type") from exc


def normalize_card_number(value: str) -> str:
    normalized = re.sub(r"[\s-]", "", value)
    if not re.fullmatch(r"\d{12,19}", normalized):
        raise InvalidLookupInput("card_number must contain 12 to 19 digits")
    return normalized


def parse_order_time(value: str, timezone: ZoneInfo) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidLookupInput("order_created_at must be ISO 8601") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def parse_lookup_input(card_number: str, card_type: str, order_created_at: str, timezone: ZoneInfo) -> LookupInput:
    return LookupInput(
        card_number=normalize_card_number(card_number),
        card_type=normalize_card_type(card_type),
        order_created_at=parse_order_time(order_created_at, timezone),
    )


def route_for(card_type: CardType) -> str:
    return ROUTES[card_type]


def select_nearest_otp(rows: list[OtpRow], lookup: LookupInput) -> OtpRow | None:
    candidates = [
        row
        for row in rows
        if row.card_number == lookup.card_number and row.created_at > lookup.order_created_at
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda row: (row.created_at - lookup.order_created_at, -row.record_id))
```

- [ ] **Step 4: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_matching -v`

Expected: all matching tests pass.

- [ ] **Step 5: Commit**

```powershell
git add nexacard_otp/matching.py tests/test_nexacard_matching.py
git commit -m "feat: match nearest NexaCard OTP"
```

---

### Task 3: Gmail Token Lifecycle and Login-Code Reader

**Files:**
- Create: `nexacard_otp/gmail_auth.py`
- Create: `nexacard_otp/gmail_reader.py`
- Create: `tests/fixtures/nexacard_verification_code.eml`
- Test: `tests/test_nexacard_gmail.py`

**Interfaces:**
- Consumes: private paths, `AuthStatus`, `GmailAuthorizationRequired`, and `GmailTemporarilyUnavailable`.
- Produces: `atomic_write_text`, `load_valid_credentials()`, `get_auth_status(expected_email)`, `parse_login_code(raw_message, internal_date_ms, sent_after)`, and `GmailCodeReader.wait_for_login_code(sent_after, interval_seconds, max_attempts)`.

- [ ] **Step 1: Add a sanitized MIME fixture and failing parser/token-state tests**

```text
From: NexaCardVCC <jushihui@mail.jushipay.com>
To: tester@example.com
Subject: NexaCard Verification Code
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"

<html><body><div>Enter this code within three minutes.</div><div>123456789</div></body></html>
```

```python
import asyncio
import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from google.auth.exceptions import RefreshError

from nexacard_otp.errors import GmailAuthorizationRequired, GmailTemporarilyUnavailable
from nexacard_otp.gmail_auth import get_auth_status, load_valid_credentials
from nexacard_otp.gmail_reader import GmailCodeReader, parse_login_code


class NexaCardGmailTests(unittest.TestCase):
    def test_sample_message_yields_nine_digit_code(self):
        raw = Path("tests/fixtures/nexacard_verification_code.eml").read_bytes()
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        sent_after = datetime(2026, 7, 19, 4, 54, tzinfo=timezone.utc)
        received_ms = int(datetime(2026, 7, 19, 4, 54, 10, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(parse_login_code(encoded, received_ms, sent_after), "123456789")

    def test_message_before_send_time_is_ignored(self):
        raw = Path("tests/fixtures/nexacard_verification_code.eml").read_bytes()
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        sent_after = datetime(2026, 7, 19, 4, 55, tzinfo=timezone.utc)
        received_ms = int(datetime(2026, 7, 19, 4, 54, 10, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertIsNone(parse_login_code(encoded, received_ms, sent_after))

    def test_wrong_sender_or_subject_is_ignored(self):
        raw = b"From: attacker@example.com\nSubject: NexaCard Verification Code\n\n123456789"
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        self.assertIsNone(parse_login_code(encoded, 2000, datetime.fromtimestamp(1, tz=timezone.utc)))

    def test_expired_access_token_refreshes_and_is_rewritten(self):
        credentials = Mock(valid=False, expired=True, refresh_token="refresh", to_json=Mock(return_value='{"token":"new"}'))
        with tempfile.TemporaryDirectory() as directory, patch(
            "nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", Path(directory) / "token.json"
        ), patch("nexacard_otp.gmail_auth.Credentials.from_authorized_user_file", return_value=credentials):
            (Path(directory) / "token.json").write_text("{}", encoding="utf-8")
            result = load_valid_credentials()
            self.assertIs(result, credentials)
            credentials.refresh.assert_called_once()
            self.assertEqual(json.loads((Path(directory) / "token.json").read_text(encoding="utf-8"))["token"], "new")

    def test_invalid_grant_requires_reauthorization_but_network_error_does_not(self):
        credentials = Mock(valid=False, expired=True, refresh_token="refresh")
        credentials.refresh.side_effect = RefreshError("invalid_grant: Token has been expired or revoked")
        with tempfile.TemporaryDirectory() as directory, patch(
            "nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", Path(directory) / "token.json"
        ), patch("nexacard_otp.gmail_auth.Credentials.from_authorized_user_file", return_value=credentials):
            (Path(directory) / "token.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(GmailAuthorizationRequired):
                load_valid_credentials()
        credentials.refresh.side_effect = OSError("temporary network failure")
        with tempfile.TemporaryDirectory() as directory, patch(
            "nexacard_otp.gmail_auth.PRIVATE_TOKEN_PATH", Path(directory) / "token.json"
        ), patch("nexacard_otp.gmail_auth.Credentials.from_authorized_user_file", return_value=credentials):
            (Path(directory) / "token.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(GmailTemporarilyUnavailable):
                load_valid_credentials()

    def test_temporary_gmail_failure_is_retried_inside_bounded_mail_poll(self):
        reader = GmailCodeReader()
        reader._fetch_once = Mock(side_effect=[GmailTemporarilyUnavailable("temporary"), "123456789"])
        sent_after = datetime(2026, 7, 19, 4, 54, tzinfo=timezone.utc)
        with patch("nexacard_otp.gmail_reader.asyncio.sleep", new=AsyncMock()):
            code = asyncio.run(reader.wait_for_login_code(sent_after, interval_seconds=0.01, max_attempts=2))
        self.assertEqual(code, "123456789")
```

- [ ] **Step 2: Run tests and verify imports fail**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_gmail -v`

Expected: FAIL because `gmail_auth` and `gmail_reader` do not exist.

- [ ] **Step 3: Implement atomic token refresh and status classification**

```python
# nexacard_otp/gmail_auth.py
import json
import os
from datetime import datetime
from pathlib import Path

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .errors import GmailAuthorizationRequired, GmailTemporarilyUnavailable
from .models import AuthStatus
from .settings import PRIVATE_TOKEN_META_PATH, PRIVATE_TOKEN_PATH, ensure_private_oauth_files

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def load_valid_credentials() -> Credentials:
    ensure_private_oauth_files()
    if not PRIVATE_TOKEN_PATH.is_file():
        raise GmailAuthorizationRequired("Google authorization has not been completed")
    try:
        credentials = Credentials.from_authorized_user_file(str(PRIVATE_TOKEN_PATH), SCOPES)
    except (ValueError, OSError) as exc:
        raise GmailAuthorizationRequired("stored Google credentials are invalid") from exc
    if credentials.valid:
        return credentials
    if not credentials.expired or not credentials.refresh_token:
        raise GmailAuthorizationRequired("Google refresh token is missing")
    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        if "invalid_grant" in str(exc).lower():
            raise GmailAuthorizationRequired("Google authorization has expired or was revoked") from exc
        raise GmailTemporarilyUnavailable("Google token refresh failed temporarily") from exc
    except (OSError, TransportError) as exc:
        raise GmailTemporarilyUnavailable("Google token refresh is temporarily unavailable") from exc
    atomic_write_text(PRIVATE_TOKEN_PATH, credentials.to_json())
    return credentials


def _profile_email(credentials: Credentials) -> str:
    def request_profile() -> str:
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return service.users().getProfile(userId="me").execute()["emailAddress"].lower()

    try:
        return request_profile()
    except HttpError as exc:
        if getattr(exc.resp, "status", None) != 401:
            raise GmailTemporarilyUnavailable("Gmail profile is temporarily unavailable") from exc
    try:
        credentials.refresh(Request())
        atomic_write_text(PRIVATE_TOKEN_PATH, credentials.to_json())
        return request_profile()
    except RefreshError as exc:
        if "invalid_grant" in str(exc).lower():
            raise GmailAuthorizationRequired("Google authorization has expired or was revoked") from exc
        raise GmailTemporarilyUnavailable("Google credential validation failed temporarily") from exc
    except (HttpError, OSError, TransportError) as exc:
        raise GmailTemporarilyUnavailable("Gmail profile is temporarily unavailable") from exc


def get_auth_status(expected_email: str = "") -> AuthStatus:
    try:
        credentials = load_valid_credentials()
        live_email = _profile_email(credentials)
    except GmailAuthorizationRequired as exc:
        return AuthStatus("reauthorize", str(exc))
    except GmailTemporarilyUnavailable as exc:
        return AuthStatus("unknown", str(exc))
    metadata = {}
    if PRIVATE_TOKEN_META_PATH.is_file():
        metadata = json.loads(PRIVATE_TOKEN_META_PATH.read_text(encoding="utf-8"))
    email = live_email
    if metadata.get("authorized_email") != email:
        metadata["authorized_email"] = email
        atomic_write_text(PRIVATE_TOKEN_META_PATH, json.dumps(metadata, ensure_ascii=False, indent=2))
    expiry_raw = metadata.get("estimated_expires_at")
    expiry = datetime.fromisoformat(expiry_raw) if expiry_raw else None
    if expected_email and email and email != expected_email.lower():
        return AuthStatus("mismatch", "authorized Gmail address does not match", email, expiry, bool(metadata.get("estimated")))
    return AuthStatus("valid", "Gmail authorization is available; access token refresh is automatic", email, expiry, bool(metadata.get("estimated")))
```

- [ ] **Step 4: Implement strict MIME parsing and bounded Gmail polling**

```python
# nexacard_otp/gmail_reader.py
import asyncio
import base64
import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .errors import GmailAuthorizationRequired, GmailTemporarilyUnavailable
from .gmail_auth import load_valid_credentials

EXPECTED_SENDER = "jushihui@mail.jushipay.com"
EXPECTED_SUBJECT = "NexaCard Verification Code"
CODE_PATTERN = re.compile(r"(?<!\d)(\d{9})(?!\d)")


def _decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def parse_login_code(raw_message: str, internal_date_ms: int, sent_after: datetime) -> str | None:
    received = datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc)
    if received <= sent_after.astimezone(timezone.utc):
        return None
    message = BytesParser(policy=policy.default).parsebytes(_decode(raw_message))
    sender = str(message.get("From", "")).lower()
    subject = str(message.get("Subject", "")).strip()
    if EXPECTED_SENDER not in sender or subject != EXPECTED_SUBJECT:
        return None
    bodies: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() in {"text/plain", "text/html"}:
            bodies.append(part.get_content())
    match = CODE_PATTERN.search("\n".join(bodies))
    return match.group(1) if match else None


class GmailCodeReader:
    def _fetch_once(self, sent_after: datetime) -> str | None:
        try:
            service = build("gmail", "v1", credentials=load_valid_credentials(), cache_discovery=False)
            query = f'from:({EXPECTED_SENDER}) subject:"{EXPECTED_SUBJECT}" newer_than:1d'
            items = service.users().messages().list(userId="me", q=query, maxResults=10).execute().get("messages", [])
            for item in items:
                data = service.users().messages().get(userId="me", id=item["id"], format="raw").execute()
                code = parse_login_code(data["raw"], int(data["internalDate"]), sent_after)
                if code:
                    return code
            return None
        except HttpError as exc:
            if getattr(exc.resp, "status", None) in {401, 403}:
                raise GmailAuthorizationRequired("Gmail authorization is no longer accepted") from exc
            raise GmailTemporarilyUnavailable("Gmail API is temporarily unavailable") from exc

    async def wait_for_login_code(self, sent_after: datetime, interval_seconds: float = 3.0, max_attempts: int = 60) -> str:
        for attempt in range(max_attempts):
            try:
                code = await asyncio.to_thread(self._fetch_once, sent_after)
            except GmailTemporarilyUnavailable:
                code = None
            if code:
                return code
            if attempt + 1 < max_attempts:
                await asyncio.sleep(interval_seconds)
        raise TimeoutError("NexaCard login verification email did not arrive")
```

- [ ] **Step 5: Run Gmail tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_gmail -v`

Expected: all Gmail tests pass without opening a browser or contacting Google.

- [ ] **Step 6: Commit**

```powershell
git add nexacard_otp/gmail_auth.py nexacard_otp/gmail_reader.py tests/test_nexacard_gmail.py tests/fixtures/nexacard_verification_code.eml
git commit -m "feat: add Gmail authorization lifecycle"
```

---

### Task 4: Google OAuth Coordinator and WebUI Authorization Endpoints

**Files:**
- Modify: `nexacard_otp/gmail_auth.py`
- Modify: `webui/server.py`
- Test: `tests/test_nexacard_oauth.py`

**Interfaces:**
- Consumes: Task 3 token/status functions and existing WebUI FastAPI app.
- Produces: `OAuthCoordinator.start(email, redirect_uri) -> str`, `OAuthCoordinator.complete(state, authorization_response) -> AuthStatus`, `/api/nexacard/oauth/start`, `/api/nexacard/oauth/callback`, and `/api/nexacard/oauth/status`.

- [ ] **Step 1: Write failing OAuth coordinator and route tests**

```python
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from nexacard_otp.gmail_auth import OAuthCoordinator
from webui import server


class NexaCardOAuthTests(unittest.TestCase):
    def test_start_uses_offline_consent_login_hint_and_random_state(self):
        flow = Mock()
        flow.authorization_url.return_value = ("https://accounts.google.com/authorize", "returned-state")
        with patch("nexacard_otp.gmail_auth.Flow.from_client_secrets_file", return_value=flow):
            coordinator = OAuthCoordinator()
            url = coordinator.start("owner@example.com", "http://127.0.0.1:8799/api/nexacard/oauth/callback")
        self.assertEqual(url, "https://accounts.google.com/authorize")
        kwargs = flow.authorization_url.call_args.kwargs
        self.assertEqual(kwargs["access_type"], "offline")
        self.assertEqual(kwargs["prompt"], "consent")
        self.assertEqual(kwargs["login_hint"], "owner@example.com")
        self.assertIn("returned-state", coordinator.pending)

    def test_callback_rejects_authorized_email_mismatch_before_token_write(self):
        coordinator = OAuthCoordinator()
        flow = Mock(credentials=Mock(to_json=Mock(return_value='{"refresh_token":"secret"}')))
        coordinator.pending["state1"] = ("expected@example.com", flow)
        profile = Mock()
        profile.users().getProfile().execute.return_value = {"emailAddress": "other@example.com"}
        with patch("nexacard_otp.gmail_auth.build", return_value=profile), patch(
            "nexacard_otp.gmail_auth.atomic_write_text"
        ) as write:
            with self.assertRaisesRegex(ValueError, "does not match"):
                coordinator.complete("state1", "http://127.0.0.1/callback?state=state1&code=abc")
        write.assert_not_called()

    def test_webui_start_route_returns_google_url(self):
        client = TestClient(server.app)
        with patch.object(server.NEXACARD_OAUTH, "start", return_value="https://accounts.google.com/authorize"):
            response = client.post("/api/nexacard/oauth/start", json={"email": "owner@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["authorization_url"], "https://accounts.google.com/authorize")
```

- [ ] **Step 2: Run tests and verify missing coordinator/routes**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_oauth -v`

Expected: FAIL because `OAuthCoordinator` and WebUI routes are missing.

- [ ] **Step 3: Implement stateful desktop OAuth completion and metadata**

Add to `nexacard_otp/gmail_auth.py`:

```python
import secrets
from datetime import timedelta, timezone

from google_auth_oauthlib.flow import Flow

from .settings import PRIVATE_CREDENTIALS_PATH


class OAuthCoordinator:
    def __init__(self) -> None:
        self.pending: dict[str, tuple[str, Flow]] = {}

    def start(self, email: str, redirect_uri: str) -> str:
        ensure_private_oauth_files()
        normalized = email.strip().lower()
        if "@" not in normalized:
            raise ValueError("a valid verification email is required")
        flow = Flow.from_client_secrets_file(str(PRIVATE_CREDENTIALS_PATH), scopes=SCOPES)
        flow.redirect_uri = redirect_uri
        requested_state = secrets.token_urlsafe(32)
        authorization_url, returned_state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            login_hint=normalized,
            include_granted_scopes="true",
            state=requested_state,
        )
        self.pending[returned_state] = (normalized, flow)
        return authorization_url

    def complete(self, state: str, authorization_response: str) -> AuthStatus:
        pending = self.pending.pop(state, None)
        if pending is None:
            raise ValueError("OAuth state is missing or expired")
        expected_email, flow = pending
        flow.fetch_token(authorization_response=authorization_response)
        profile = build("gmail", "v1", credentials=flow.credentials, cache_discovery=False)
        authorized_email = profile.users().getProfile(userId="me").execute()["emailAddress"].lower()
        if authorized_email != expected_email:
            raise ValueError("authorized Gmail address does not match the configured verification email")
        now = datetime.now(timezone.utc)
        remaining = flow.oauth2session.token.get("refresh_token_expires_in")
        estimated = remaining is None
        expires_at = now + timedelta(seconds=int(remaining)) if remaining is not None else now + timedelta(days=7)
        atomic_write_text(PRIVATE_TOKEN_PATH, flow.credentials.to_json())
        atomic_write_text(
            PRIVATE_TOKEN_META_PATH,
            json.dumps(
                {
                    "authorized_email": authorized_email,
                    "authorized_at": now.isoformat(),
                    "estimated_expires_at": expires_at.isoformat(),
                    "estimated": estimated,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        return AuthStatus("valid", "Google authorization completed", authorized_email, expires_at, estimated)
```

- [ ] **Step 4: Add WebUI OAuth routes without exposing tokens**

Add imports and routes to `webui/server.py`:

```python
from fastapi.responses import HTMLResponse

from nexacard_otp.gmail_auth import OAuthCoordinator, get_auth_status

NEXACARD_OAUTH = OAuthCoordinator()


@app.post("/api/nexacard/oauth/start")
async def nexacard_oauth_start(request: Request):
    data = await request.json()
    email = str(data.get("email") or "").strip()
    redirect_uri = str(request.base_url).rstrip("/") + "/api/nexacard/oauth/callback"
    try:
        url = NEXACARD_OAUTH.start(email, redirect_uri)
    except (ValueError, FileNotFoundError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "authorization_url": url}


@app.get("/api/nexacard/oauth/callback", response_class=HTMLResponse)
async def nexacard_oauth_callback(request: Request):
    state = request.query_params.get("state", "")
    try:
        status = await asyncio.to_thread(NEXACARD_OAUTH.complete, state, str(request.url))
    except Exception as exc:
        return HTMLResponse(f"<h2>Google 鉴权失败</h2><p>{html.escape(str(exc))}</p>", status_code=400)
    return HTMLResponse(f"<h2>Google 鉴权成功</h2><p>{html.escape(status.authorized_email or '')}</p>")


@app.get("/api/nexacard/oauth/status")
async def nexacard_oauth_status(email: str = ""):
    status = await asyncio.to_thread(get_auth_status, email.strip().lower())
    return {
        "state": status.state,
        "message": status.message,
        "authorized_email": status.authorized_email,
        "estimated_expires_at": status.estimated_expires_at.isoformat() if status.estimated_expires_at else None,
        "estimated": status.estimated,
    }
```

Also add `import asyncio` and `import html` at the existing import block.

- [ ] **Step 5: Run OAuth route tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_oauth -v`

Expected: all OAuth coordinator and route tests pass.

- [ ] **Step 6: Commit**

```powershell
git add nexacard_otp/gmail_auth.py webui/server.py tests/test_nexacard_oauth.py
git commit -m "feat: add NexaCard Gmail authorization endpoints"
```

---

### Task 5: Direct Native-Chrome Manager

**Files:**
- Create: `nexacard_otp/browser.py`
- Test: `tests/test_nexacard_browser.py`

**Interfaces:**
- Consumes: `Settings`, `CHROME_PROFILE_DIR`.
- Produces: `direct_browser_env(source) -> dict[str, str]`, `chrome_args() -> list[str]`, and `NativeChromeManager.page(settings)` async context manager.

- [ ] **Step 1: Write failing direct-network and lifecycle tests**

```python
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from nexacard_otp.browser import NativeChromeManager, chrome_args, direct_browser_env


class NexaCardBrowserTests(unittest.IsolatedAsyncioTestCase):
    def test_direct_env_removes_every_proxy_spelling(self):
        source = {
            "HTTP_PROXY": "http://proxy",
            "HTTPS_PROXY": "http://proxy",
            "ALL_PROXY": "socks://proxy",
            "http_proxy": "http://proxy",
            "https_proxy": "http://proxy",
            "all_proxy": "socks://proxy",
            "KEEP_ME": "yes",
        }
        result = direct_browser_env(source)
        self.assertEqual(result["KEEP_ME"], "yes")
        for key in source:
            if "proxy" in key.lower():
                self.assertNotIn(key, result)

    def test_chrome_args_force_direct_network(self):
        self.assertEqual(
            chrome_args(),
            ["--no-proxy-server", "--proxy-server=direct://", "--proxy-bypass-list=*"],
        )

    async def test_same_browser_fingerprint_reuses_context_and_each_request_gets_a_page(self):
        settings = Mock(browser_fingerprint=("chrome", True, "account", "mail"), chrome_path=Path("chrome.exe"), headless=True)
        context = AsyncMock()
        context.new_page.side_effect = [AsyncMock(), AsyncMock()]
        playwright = AsyncMock()
        playwright.chromium.launch_persistent_context.return_value = context
        manager = NativeChromeManager(playwright_factory=AsyncMock(return_value=playwright))
        async with manager.page(settings):
            pass
        async with manager.page(settings):
            pass
        self.assertEqual(playwright.chromium.launch_persistent_context.await_count, 1)
        self.assertEqual(context.new_page.await_count, 2)
```

- [ ] **Step 2: Run tests and verify missing browser module**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_browser -v`

Expected: FAIL because `nexacard_otp.browser` is missing.

- [ ] **Step 3: Implement direct environment and persistent-context reuse**

```python
# nexacard_otp/browser.py
import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from playwright.async_api import BrowserContext, Page, async_playwright

from .settings import CHROME_PROFILE_DIR, Settings

PROXY_KEYS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}


def direct_browser_env(source: dict[str, str] | None = None) -> dict[str, str]:
    return {key: value for key, value in (source or dict(os.environ)).items() if key not in PROXY_KEYS}


def chrome_args() -> list[str]:
    return ["--no-proxy-server", "--proxy-server=direct://", "--proxy-bypass-list=*"]


async def _default_playwright_factory():
    return await async_playwright().start()


class NativeChromeManager:
    def __init__(self, playwright_factory: Callable = _default_playwright_factory) -> None:
        self._playwright_factory = playwright_factory
        self._playwright = None
        self._context: BrowserContext | None = None
        self._fingerprint = None
        self._lock = asyncio.Lock()
        self.login_lock = asyncio.Lock()

    async def _context_for(self, settings: Settings) -> BrowserContext:
        async with self._lock:
            if self._context is not None and self._fingerprint == settings.browser_fingerprint:
                return self._context
            if self._context is not None:
                await self._context.close()
            if self._playwright is None:
                self._playwright = await self._playwright_factory()
            CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE_DIR),
                executable_path=str(settings.chrome_path),
                headless=settings.headless,
                args=chrome_args(),
                env=direct_browser_env(),
            )
            self._fingerprint = settings.browser_fingerprint
            return self._context

    @asynccontextmanager
    async def page(self, settings: Settings) -> AsyncIterator[Page]:
        context = await self._context_for(settings)
        page = await context.new_page()
        try:
            yield page
        finally:
            await page.close()

    async def close(self) -> None:
        async with self._lock:
            if self._context is not None:
                await self._context.close()
                self._context = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
            self._fingerprint = None
```

- [ ] **Step 4: Run browser tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_browser -v`

Expected: all direct-network and lifecycle tests pass.

- [ ] **Step 5: Commit**

```powershell
git add nexacard_otp/browser.py tests/test_nexacard_browser.py
git commit -m "feat: launch direct native Chrome for NexaCard"
```

---

### Task 6: On-Demand NexaCard Login Recovery

**Files:**
- Create: `nexacard_otp/login.py`
- Test: `tests/test_nexacard_login.py`

**Interfaces:**
- Consumes: Playwright `Page`, `Settings`, `GmailCodeReader`, browser `login_lock`, and `NexaCardLoginFailed`.
- Produces: `NexaCardLogin.ensure_authenticated(page, settings) -> bool`; return value is `True` only when recovery occurred.

- [ ] **Step 1: Write failing logout and serialized recovery tests**

```python
import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from nexacard_otp.login import NexaCardLogin


class NexaCardLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_page_does_not_touch_login_or_gmail(self):
        page = AsyncMock(url="https://www.nexacardvcc.com/nova-v-card-b/verify-code")
        page.locator.return_value.count.return_value = 0
        reader = AsyncMock()
        login = NexaCardLogin(asyncio.Lock(), reader)
        recovered = await login.ensure_authenticated(page, Mock())
        self.assertFalse(recovered)
        reader.wait_for_login_code.assert_not_called()

    async def test_logged_out_page_requests_fresh_email_code_and_submits(self):
        page = AsyncMock(url="https://www.nexacardvcc.com/login")
        locator = AsyncMock()
        page.locator.return_value = locator
        locator.count.return_value = 1
        locator.nth.return_value = locator
        reader = AsyncMock()
        reader.wait_for_login_code.return_value = "123456789"
        settings = Mock(account="account1", password="password1", verification_email="owner@example.com")
        login = NexaCardLogin(asyncio.Lock(), reader)
        with patch("nexacard_otp.login.datetime") as clock:
            clock.now.return_value = datetime(2026, 7, 19, 5, 0, tzinfo=timezone.utc)
            page.wait_for_url.side_effect = None
            recovered = await login.ensure_authenticated(page, settings)
        self.assertTrue(recovered)
        reader.wait_for_login_code.assert_awaited_once()

    async def test_concurrent_logout_checks_execute_one_recovery(self):
        lock = asyncio.Lock()
        reader = AsyncMock(wait_for_login_code=AsyncMock(return_value="123456789"))
        login = NexaCardLogin(lock, reader)
        state = {"logged_out": True}

        async def is_logged_out(page):
            return state["logged_out"]

        async def perform_login(page, settings):
            state["logged_out"] = False

        login._is_logged_out = AsyncMock(side_effect=is_logged_out)
        login._perform_login = AsyncMock(side_effect=perform_login)
        page1 = AsyncMock()
        page2 = AsyncMock()
        settings = Mock()
        await asyncio.gather(
            login.ensure_authenticated(page1, settings),
            login.ensure_authenticated(page2, settings),
        )
        self.assertEqual(login._perform_login.await_count, 1)
```

- [ ] **Step 2: Run tests and verify missing login module**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_login -v`

Expected: FAIL because `nexacard_otp.login` is missing.

- [ ] **Step 3: Implement logout detection and one-lock email login**

```python
# nexacard_otp/login.py
import asyncio
from datetime import datetime, timezone

from playwright.async_api import Page

from .errors import NexaCardLoginFailed
from .gmail_reader import GmailCodeReader
from .settings import Settings

BASE_URL = "https://www.nexacardvcc.com"


class NexaCardLogin:
    def __init__(self, login_lock: asyncio.Lock, gmail_reader: GmailCodeReader) -> None:
        self._login_lock = login_lock
        self._gmail_reader = gmail_reader

    async def _is_logged_out(self, page: Page) -> bool:
        if "/login" in page.url:
            return True
        return await page.locator('input[placeholder="请输入用户名"]').count() > 0

    async def _perform_login(self, page: Page, settings: Settings) -> None:
        if not settings.account or not settings.password or not settings.verification_email:
            raise NexaCardLoginFailed("NexaCard account, password, and verification email are required")
        await page.goto(f"{BASE_URL}/login", wait_until="networkidle")
        await page.locator('input[placeholder="请输入用户名"]').fill(settings.account)
        await page.locator('input[placeholder="请输入密码"]').fill(settings.password)
        await page.locator(".el-radio").nth(1).click()
        await page.locator('input[placeholder="请输入邮箱"]').fill(settings.verification_email)
        sent_after = datetime.now(timezone.utc)
        await page.locator("button.get-code-btn").click()
        try:
            code = await self._gmail_reader.wait_for_login_code(sent_after)
        except TimeoutError as exc:
            raise NexaCardLoginFailed("NexaCard login verification email timed out") from exc
        await page.locator('input[placeholder="请输入邮箱验证码"]').fill(code)
        await page.locator("button.submit-btn").click()
        try:
            await page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
        except Exception as exc:
            raise NexaCardLoginFailed("NexaCard login did not reach an authenticated page") from exc

    async def ensure_authenticated(self, page: Page, settings: Settings) -> bool:
        if not await self._is_logged_out(page):
            return False
        async with self._login_lock:
            await page.goto(f"{BASE_URL}/index", wait_until="networkidle")
            if not await self._is_logged_out(page):
                return False
            await self._perform_login(page, settings)
            return True
```

- [ ] **Step 4: Run login recovery tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_login -v`

Expected: all login tests pass and no live NexaCard request occurs.

- [ ] **Step 5: Commit**

```powershell
git add nexacard_otp/login.py tests/test_nexacard_login.py
git commit -m "feat: recover expired NexaCard sessions"
```

---

### Task 7: Verification-Page Reader and Bounded OTP Polling

**Files:**
- Create: `nexacard_otp/lookup.py`
- Test: `tests/test_nexacard_lookup.py`

**Interfaces:**
- Consumes: `NativeChromeManager`, `NexaCardLogin`, Task 2 matching functions, `Settings`, and lookup errors.
- Produces: `VerificationPage.search_rows(page, lookup, settings) -> list[OtpRow]` and `OtpLookupService.lookup(lookup, settings) -> str`.

- [ ] **Step 1: Write failing polling, logout-resume, and pagination tests**

```python
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from nexacard_otp.errors import NexaCardTransientError, OtpLookupTimedOut
from nexacard_otp.lookup import OtpLookupService
from nexacard_otp.models import CardType, LookupInput, OtpRow


class NexaCardLookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        zone = ZoneInfo("Asia/Shanghai")
        self.lookup = LookupInput(
            "6500000000000037",
            CardType.NEXACARD_B,
            datetime(2026, 7, 19, 3, 0, tzinfo=zone),
        )
        self.settings = Mock(max_attempts=3, poll_interval_seconds=0.01, page_timezone=zone)

    async def test_first_attempt_returns_without_sleep(self):
        row = OtpRow(1, "123456", self.lookup.card_number, datetime(2026, 7, 19, 3, 0, 1, tzinfo=self.settings.page_timezone))
        reader = AsyncMock(search_rows=AsyncMock(return_value=[row]))
        manager = Mock()
        page_context = AsyncMock()
        page_context.__aenter__.return_value = AsyncMock()
        manager.page.return_value = page_context
        service = OtpLookupService(manager, AsyncMock(), reader)
        with patch("nexacard_otp.lookup.asyncio.sleep") as sleep:
            otp = await service.lookup(self.lookup, self.settings)
        self.assertEqual(otp, "123456")
        sleep.assert_not_called()

    async def test_exactly_max_attempts_then_timeout(self):
        reader = AsyncMock(search_rows=AsyncMock(return_value=[]))
        manager = Mock()
        page_context = AsyncMock()
        page_context.__aenter__.return_value = AsyncMock()
        manager.page.return_value = page_context
        service = OtpLookupService(manager, AsyncMock(), reader)
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(OtpLookupTimedOut):
                await service.lookup(self.lookup, self.settings)
        self.assertEqual(reader.search_rows.await_count, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_login_recovery_repeats_current_attempt_without_consuming_it(self):
        reader = AsyncMock(search_rows=AsyncMock(side_effect=[PermissionError("logged out"), [] , [] , []]))
        login = AsyncMock()
        manager = Mock()
        page_context = AsyncMock()
        page_context.__aenter__.return_value = AsyncMock()
        manager.page.return_value = page_context
        service = OtpLookupService(manager, login, reader)
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(OtpLookupTimedOut):
                await service.lookup(self.lookup, self.settings)
        login.ensure_authenticated.assert_awaited_once()
        self.assertEqual(reader.search_rows.await_count, 4)

    async def test_transient_page_errors_retry_twice_without_consuming_an_otp_attempt(self):
        reader = AsyncMock(
            search_rows=AsyncMock(
                side_effect=[
                    NexaCardTransientError("network one"),
                    NexaCardTransientError("network two"),
                    [],
                    [],
                    [],
                ]
            )
        )
        manager = Mock()
        page_context = AsyncMock()
        page_context.__aenter__.return_value = AsyncMock()
        manager.page.return_value = page_context
        service = OtpLookupService(manager, AsyncMock(), reader)
        with patch("nexacard_otp.lookup.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(OtpLookupTimedOut):
                await service.lookup(self.lookup, self.settings)
        self.assertEqual(reader.search_rows.await_count, 5)
```

- [ ] **Step 2: Run tests and verify missing lookup module**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_lookup -v`

Expected: FAIL because `nexacard_otp.lookup` is missing.

- [ ] **Step 3: Implement DOM row parsing and complete pagination**

```python
# nexacard_otp/lookup.py
import asyncio
from datetime import datetime

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from .browser import NativeChromeManager
from .errors import NexaCardPageError, NexaCardTransientError, OtpLookupTimedOut
from .login import BASE_URL, NexaCardLogin
from .matching import route_for, select_nearest_otp
from .models import LookupInput, OtpRow
from .settings import Settings


class VerificationPage:
    async def _is_logged_out(self, page: Page) -> bool:
        return "/login" in page.url or await page.locator('input[placeholder="请输入用户名"]').count() > 0

    async def _click_and_wait_for_query(self, page: Page, locator) -> None:
        try:
            async with page.expect_response(
                lambda response: response.url.startswith("https://admin.jushipay.com/api/verify/code/")
            ) as response_info:
                await locator.click()
            response = await response_info.value
        except PlaywrightError as exc:
            raise NexaCardTransientError("NexaCard verification request failed temporarily") from exc
        if response.status in {401, 403}:
            raise PermissionError("NexaCard session is logged out")

    async def _current_rows(self, page: Page, settings: Settings) -> list[OtpRow]:
        output: list[OtpRow] = []
        for row in await page.locator("table tbody tr").all():
            cells = await row.locator("td").all_inner_texts()
            if len(cells) < 8:
                continue
            try:
                output.append(
                    OtpRow(
                        record_id=int(cells[0].strip()),
                        otp=cells[2].strip(),
                        card_number=cells[3].strip().replace(" ", "").replace("-", ""),
                        created_at=datetime.strptime(cells[6].strip(), "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=settings.page_timezone
                        ),
                    )
                )
            except (ValueError, IndexError) as exc:
                raise NexaCardPageError("NexaCard verification table has an unexpected row") from exc
        return output

    async def search_rows(self, page: Page, lookup: LookupInput, settings: Settings) -> list[OtpRow]:
        try:
            await page.goto(BASE_URL + route_for(lookup.card_type), wait_until="networkidle")
        except PlaywrightError as exc:
            raise NexaCardTransientError("NexaCard verification page is temporarily unavailable") from exc
        if await self._is_logged_out(page):
            raise PermissionError("NexaCard session is logged out")
        card_input = page.locator('input[placeholder="请输入卡号"]')
        await card_input.fill(lookup.card_number)
        await self._click_and_wait_for_query(page, page.locator("button.act-color"))
        rows: list[OtpRow] = []
        seen_pages = 0
        while True:
            rows.extend(await self._current_rows(page, settings))
            next_button = page.locator(".el-pagination .btn-next")
            if await next_button.count() == 0 or await next_button.is_disabled():
                break
            seen_pages += 1
            if seen_pages > 1000:
                raise NexaCardPageError("NexaCard pagination exceeded the safety bound")
            await self._click_and_wait_for_query(page, next_button)
            if await self._is_logged_out(page):
                raise PermissionError("NexaCard session expired during pagination")
        return rows
```

- [ ] **Step 4: Implement bounded polling with one login recovery**

Append to `nexacard_otp/lookup.py`:

```python
class OtpLookupService:
    def __init__(
        self,
        browser: NativeChromeManager,
        login: NexaCardLogin,
        verification_page: VerificationPage | None = None,
    ) -> None:
        self._browser = browser
        self._login = login
        self._verification_page = verification_page or VerificationPage()

    async def lookup(self, lookup: LookupInput, settings: Settings) -> str:
        recovered = False
        transient_failures = 0
        async with self._browser.page(settings) as page:
            attempt = 0
            while attempt < settings.max_attempts:
                try:
                    rows = await self._verification_page.search_rows(page, lookup, settings)
                except PermissionError:
                    if recovered:
                        raise NexaCardPageError("NexaCard session expired again after login recovery")
                    await self._login.ensure_authenticated(page, settings)
                    recovered = True
                    continue
                except NexaCardTransientError:
                    transient_failures += 1
                    if transient_failures > 2:
                        raise
                    await asyncio.sleep(min(settings.poll_interval_seconds, 1.0))
                    continue
                transient_failures = 0
                attempt += 1
                match = select_nearest_otp(rows, lookup)
                if match is not None:
                    return match.otp
                if attempt < settings.max_attempts:
                    await asyncio.sleep(settings.poll_interval_seconds)
            raise OtpLookupTimedOut("no matching OTP appeared before the configured attempt limit")
```

- [ ] **Step 5: Run lookup tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_lookup -v`

Expected: all polling and recovery tests pass; attempt count is exactly three in the boundary test.

- [ ] **Step 6: Commit**

```powershell
git add nexacard_otp/lookup.py tests/test_nexacard_lookup.py
git commit -m "feat: poll NexaCard verification pages"
```

---

### Task 8: Standalone OTP HTTP API

**Files:**
- Create: `nexacard_otp/app.py`
- Create: `nexacard_otp_service.py`
- Test: `tests/test_nexacard_api.py`

**Interfaces:**
- Consumes: `load_settings`, `parse_lookup_input`, `NativeChromeManager`, `GmailCodeReader`, `NexaCardLogin`, and `OtpLookupService`.
- Produces: FastAPI `app`, `POST /v1/otp`, `GET /health`, and executable service entry point.

- [ ] **Step 1: Write failing success and error-contract tests**

```python
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from nexacard_otp.errors import (
    GmailAuthorizationRequired,
    NexaCardLoginFailed,
    NexaCardTransientError,
    OtpLookupTimedOut,
)
from nexacard_otp.app import app


class NexaCardApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_success_response_contains_only_otp(self):
        app.state.lookup_service = AsyncMock(lookup=AsyncMock(return_value="123456"))
        with patch("nexacard_otp.app.load_settings", return_value=Mock(page_timezone=Mock())):
            with patch("nexacard_otp.app.parse_lookup_input", return_value=Mock()):
                response = self.client.post(
                    "/v1/otp",
                    json={
                        "card_number": "6500000000000037",
                        "card_type": "NexaCardB",
                        "order_created_at": "2026-07-19T05:30:20+08:00",
                    },
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"otp": "123456"})

    def test_domain_failures_map_to_confirmed_status_codes(self):
        cases = [
            (GmailAuthorizationRequired("reauthorize"), 503),
            (NexaCardTransientError("temporary"), 503),
            (NexaCardLoginFailed("login failed"), 502),
            (OtpLookupTimedOut("timed out"), 504),
        ]
        for failure, expected in cases:
            with self.subTest(failure=type(failure).__name__):
                app.state.lookup_service = AsyncMock(lookup=AsyncMock(side_effect=failure))
                with patch("nexacard_otp.app.load_settings", return_value=Mock(page_timezone=Mock())), patch(
                    "nexacard_otp.app.parse_lookup_input", return_value=Mock()
                ):
                    response = self.client.post(
                        "/v1/otp",
                        json={"card_number": "6500000000000037", "card_type": "NexaCardB", "order_created_at": "2026-07-19 05:30:20"},
                    )
                self.assertEqual(response.status_code, expected)
                self.assertNotIn("6500000000000037", response.text)

    def test_health_never_returns_credentials(self):
        response = self.client.get("/health")
        self.assertEqual(response.json(), {"ok": True})
```

- [ ] **Step 2: Run API tests and verify missing app module**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_api -v`

Expected: FAIL because `nexacard_otp.app` is missing.

- [ ] **Step 3: Implement app state, endpoint, and safe error mapping**

```python
# nexacard_otp/app.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .browser import NativeChromeManager
from .errors import (
    GmailAuthorizationRequired,
    GmailTemporarilyUnavailable,
    InvalidLookupInput,
    NexaCardLoginFailed,
    NexaCardPageError,
    NexaCardTransientError,
    OtpLookupTimedOut,
)
from .gmail_reader import GmailCodeReader
from .login import NexaCardLogin
from .lookup import OtpLookupService
from .matching import parse_lookup_input
from .settings import load_settings


class OtpRequest(BaseModel):
    card_number: str
    card_type: str
    order_created_at: str


app = FastAPI(title="NexaCard OTP Service")


@app.on_event("startup")
async def startup() -> None:
    browser = NativeChromeManager()
    login = NexaCardLogin(browser.login_lock, GmailCodeReader())
    app.state.browser = browser
    app.state.lookup_service = OtpLookupService(browser, login)


@app.on_event("shutdown")
async def shutdown() -> None:
    browser = getattr(app.state, "browser", None)
    if browser is not None:
        await browser.close()


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/v1/otp")
async def get_otp(request: OtpRequest):
    try:
        settings = load_settings()
        lookup = parse_lookup_input(
            request.card_number,
            request.card_type,
            request.order_created_at,
            settings.page_timezone,
        )
        otp = await app.state.lookup_service.lookup(lookup, settings)
        return {"otp": otp}
    except InvalidLookupInput as exc:
        raise HTTPException(400, {"code": exc.code, "message": str(exc)}) from exc
    except (NexaCardLoginFailed, NexaCardPageError) as exc:
        raise HTTPException(502, {"code": exc.code, "message": str(exc)}) from exc
    except (GmailAuthorizationRequired, GmailTemporarilyUnavailable, NexaCardTransientError) as exc:
        raise HTTPException(503, {"code": exc.code, "message": str(exc)}) from exc
    except OtpLookupTimedOut as exc:
        raise HTTPException(504, {"code": exc.code, "message": str(exc)}) from exc
```

- [ ] **Step 4: Add the Uvicorn entry point**

```python
# nexacard_otp_service.py
import uvicorn

from nexacard_otp.settings import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "nexacard_otp.app:app",
        host=settings.service_host,
        port=settings.service_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run API tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_api -v`

Expected: all endpoint and error-contract tests pass.

- [ ] **Step 6: Commit**

```powershell
git add nexacard_otp/app.py nexacard_otp_service.py tests/test_nexacard_api.py
git commit -m "feat: expose NexaCard OTP API"
```

---

### Task 9: WebUI Controls, Service Entry, Documentation, and Full Verification

**Files:**
- Modify: `webui/scripts.py`
- Modify: `webui/server.py`
- Modify: `webui/static/app.js`
- Modify: `webui/static/style.css`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Test: `tests/test_nexacard_webui.py`

**Interfaces:**
- Consumes: OAuth endpoints from Task 4, service entry from Task 8, and existing WebUI configuration renderer/run controls.
- Produces: WebUI `NexaCard OTP` group, authorization buttons/status, service connectivity test, and long-running service launcher.

- [ ] **Step 1: Write failing schema, service-entry, route, and static-asset tests**

```python
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webui import scripts, server


class NexaCardWebUITests(unittest.TestCase):
    def test_schema_exposes_secret_credentials_and_polling_defaults(self):
        group = next(group for group in scripts.ENV_SCHEMA if group["group"] == "NexaCard OTP")
        items = {item["key"]: item for item in group["items"]}
        self.assertTrue(items["NEXACARD_PASSWORD"]["secret"])
        self.assertEqual(items["NEXACARD_HEADLESS"]["default"], "true")
        self.assertEqual(items["NEXACARD_OTP_POLL_INTERVAL_SECONDS"]["default"], 3)
        self.assertEqual(items["NEXACARD_OTP_MAX_ATTEMPTS"]["default"], 100)
        self.assertTrue(items["NEXACARD_VERIFICATION_EMAIL"]["gmail_oauth"])

    def test_service_is_available_in_script_launcher(self):
        entry = next(item for item in scripts.SCRIPTS if item["id"] == "nexacard_otp_service")
        self.assertEqual(entry["file"], "nexacard_otp_service.py")

    def test_connectivity_check_uses_configured_local_service(self):
        client = TestClient(server.app)
        with patch.object(server, "_read_config_val", side_effect=lambda key, default="": "127.0.0.1" if key.endswith("HOST") else "8811"), patch.object(
            server, "_http_alive", return_value=True
        ):
            response = client.post("/api/test/nexacard", json={"env": {}})
        self.assertTrue(response.json()["ok"])

    def test_static_assets_contain_authorize_reauthorize_and_status_controls(self):
        script = open("webui/static/app.js", encoding="utf-8").read()
        style = open("webui/static/style.css", encoding="utf-8").read()
        self.assertIn("Google 鉴权", script)
        self.assertIn("重新鉴权", script)
        self.assertIn("检测状态", script)
        self.assertIn("oauth-actions", style)
```

- [ ] **Step 2: Run WebUI tests and verify schema/entry failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_webui -v`

Expected: FAIL because the NexaCard WebUI group and service entry are absent.

- [ ] **Step 3: Add the service entry and WebUI configuration schema**

Add to `SCRIPTS` in `webui/scripts.py`:

```python
{
    "id": "nexacard_otp_service",
    "file": "nexacard_otp_service.py",
    "category": "NexaCard OTP",
    "title": "NexaCard OTP 服务",
    "desc": "启动本机直连 Chrome 的 OTP 查询 HTTP 服务。",
    "args": [],
},
```

Add near the top of `ENV_SCHEMA`:

```python
{
    "group": "NexaCard OTP",
    "tests": [{"target": "nexacard", "label": "检测 OTP 服务"}],
    "items": [
        {"key": "NEXACARD_ACCOUNT", "required": True, "help": "NexaCard 登录账号"},
        {"key": "NEXACARD_PASSWORD", "required": True, "secret": True, "help": "NexaCard 登录密码"},
        {
            "key": "NEXACARD_VERIFICATION_EMAIL",
            "required": True,
            "gmail_oauth": True,
            "help": "NexaCard 登录验证邮箱；需完成右侧 Google 鉴权",
        },
        {"key": "NEXACARD_HEADLESS", "type": "choice", "choices": ["true", "false"], "default": "true", "help": "默认无头运行；调试时可设为 false"},
        {"key": "NEXACARD_CHROME_PATH", "help": "留空自动发现本机 Google Chrome"},
        {"key": "NEXACARD_PAGE_TIMEZONE", "default": "Asia/Shanghai", "help": "页面裸时间和无时区订单时间的解释时区"},
        {"key": "NEXACARD_OTP_POLL_INTERVAL_SECONDS", "type": "int", "default": 3, "help": "OTP 刷新间隔（秒），必须为正数"},
        {"key": "NEXACARD_OTP_MAX_ATTEMPTS", "type": "int", "default": 100, "help": "OTP 最大查询次数，必须为正整数"},
        {"key": "NEXACARD_SERVICE_HOST", "default": "127.0.0.1", "help": "OTP 服务监听地址"},
        {"key": "NEXACARD_SERVICE_PORT", "type": "int", "default": 8811, "help": "OTP 服务监听端口"},
    ],
},
```

- [ ] **Step 4: Add service connectivity testing**

Add a `nexacard` branch to `api_test` in `webui/server.py` before the unknown-target response:

```python
if target == "nexacard":
    host = _read_config_val("NEXACARD_SERVICE_HOST", "127.0.0.1")
    port = _read_config_val("NEXACARD_SERVICE_PORT", "8811")
    url = f"http://{host}:{port}/health"
    return {"ok": _http_alive(url), "msg": f"NexaCard OTP 服务 {url}"}
```

- [ ] **Step 5: Render OAuth actions next to the verification email**

In `loadEnv()` after `box.appendChild(row)` in `webui/static/app.js`, add:

```javascript
if(it.gmail_oauth){
  const actions = document.createElement('div');
  actions.className = 'oauth-actions';
  actions.innerHTML = `
    <button type="button" data-oauth-action="authorize">Google 鉴权</button>
    <button type="button" data-oauth-action="reauthorize">重新鉴权</button>
    <button type="button" data-oauth-action="status">检测状态</button>
    <span class="oauth-status">尚未检测</span>`;
  row.querySelector('.v').appendChild(actions);
  actions.querySelectorAll('button').forEach(button=>{
    button.onclick = async ()=>{
      const email = row.querySelector('input[data-env]').value.trim();
      const status = actions.querySelector('.oauth-status');
      if(button.dataset.oauthAction === 'status'){
        await loadNexaCardOAuthStatus(email, status);
        return;
      }
      const result = await (await fetch('/api/nexacard/oauth/start', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({email})
      })).json();
      if(!result.ok){ status.textContent = result.error || '鉴权启动失败'; return; }
      window.open(result.authorization_url, '_blank', 'noopener');
      status.textContent = '请在 Google 页面完成授权，然后点击检测状态';
    };
  });
  loadNexaCardOAuthStatus(value, actions.querySelector('.oauth-status'));
}
```

Add this function above `loadEnv()`:

```javascript
async function loadNexaCardOAuthStatus(email, target){
  if(!email){ target.textContent='请先填写验证邮箱'; return; }
  try{
    const result = await (await fetch('/api/nexacard/oauth/status?email='+encodeURIComponent(email))).json();
    let text = result.message || result.state;
    if(result.authorized_email) text += ' · '+result.authorized_email;
    if(result.estimated_expires_at) text += ` · ${result.estimated?'预计':''}到期 ${result.estimated_expires_at}`;
    target.textContent = text;
    target.classList.toggle('bad', result.state==='reauthorize' || result.state==='mismatch');
  }catch(error){
    target.textContent='暂时无法验证授权状态';
    target.classList.add('bad');
  }
}
```

- [ ] **Step 6: Style the controls without changing unrelated WebUI layout**

Append to `webui/static/style.css`:

```css
.oauth-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px}
.oauth-actions button{padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel2);color:var(--text);cursor:pointer}
.oauth-actions button:hover{border-color:var(--accent)}
.oauth-status{font-size:12px;color:var(--green);overflow-wrap:anywhere}
.oauth-status.bad{color:var(--red)}
```

- [ ] **Step 7: Document setup and API usage**

Add a `NexaCard OTP 服务` section to `README.md` containing these exact operator steps:

````markdown
### NexaCard OTP 服务

1. 在 WebUI 的 `NexaCard OTP` 配置组保存账号、密码、验证邮箱、时区和轮询参数。
2. 点击 `Google 鉴权`；授权成功后点击 `检测状态`。
3. 从脚本列表启动 `NexaCard OTP 服务`。服务默认监听 `127.0.0.1:8811`。
4. 调用：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8811/v1/otp -ContentType application/json -Body '{"card_number":"6500000000000037","card_type":"NexaCardB","order_created_at":"2026-07-19T05:30:20+08:00"}'
```

成功响应只含 `otp`。默认每 3 秒查询一次、最多 100 次。Chrome 强制直连，不使用项目代理或指纹浏览器。Google OAuth 文件和 Chrome 会话保存在 `nexacard_otp/private/`，该目录不会进入 Git。
````

Add a dated `NexaCard OTP service` entry to `CHANGELOG.md` covering native direct Chrome, Gmail reauthorization, WebUI `x/y`, and the local API.

- [ ] **Step 8: Run all focused NexaCard tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_nexacard_settings tests.test_nexacard_matching tests.test_nexacard_gmail tests.test_nexacard_oauth tests.test_nexacard_browser tests.test_nexacard_login tests.test_nexacard_lookup tests.test_nexacard_api tests.test_nexacard_webui -v
```

Expected: all focused tests pass.

- [ ] **Step 9: Run full regression and static checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall nexacard_otp nexacard_otp_service.py webui
git diff --check
```

Expected: the existing test suite passes, compileall reports no syntax errors, and `git diff --check` prints nothing.

- [ ] **Step 10: Perform controlled live acceptance**

Run the WebUI, save a visible-mode configuration, and complete these checks in order:

1. Confirm `检测状态` reports the configured Gmail address without displaying any token.
2. Start `nexacard_otp_service.py` and confirm `GET http://127.0.0.1:8811/health` returns `{"ok":true}`.
3. Use a known read-only NexaCardB record to verify strict timestamp selection without initiating a payment.
4. Repeat with the 3D-1 route.
5. Log out NexaCard manually, issue one OTP request, and verify the service performs one email login recovery and resumes the same request.
6. Set `NEXACARD_HEADLESS=true`, restart only the browser generation through the next request, and repeat the read-only lookup.
7. Inspect logs and confirm passwords, complete cards, Gmail codes, OTPs, and tokens are absent.

Expected: all seven checks pass; no payment, card mutation, or proxy request occurs.

- [ ] **Step 11: Commit**

```powershell
git add webui/scripts.py webui/server.py webui/static/app.js webui/static/style.css README.md CHANGELOG.md tests/test_nexacard_webui.py
git commit -m "feat: integrate NexaCard OTP service in WebUI"
```

---

## Final Verification Gate

Before claiming completion, run:

```powershell
git status --short
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall nexacard_otp nexacard_otp_service.py webui
git diff --check
```

Expected:

- no unexpected untracked private files appear because `nexacard_otp/private/` is ignored;
- all tests pass;
- all new Python modules compile;
- no whitespace errors are reported;
- the working tree contains only intentional documentation or implementation changes not yet committed by the chosen execution workflow.
