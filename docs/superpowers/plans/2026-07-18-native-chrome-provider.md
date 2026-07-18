# Native Chrome Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a configurable headed native Google Chrome provider that uses one credentialed IPMart SID lease per account, derives timezone from the lease's IPinfo response, prevents WebRTC direct-IP leakage, isolates browser state in a temporary profile, and deletes that profile after use.

**Architecture:** Extend the IPMart lease with validated exit metadata, then add a provider-neutral async `BrowserSession` layer. BitBrowser and AdsPower continue to create managed profiles and attach over CDP; Chrome launches a Playwright persistent context with `channel="chrome"`, a unique temporary `user-data-dir`, direct proxy credentials, regional settings, and a proxy-only WebRTC policy. Existing browser workflows migrate to the session layer without changing non-browser platform behavior.

**Tech Stack:** Python 3.10+, `playwright.async_api`, `requests`, standard-library `dataclasses`, `zoneinfo`, `tempfile`, `pathlib`, `ipaddress`, `unittest`, FastAPI WebUI.

## Global Constraints

- `FINGERPRINT_BROWSER=bitbrowser` remains the default; accepted values become `bitbrowser`, `adspower`, and `chrome`.
- Chrome means installed branded Google Chrome, headed mode only; do not silently fall back to Playwright Chromium.
- Every Chrome session uses a unique non-default user-data directory and removes it after success, failure, timeout, or cancellation.
- IPMart credentials are passed as structured Playwright proxy fields, never as a command-line URL and never in logs.
- A Chrome session with IPMart enabled fails closed when the lease, country, timezone, coordinates, browser exit IP, or WebRTC validation is missing or inconsistent.
- `IPMART_IP_CHECK_URL` defaults to `https://ipinfo.io/json`; `IPMART_EXPECTED_COUNTRY` defaults to `US`.
- Chrome uses its real UA, Client Hints, GPU, Canvas, AudioContext, fonts, CPU, memory, screen, and TLS behavior; do not randomize or overwrite them.
- Chrome locale defaults to `en-US`; `Accept-Language` defaults to `en-US,en;q=0.9`; timezone comes from the inspected IPMart exit.
- WebRTC stays enabled with `disable_non_proxied_udp`; unrestricted fallback is forbidden.
- Geolocation permission remains denied unless a future workflow explicitly requires it.
- BitBrowser and AdsPower payload behavior remains backward compatible.
- Never modify the user's normal Chrome profile, Windows global proxy, or Clash Verge configuration.
- Do not stage, modify, or commit unrelated `mail.txt` or `_outlook_pool/mail.txt` files.

## File Structure

- Create `common/chrome_privacy.py`: regional-profile construction, IPinfo parsing helpers used by Chrome, WebRTC candidate validation, and browser preflight.
- Create `common/browser_provider.py`: provider selection, `BrowserSession`, managed-profile backend, native Chrome backend, lifecycle and cleanup.
- Modify `common/ipmart_proxy.py`: rich exit metadata, country validation, acquisition, verification, and safe ledger fields.
- Modify `common/account_proxy.py`: transport rich exit metadata through child environments and generalize sanitized browser errors.
- Modify `common/browser.py`: compatibility helpers delegate to `BrowserSession` while preserving existing callers during migration.
- Modify browser workflows: `register.py`, `outlook_reg_loop.py`, `register_outlook_standalone.py`, `mailbox_broker.py`, `register_grok.py`, `unlock_outlook.py`, `validate_keys.py`, `register_chatgpt.py`, `register_github.py`, `oauth_codex.py`, and `common/oauth_codex.py`.
- Modify configuration surfaces: `config.py`, `.env.example`, `webui/scripts.py`, `webui/server.py`, `webui/static/app.js`, `webui/static/index.html`, `README.md`, and `CHANGELOG.md`.
- Create or modify focused tests under `tests/` for every boundary.

---

### Task 1: Enrich IPMart SID Leases With Exit Metadata

**Files:**
- Modify: `common/ipmart_proxy.py`
- Modify: `tests/test_ipmart_proxy.py`

**Interfaces:**
- Produces: `ProxyExitInfo(ip, country, region, city, latitude, longitude, timezone_id)`.
- Produces: `ProxyLease(proxy_type, host, port, username, password, sid, exit_ip, exit_info: ProxyExitInfo | None = None)` while preserving the existing positional fields through `exit_ip`.
- Produces: `read_proxy_exit(response) -> ProxyExitInfo`.
- Produces: `inspect_proxy(lease, *, env=None, session_factory=requests.Session) -> ProxyExitInfo`.
- Preserves: `verify_proxy(lease, expected_exit_ip=None, *, env=None, session_factory=requests.Session) -> str` for existing callers.

- [ ] **Step 1: Write failing exit-metadata and acquisition tests**

Add imports and tests to `tests/test_ipmart_proxy.py`:

```python
from unittest.mock import Mock


def rich_response(ip="203.0.113.8", country="US", timezone="America/Chicago"):
    response = Mock(status_code=200)
    response.json.return_value = {
        "ip": ip,
        "city": "Bloomfield",
        "region": "Iowa",
        "country": country,
        "loc": "40.7517,-92.4149",
        "timezone": timezone,
    }
    return response


class ProxyExitInfoTests(unittest.TestCase):
    def test_plain_proxy_url_omits_empty_authentication(self):
        lease = ipmart_proxy.ProxyLease(
            "http", "127.0.0.1", 7890, "", "", "", ""
        )
        self.assertEqual(
            ipmart_proxy.requests_proxy_url(lease),
            "http://127.0.0.1:7890",
        )

    def test_read_proxy_exit_parses_ipinfo_shape(self):
        info = ipmart_proxy.read_proxy_exit(rich_response())
        self.assertEqual(info.ip, "203.0.113.8")
        self.assertEqual(info.country, "US")
        self.assertEqual(info.region, "Iowa")
        self.assertEqual(info.city, "Bloomfield")
        self.assertEqual((info.latitude, info.longitude), (40.7517, -92.4149))
        self.assertEqual(info.timezone_id, "America/Chicago")

    def test_read_proxy_exit_rejects_invalid_timezone(self):
        with self.assertRaisesRegex(ipmart_proxy.IPMartProxyError, "timezone"):
            ipmart_proxy.read_proxy_exit(rich_response(timezone="Not/AZone"))

    def test_read_proxy_exit_rejects_invalid_coordinates(self):
        response = rich_response()
        response.json.return_value["loc"] = "200,300"
        with self.assertRaisesRegex(ipmart_proxy.IPMartProxyError, "coordinates"):
            ipmart_proxy.read_proxy_exit(response)

    def test_acquire_carries_exit_info_and_checks_expected_country(self):
        session = Mock()
        session.get.return_value = rich_response()
        lease = ipmart_proxy.acquire_proxy(
            env={
                **self.env,
                "IPMART_EXPECTED_COUNTRY": "US",
                "IPMART_IP_CHECK_URL": "https://ipinfo.io/json",
            },
            session_factory=Mock(return_value=session),
            sid_factory=lambda: "00000042",
            reserve=False,
            sleep=lambda _: None,
        )
        self.assertEqual(lease.exit_ip, "203.0.113.8")
        self.assertEqual(lease.exit_info.timezone_id, "America/Chicago")

    def test_acquire_rejects_wrong_country(self):
        session = Mock()
        session.get.return_value = rich_response(country="CA")
        with self.assertRaisesRegex(ipmart_proxy.IPMartProxyError, "country"):
            ipmart_proxy.acquire_proxy(
                env={**self.env, "IPMART_EXPECTED_COUNTRY": "US"},
                session_factory=Mock(return_value=session),
                sid_factory=lambda: "00000042",
                reserve=False,
                sleep=lambda _: None,
            )
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
python -m unittest tests.test_ipmart_proxy.ProxyExitInfoTests -v
```

Expected: failures because `ProxyExitInfo`, `read_proxy_exit`, and `ProxyLease.exit_info` do not exist.

- [ ] **Step 3: Implement rich response parsing and lease acquisition**

In `common/ipmart_proxy.py`, add `zoneinfo`, the metadata type, settings field, parser, and inspection function:

```python
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_IP_CHECK_URL = "https://ipinfo.io/json"


@dataclass(frozen=True)
class ProxyExitInfo:
    ip: str
    country: str
    region: str
    city: str
    latitude: float
    longitude: float
    timezone_id: str


@dataclass(frozen=True)
class IPMartSettings:
    enabled: bool
    proxy_host: str
    proxy_port: int
    username_template: str = field(repr=False)
    password: str = field(repr=False)
    max_attempts: int = 3
    ip_check_url: str = DEFAULT_IP_CHECK_URL
    expected_country: str = "US"


@dataclass(frozen=True)
class ProxyLease:
    proxy_type: str
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    sid: str = ""
    exit_ip: str = ""
    exit_info: ProxyExitInfo | None = None


def _country(value):
    country = (value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise IPMartProxyError("proxy IP check returned an invalid country")
    return country


def read_proxy_exit(response):
    if response.status_code != 200:
        raise IPMartProxyError(
            f"proxy IP check failed with HTTP {response.status_code}"
        )
    try:
        data = response.json()
        ip = str(ipaddress.ip_address(data.get("ip", "")))
        country = _country(data.get("country"))
        raw_lat, raw_lon = str(data.get("loc", "")).split(",", 1)
        latitude, longitude = float(raw_lat), float(raw_lon)
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("coordinate range")
        timezone_id = str(data.get("timezone", "")).strip()
        ZoneInfo(timezone_id)
    except ZoneInfoNotFoundError:
        raise IPMartProxyError("proxy IP check returned an invalid timezone") from None
    except (AttributeError, KeyError, TypeError, ValueError):
        raise IPMartProxyError("proxy IP check returned invalid regional coordinates") from None
    return ProxyExitInfo(
        ip=ip,
        country=country,
        region=str(data.get("region", "")).strip(),
        city=str(data.get("city", "")).strip(),
        latitude=latitude,
        longitude=longitude,
        timezone_id=timezone_id,
    )


def requests_proxy_url(lease):
    auth = ""
    if lease.username or lease.password:
        username = quote(lease.username, safe="")
        password = quote(lease.password, safe="")
        auth = f"{username}:{password}@"
    return f"{lease.proxy_type}://{auth}{lease.host}:{lease.port}"


def inspect_proxy(lease, *, env=None, session_factory=requests.Session):
    settings = settings_from_env(env)
    session = _credentialed_session(lease, session_factory)
    try:
        response = session.get(settings.ip_check_url, timeout=20)
        info = read_proxy_exit(response)
    except IPMartProxyError:
        raise
    except Exception:
        raise IPMartProxyError("proxy IP check request failed") from None
    if info.country != settings.expected_country:
        raise IPMartProxyError("proxy exit country does not match configured country")
    return info
```

Update `settings_from_env`, `verify_proxy`, and `acquire_proxy`:

```python
expected_country = _country(env.get("IPMART_EXPECTED_COUNTRY", "US"))

# In the IPMartSettings return value:
expected_country=expected_country,


def verify_proxy(lease, expected_exit_ip=None, *, env=None,
                 session_factory=requests.Session):
    info = inspect_proxy(lease, env=env, session_factory=session_factory)
    if expected_exit_ip and info.ip != expected_exit_ip:
        raise IPMartProxyError(
            f"proxy exit changed: expected {expected_exit_ip}, observed {info.ip}"
        )
    return info.ip


# In acquire_proxy, replace the verify call with:
info = inspect_proxy(candidate, env=env, session_factory=session_factory)
if info.ip in used:
    raise IPMartProxyError(f"duplicate proxy exit IP {info.ip}")
lease = replace(candidate, exit_ip=info.ip, exit_info=info)
```

Extend `_write_lease` only with safe fields:

```python
if lease.exit_info is not None:
    record.update({
        "country": lease.exit_info.country,
        "region": lease.exit_info.region,
        "city": lease.exit_info.city,
        "timezone": lease.exit_info.timezone_id,
    })
```

- [ ] **Step 4: Run focused and existing IPMart tests**

Run:

```powershell
python -m unittest tests.test_ipmart_proxy tests.test_account_proxy -v
```

Expected: all tests pass; update existing fake IP responses to include country, `loc`, and timezone where acquisition now requires them.

- [ ] **Step 5: Commit the exit metadata model**

```powershell
git add common/ipmart_proxy.py tests/test_ipmart_proxy.py
git commit -m "feat: inspect IPMart exit metadata"
```

---

### Task 2: Transport One Immutable Regional Lease Across Processes

**Files:**
- Modify: `common/account_proxy.py`
- Modify: `tests/test_account_proxy.py`
- Modify: `tests/test_full_flow_ipmart_proxy.py`

**Interfaces:**
- Consumes: `ProxyExitInfo` and `ProxyLease.exit_info` from Task 1.
- Produces: `lease_to_env(lease) -> dict[str, str]` with safe geo fields.
- Produces: `lease_from_env(env) -> ProxyLease | None` with validated geo fields.
- Produces: `BrowserProviderError` and `sanitized_browser_error(exc)` while retaining aliases for old names.

- [ ] **Step 1: Write failing regional round-trip and sanitization tests**

Add to `tests/test_account_proxy.py`:

```python
def make_rich_lease():
    info = ProxyExitInfo(
        "203.0.113.8", "US", "Iowa", "Bloomfield",
        40.7517, -92.4149, "America/Chicago",
    )
    return ProxyLease(
        "http", "gateway.example", 9999,
        "account-res-US-sid-00000042", "secret",
        "00000042", info.ip, info,
    )


class RegionalLeaseTransportTests(unittest.TestCase):
    def test_round_trip_preserves_exit_metadata(self):
        original = make_rich_lease()
        restored = account_proxy.lease_from_env(
            account_proxy.lease_to_env(original)
        )
        self.assertEqual(restored.exit_info, original.exit_info)

    def test_invalid_inherited_timezone_is_rejected(self):
        env = account_proxy.lease_to_env(make_rich_lease())
        env["ACCOUNT_PROXY_TIMEZONE"] = "Invalid/Zone"
        with self.assertRaisesRegex(IPMartProxyError, "timezone"):
            account_proxy.lease_from_env(env)

    def test_sanitized_error_never_contains_credentials(self):
        error = account_proxy.sanitized_browser_error(
            RuntimeError("proxy account-res-US-sid-00000042:secret failed")
        )
        self.assertNotIn("secret", str(error))
        self.assertEqual(error.category, "configuration")
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
python -m unittest tests.test_account_proxy.RegionalLeaseTransportTests -v
```

Expected: failure because regional environment fields and generalized error names do not exist.

- [ ] **Step 3: Implement safe regional transport and compatibility aliases**

In `common/account_proxy.py`, add fields and validation:

```python
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from common.ipmart_proxy import IPMartProxyError, ProxyExitInfo, ProxyLease

ACCOUNT_PROXY_ENV_KEYS = (
    "ACCOUNT_PROXY_SOURCE", "ACCOUNT_PROXY_TYPE", "ACCOUNT_PROXY_HOST",
    "ACCOUNT_PROXY_PORT", "ACCOUNT_PROXY_USERNAME", "ACCOUNT_PROXY_PASSWORD",
    "ACCOUNT_PROXY_SID", "ACCOUNT_PROXY_EXIT_IP", "ACCOUNT_PROXY_COUNTRY",
    "ACCOUNT_PROXY_REGION", "ACCOUNT_PROXY_CITY", "ACCOUNT_PROXY_LATITUDE",
    "ACCOUNT_PROXY_LONGITUDE", "ACCOUNT_PROXY_TIMEZONE",
)


class BrowserProviderError(RuntimeError):
    def __init__(self, category: str):
        if category not in {"quota", "transient", "configuration"}:
            category = "configuration"
        super().__init__("Browser provider failed with IPMart account proxy")
        self.category = category


IPMartBitBrowserError = BrowserProviderError


def sanitized_browser_error(exc: Exception) -> BrowserProviderError:
    message = str(exc).lower()
    if any(marker in message for marker in _BITBROWSER_QUOTA_MARKERS):
        category = "quota"
    elif any(marker in message for marker in _BITBROWSER_TRANSIENT_MARKERS):
        category = "transient"
    else:
        category = "configuration"
    return BrowserProviderError(category)


sanitized_bitbrowser_error = sanitized_browser_error
```

Extend serialization and parsing:

```python
def lease_to_env(lease):
    result = {
        "ACCOUNT_PROXY_SOURCE": "ipmart",
        "ACCOUNT_PROXY_TYPE": lease.proxy_type,
        "ACCOUNT_PROXY_HOST": lease.host,
        "ACCOUNT_PROXY_PORT": str(lease.port),
        "ACCOUNT_PROXY_USERNAME": lease.username,
        "ACCOUNT_PROXY_PASSWORD": lease.password,
        "ACCOUNT_PROXY_SID": lease.sid,
        "ACCOUNT_PROXY_EXIT_IP": lease.exit_ip,
    }
    if lease.exit_info:
        result.update({
            "ACCOUNT_PROXY_COUNTRY": lease.exit_info.country,
            "ACCOUNT_PROXY_REGION": lease.exit_info.region,
            "ACCOUNT_PROXY_CITY": lease.exit_info.city,
            "ACCOUNT_PROXY_LATITUDE": str(lease.exit_info.latitude),
            "ACCOUNT_PROXY_LONGITUDE": str(lease.exit_info.longitude),
            "ACCOUNT_PROXY_TIMEZONE": lease.exit_info.timezone_id,
        })
    return result


def _exit_info_from_env(env, exit_ip):
    timezone_id = (env.get("ACCOUNT_PROXY_TIMEZONE") or "").strip()
    if not timezone_id:
        return None
    try:
        ZoneInfo(timezone_id)
        latitude = float(env["ACCOUNT_PROXY_LATITUDE"])
        longitude = float(env["ACCOUNT_PROXY_LONGITUDE"])
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        raise IPMartProxyError("invalid inherited account proxy timezone or coordinates") from None
    country = (env.get("ACCOUNT_PROXY_COUNTRY") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise IPMartProxyError("invalid inherited account proxy country")
    return ProxyExitInfo(
        exit_ip, country,
        env.get("ACCOUNT_PROXY_REGION", ""),
        env.get("ACCOUNT_PROXY_CITY", ""),
        latitude, longitude, timezone_id,
    )


# At the end of lease_from_env:
exit_info = _exit_info_from_env(env, exit_ip)
return ProxyLease(
    proxy_type, host, port, username, password, sid, exit_ip, exit_info
)
```

- [ ] **Step 4: Run transport and orchestrator tests**

Run:

```powershell
python -m unittest tests.test_account_proxy tests.test_full_flow_ipmart_proxy -v
```

Expected: all tests pass and child environments preserve one regional profile.

- [ ] **Step 5: Commit regional transport**

```powershell
git add common/account_proxy.py tests/test_account_proxy.py tests/test_full_flow_ipmart_proxy.py
git commit -m "feat: transport IPMart regional metadata"
```

---

### Task 3: Build Regional and WebRTC Privacy Validation

**Files:**
- Create: `common/chrome_privacy.py`
- Create: `tests/test_chrome_privacy.py`

**Interfaces:**
- Consumes: `ProxyLease.exit_info` from Task 1.
- Produces: `RegionalProfile(locale, accept_language, timezone_id, latitude, longitude)`.
- Produces: `regional_profile_from_lease(lease, env=None) -> RegionalProfile`.
- Produces: `validate_ice_candidates(candidates, expected_exit_ip) -> None`.
- Produces: `run_chrome_preflight(page, lease, profile, ip_check_url) -> None`.
- Raises: `ChromePrivacyError` with sanitized messages.

- [ ] **Step 1: Write failing regional and ICE validation tests**

Create `tests/test_chrome_privacy.py`:

```python
import unittest
from unittest.mock import AsyncMock, Mock

from common.chrome_privacy import (
    ChromePrivacyError,
    regional_profile_from_lease,
    validate_ice_candidates,
)
from common.ipmart_proxy import ProxyExitInfo, ProxyLease


def lease():
    info = ProxyExitInfo(
        "203.0.113.8", "US", "Iowa", "Bloomfield",
        40.7517, -92.4149, "America/Chicago",
    )
    return ProxyLease(
        "http", "gateway.example", 9999,
        "user", "password", "00000042", info.ip, info,
    )


class ChromePrivacyTests(unittest.TestCase):
    def test_profile_uses_lease_timezone_and_configured_language(self):
        profile = regional_profile_from_lease(lease(), {
            "CHROME_LOCALE": "en-US",
            "CHROME_ACCEPT_LANGUAGE": "en-US,en;q=0.9",
        })
        self.assertEqual(profile.timezone_id, "America/Chicago")
        self.assertEqual(profile.locale, "en-US")

    def test_private_host_candidate_is_rejected(self):
        with self.assertRaisesRegex(ChromePrivacyError, "private"):
            validate_ice_candidates(
                [{"type": "host", "address": "192.168.1.2"}],
                "203.0.113.8",
            )

    def test_unexpected_srflx_public_candidate_is_rejected(self):
        with self.assertRaisesRegex(ChromePrivacyError, "unexpected"):
            validate_ice_candidates(
                [{"type": "srflx", "address": "198.51.100.20"}],
                "203.0.113.8",
            )

    def test_mdns_relay_and_expected_exit_are_allowed(self):
        validate_ice_candidates([
            {"type": "host", "address": "random-name.local"},
            {"type": "relay", "address": "198.51.100.30"},
            {"type": "srflx", "address": "203.0.113.8"},
        ], "203.0.113.8")
```

- [ ] **Step 2: Run the new test and verify failure**

Run:

```powershell
python -m unittest tests.test_chrome_privacy -v
```

Expected: import failure because `common.chrome_privacy` does not exist.

- [ ] **Step 3: Implement regional profile and candidate validation**

Create `common/chrome_privacy.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ChromePrivacyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RegionalProfile:
    locale: str
    accept_language: str
    timezone_id: str
    latitude: float
    longitude: float


def regional_profile_from_lease(lease, env=None):
    env = os.environ if env is None else env
    info = lease.exit_info
    if info is None:
        raise ChromePrivacyError("IPMart exit metadata is required for Chrome")
    locale = (env.get("CHROME_LOCALE") or "en-US").strip()
    accept_language = (
        env.get("CHROME_ACCEPT_LANGUAGE") or "en-US,en;q=0.9"
    ).strip()
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", locale):
        raise ChromePrivacyError("invalid Chrome locale")
    if "\n" in accept_language or "\r" in accept_language:
        raise ChromePrivacyError("invalid Chrome Accept-Language")
    try:
        ZoneInfo(info.timezone_id)
    except ZoneInfoNotFoundError:
        raise ChromePrivacyError("invalid Chrome timezone") from None
    return RegionalProfile(
        locale, accept_language, info.timezone_id,
        info.latitude, info.longitude,
    )


def validate_ice_candidates(candidates, expected_exit_ip):
    expected = ipaddress.ip_address(expected_exit_ip)
    for candidate in candidates:
        kind = str(candidate.get("type", "")).lower()
        address = str(candidate.get("address", "")).strip().strip("[]")
        if not address or address.endswith(".local") or kind == "relay":
            continue
        try:
            observed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if observed.is_private or observed.is_loopback or observed.is_link_local:
            raise ChromePrivacyError("WebRTC exposed a private address")
        if kind in {"host", "srflx"} and observed != expected:
            raise ChromePrivacyError("WebRTC exposed an unexpected public address")
```

- [ ] **Step 4: Add failing async browser-preflight tests**

Append to `tests/test_chrome_privacy.py`:

```python
from common.chrome_privacy import run_chrome_preflight


class ChromePreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_checks_browser_ip_region_and_ice(self):
        page = AsyncMock()
        page.goto.return_value = Mock(status=200)
        page.locator.return_value.inner_text.return_value = json.dumps({
            "ip": "203.0.113.8",
            "country": "US",
            "loc": "40.7517,-92.4149",
            "timezone": "America/Chicago",
        })
        page.evaluate.side_effect = [
            {
                "language": "en-US",
                "languages": ["en-US", "en"],
                "timezone": "America/Chicago",
            },
            [{"type": "relay", "address": "198.51.100.30"}],
        ]
        profile = regional_profile_from_lease(lease())
        await run_chrome_preflight(
            page, lease(), profile, "https://ipinfo.io/json"
        )
        page.goto.assert_awaited_once()

    async def test_preflight_rejects_browser_exit_mismatch(self):
        page = AsyncMock()
        page.goto.return_value = Mock(status=200)
        page.locator.return_value.inner_text.return_value = json.dumps({
            "ip": "198.51.100.9",
        })
        with self.assertRaisesRegex(ChromePrivacyError, "exit"):
            await run_chrome_preflight(
                page, lease(), regional_profile_from_lease(lease()),
                "https://ipinfo.io/json",
            )
```

- [ ] **Step 5: Implement bounded browser preflight**

Append to `common/chrome_privacy.py`:

```python
_ICE_PROBE_JS = """
async ({stunUrl, timeoutMs}) => {
  const pc = new RTCPeerConnection({iceServers: [{urls: stunUrl}]});
  const found = [];
  pc.onicecandidate = event => {
    if (!event.candidate) return;
    const c = event.candidate;
    found.push({type: c.type || "", address: c.address || ""});
  };
  try {
    pc.createDataChannel("probe");
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise(resolve => {
      const timer = setTimeout(resolve, timeoutMs);
      pc.onicegatheringstatechange = () => {
        if (pc.iceGatheringState === "complete") {
          clearTimeout(timer);
          resolve();
        }
      };
    });
    return found;
  } finally {
    pc.close();
  }
}
"""


async def run_chrome_preflight(page, lease, profile, ip_check_url,
                               stun_url="stun:stun.l.google.com:19302"):
    response = await page.goto(ip_check_url, wait_until="domcontentloaded",
                               timeout=30000)
    if response is None or response.status != 200:
        raise ChromePrivacyError("Chrome proxy preflight request failed")
    try:
        payload = json.loads(await page.locator("body").inner_text())
        browser_ip = str(ipaddress.ip_address(payload.get("ip", "")))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ChromePrivacyError("Chrome proxy preflight returned invalid IP") from None
    if browser_ip != lease.exit_ip:
        raise ChromePrivacyError("Chrome proxy exit does not match IPMart lease")
    signals = await page.evaluate("""() => ({
        language: navigator.language,
        languages: Array.from(navigator.languages || []),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    })""")
    if signals.get("language") != profile.locale:
        raise ChromePrivacyError("Chrome locale does not match regional profile")
    if signals.get("timezone") != profile.timezone_id:
        raise ChromePrivacyError("Chrome timezone does not match IPMart exit")
    candidates = await page.evaluate(
        _ICE_PROBE_JS, {"stunUrl": stun_url, "timeoutMs": 5000}
    )
    validate_ice_candidates(candidates, lease.exit_ip)
```

- [ ] **Step 6: Run privacy tests**

Run:

```powershell
python -m unittest tests.test_chrome_privacy -v
```

Expected: all tests pass without live network access.

- [ ] **Step 7: Commit privacy policy**

```powershell
git add common/chrome_privacy.py tests/test_chrome_privacy.py
git commit -m "feat: validate Chrome region and WebRTC privacy"
```

---

### Task 4: Implement Provider-Neutral Browser Sessions

**Files:**
- Create: `common/browser_provider.py`
- Create: `tests/test_browser_provider.py`

**Interfaces:**
- Consumes: `regional_profile_from_lease` and `run_chrome_preflight` from Task 3.
- Produces: `selected_browser_provider(env=None) -> str`.
- Produces: `BrowserOpenRequest(name, account_lease=None, proxy=None, remark="", platform="")`.
- Produces: `BrowserSession(provider, context, page, browser=None, profile_id=None)` with idempotent `async close()`.
- Produces: `open_browser_session(playwright, request, *, env=None) -> BrowserSession`.

- [ ] **Step 1: Write failing provider, launch, and cleanup tests**

Create `tests/test_browser_provider.py`:

```python
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from common.browser_provider import (
    BrowserOpenRequest,
    BrowserProviderConfigurationError,
    open_browser_session,
    selected_browser_provider,
)
from tests.test_chrome_privacy import lease


class ProviderSelectionTests(unittest.TestCase):
    def test_selects_all_supported_providers(self):
        for name in ("bitbrowser", "adspower", "chrome"):
            self.assertEqual(
                selected_browser_provider({"FINGERPRINT_BROWSER": name}), name
            )

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(BrowserProviderConfigurationError):
            selected_browser_provider({"FINGERPRINT_BROWSER": "unknown"})


class NativeChromeBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_chrome_launch_uses_real_channel_proxy_region_and_webrtc(self):
        playwright = Mock()
        context = AsyncMock()
        page = AsyncMock()
        context.pages = [page]
        playwright.chromium.launch_persistent_context = AsyncMock(
            return_value=context
        )
        with tempfile.TemporaryDirectory() as root, patch(
            "common.browser_provider.run_chrome_preflight", AsyncMock()
        ) as preflight:
            session = await open_browser_session(
                playwright,
                BrowserOpenRequest("test", account_lease=lease()),
                env={
                    "FINGERPRINT_BROWSER": "chrome",
                    "CHROME_PROFILE_ROOT": root,
                    "IPMART_IP_CHECK_URL": "https://ipinfo.io/json",
                },
            )
            kwargs = playwright.chromium.launch_persistent_context.await_args.kwargs
            self.assertEqual(kwargs["channel"], "chrome")
            self.assertFalse(kwargs["headless"])
            self.assertEqual(kwargs["timezone_id"], "America/Chicago")
            self.assertNotIn("user_agent", kwargs)
            self.assertIn(
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                kwargs["args"],
            )
            self.assertEqual(kwargs["proxy"]["username"], "user")
            profile_dir = Path(playwright.chromium.launch_persistent_context.await_args.args[0])
            self.assertTrue(profile_dir.exists())
            self.assertIsNot(preflight.await_args.args[0], page)
            preflight.await_args.args[0].close.assert_awaited_once()
            await session.close()
            self.assertFalse(profile_dir.exists())

    async def test_close_is_idempotent(self):
        # Build through the same fake launch, call close twice, and assert the
        # persistent context close coroutine is awaited exactly once.
        playwright = Mock()
        context = AsyncMock()
        context.pages = [AsyncMock()]
        playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
        with tempfile.TemporaryDirectory() as root, patch(
            "common.browser_provider.run_chrome_preflight", AsyncMock()
        ):
            session = await open_browser_session(
                playwright, BrowserOpenRequest("test", account_lease=lease()),
                env={"FINGERPRINT_BROWSER": "chrome", "CHROME_PROFILE_ROOT": root},
            )
            await session.close()
            await session.close()
            context.close.assert_awaited_once()

    async def test_plain_clash_proxy_is_inspected_before_chrome_launch(self):
        playwright = Mock()
        context = AsyncMock()
        context.pages = [AsyncMock()]
        playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
        exit_info = lease().exit_info
        with tempfile.TemporaryDirectory() as root, patch(
            "common.browser_provider.inspect_proxy", return_value=exit_info
        ) as inspect, patch(
            "common.browser_provider.run_chrome_preflight", AsyncMock()
        ):
            session = await open_browser_session(
                playwright,
                BrowserOpenRequest(
                    "test", proxy={"server": "http://127.0.0.1:7890"}
                ),
                env={"FINGERPRINT_BROWSER": "chrome", "CHROME_PROFILE_ROOT": root},
            )
            inspect.assert_called_once()
            kwargs = playwright.chromium.launch_persistent_context.await_args.kwargs
            self.assertEqual(kwargs["timezone_id"], exit_info.timezone_id)
            await session.close()


class ManagedBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_proxy_is_mapped_without_global_env_mutation(self):
        playwright = Mock()
        browser = AsyncMock()
        context = AsyncMock()
        context.pages = [AsyncMock()]
        browser.contexts = [context]
        playwright.chromium.connect_over_cdp = AsyncMock(return_value=browser)
        client = Mock()
        client.create_browser.return_value = "profile-1"
        client.open_browser.return_value = {"ws": "ws://managed"}
        with patch("common.browser_provider.BitBrowser", return_value=client):
            session = await open_browser_session(
                playwright,
                BrowserOpenRequest(
                    "test", proxy={"server": "http://127.0.0.1:7890"}
                ),
                env={"FINGERPRINT_BROWSER": "bitbrowser"},
            )
        kwargs = client.create_browser.call_args.kwargs
        self.assertEqual(kwargs["proxyType"], "http")
        self.assertEqual(kwargs["host"], "127.0.0.1")
        self.assertEqual(kwargs["port"], "7890")
        await session.close()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m unittest tests.test_browser_provider -v
```

Expected: import failure because `common.browser_provider` does not exist.

- [ ] **Step 3: Implement selection, session ownership, and Chrome backend**

Create `common/browser_provider.py` with the focused public surface:

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit

from bitbrowser import BitBrowser
from common.chrome_privacy import (
    regional_profile_from_lease,
    run_chrome_preflight,
)
from common.ipmart_proxy import ProxyLease, inspect_proxy


class BrowserProviderConfigurationError(RuntimeError):
    pass


def selected_browser_provider(env=None):
    env = os.environ if env is None else env
    raw = (env.get("FINGERPRINT_BROWSER") or env.get("BROWSER_PROVIDER")
           or "bitbrowser").strip().lower()
    aliases = {"ads": "adspower", "ads_power": "adspower",
               "google_chrome": "chrome"}
    provider = aliases.get(raw, raw)
    if provider not in {"bitbrowser", "adspower", "chrome"}:
        raise BrowserProviderConfigurationError(
            f"unsupported fingerprint browser provider: {provider}"
        )
    return provider


@dataclass(frozen=True)
class BrowserOpenRequest:
    name: str
    account_lease: object | None = None
    proxy: dict | None = None
    remark: str = ""
    platform: str = ""


class BrowserSession:
    def __init__(self, provider, context, page, *, browser=None,
                 profile_id=None, close_callback=None):
        self.provider = provider
        self.context = context
        self.page = page
        self.browser = browser
        self.profile_id = profile_id
        self._close_callback = close_callback
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def close(self):
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._close_callback:
                await self._close_callback()


def _playwright_proxy(request):
    lease = request.account_lease
    if lease is not None:
        return {
            "server": f"http://{lease.host}:{lease.port}",
            "username": lease.username,
            "password": lease.password,
        }
    return request.proxy


def _inspected_lease(request, env):
    if request.account_lease is not None:
        return request.account_lease
    proxy = request.proxy or {}
    parsed = urlsplit(str(proxy.get("server", "")))
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise BrowserProviderConfigurationError(
            "Chrome requires an explicit valid proxy"
        )
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        raise BrowserProviderConfigurationError(
            "Chrome proxy requires an explicit port"
        )
    lease = ProxyLease(
        parsed.scheme, parsed.hostname, port,
        str(proxy.get("username", "")), str(proxy.get("password", "")),
    )
    info = inspect_proxy(lease, env=env)
    return replace(lease, exit_ip=info.ip, exit_info=info)


def _managed_proxy_fields(proxy):
    parsed = urlsplit(str((proxy or {}).get("server", "")))
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise BrowserProviderConfigurationError("invalid managed-browser proxy")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        raise BrowserProviderConfigurationError("managed proxy requires a port")
    fields = {
        "proxyMethod": 2,
        "proxyType": parsed.scheme,
        "host": parsed.hostname,
        "port": str(port),
    }
    if proxy.get("username"):
        fields["proxyUserName"] = proxy["username"]
    if proxy.get("password"):
        fields["proxyPassword"] = proxy["password"]
    return fields


def _safe_remove_profile(profile_dir, profile_root):
    resolved = profile_dir.resolve()
    root = profile_root.resolve()
    if resolved.parent != root or not resolved.name.startswith("reg-factory-chrome-"):
        raise BrowserProviderConfigurationError("refusing unsafe Chrome profile cleanup")
    shutil.rmtree(resolved, ignore_errors=False)


async def _open_chrome(playwright, request, env):
    inspected_lease = _inspected_lease(request, env)
    profile = regional_profile_from_lease(inspected_lease, env)
    profile_root = Path(
        env.get("CHROME_PROFILE_ROOT")
        or Path(tempfile.gettempdir()) / "reg-factory-chrome"
    ).resolve()
    profile_root.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(tempfile.mkdtemp(
        prefix="reg-factory-chrome-", dir=profile_root
    ))
    launch = {
        "channel": "chrome",
        "headless": False,
        "proxy": _playwright_proxy(request),
        "locale": profile.locale,
        "timezone_id": profile.timezone_id,
        "extra_http_headers": {"Accept-Language": profile.accept_language},
        "no_viewport": True,
        "args": [
            "--no-first-run",
            "--no-default-browser-check",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ],
    }
    executable = (env.get("CHROME_EXECUTABLE_PATH") or "").strip()
    if executable:
        launch.pop("channel")
        launch["executable_path"] = executable
    context = None
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir), **launch
        )
        page = context.pages[0] if context.pages else await context.new_page()
        preflight_page = await context.new_page()
        try:
            await run_chrome_preflight(
                preflight_page, inspected_lease, profile,
                env.get("IPMART_IP_CHECK_URL", "https://ipinfo.io/json"),
            )
        finally:
            await preflight_page.close()
    except BaseException:
        try:
            if context is not None:
                await context.close()
        finally:
            await asyncio.to_thread(
                _safe_remove_profile, profile_dir, profile_root
            )
        raise

    async def close():
        try:
            await context.close()
        finally:
            for attempt in range(3):
                try:
                    await asyncio.to_thread(
                        _safe_remove_profile, profile_dir, profile_root
                    )
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))

    return BrowserSession("chrome", context, page, close_callback=close)
```

- [ ] **Step 4: Implement managed BitBrowser/AdsPower backend**

Append the managed path and dispatcher:

```python
async def _open_managed(playwright, request, provider):
    if provider == "adspower":
        from adspower import AdsPower
        client = AdsPower()
    else:
        client = BitBrowser()
    kwargs = {}
    if request.account_lease is not None:
        from common.account_proxy import bitbrowser_proxy_fields
        kwargs.update(bitbrowser_proxy_fields(request.account_lease))
    elif request.proxy is not None:
        kwargs.update(_managed_proxy_fields(request.proxy))
    if request.remark:
        kwargs["remark"] = request.remark
    if request.platform:
        kwargs["platform"] = request.platform
    profile_id = await asyncio.to_thread(
        client.create_browser, name=request.name, **kwargs
    )
    try:
        data = await asyncio.to_thread(client.open_browser, profile_id)
        browser = await playwright.chromium.connect_over_cdp(data["ws"])
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
    except BaseException:
        await asyncio.to_thread(client.close_browser, profile_id)
        await asyncio.to_thread(client.delete_browser, profile_id)
        raise

    async def close():
        try:
            await browser.close()
        finally:
            try:
                await asyncio.to_thread(client.close_browser, profile_id)
            finally:
                await asyncio.sleep(1)
                await asyncio.to_thread(client.delete_browser, profile_id)

    return BrowserSession(
        provider, context, page, browser=browser,
        profile_id=profile_id, close_callback=close,
    )


async def open_browser_session(playwright, request, *, env=None):
    env = os.environ if env is None else env
    provider = selected_browser_provider(env)
    if provider == "chrome":
        return await _open_chrome(playwright, request, env)
    return await _open_managed(playwright, request, provider)
```

Leave the legacy `BitBrowser.__new__` factory unchanged for compatibility. The new provider layer selects the concrete managed client directly, so it does not mutate process-wide environment variables and does not introduce a circular import.

- [ ] **Step 5: Run provider and existing adapter tests**

Run:

```powershell
python -m unittest tests.test_browser_provider tests.test_platform_proxy_env tests.test_outlook_ipmart_proxy -v
```

Expected: all tests pass; no real Chrome or local browser API starts.

- [ ] **Step 6: Commit provider-neutral sessions**

```powershell
git add common/browser_provider.py tests/test_browser_provider.py
git commit -m "feat: add native Chrome browser sessions"
```

---

### Task 5: Route Common Browser Helpers Through BrowserSession

**Files:**
- Modify: `common/browser.py`
- Modify: `register_chatgpt.py`
- Modify: `register_github.py`
- Modify: `oauth_codex.py`
- Modify: `common/oauth_codex.py`
- Create: `tests/test_common_browser_provider.py`

**Interfaces:**
- Consumes: `open_browser_session` and `BrowserOpenRequest` from Task 4.
- Produces: `open_and_connect(name, p, account_lease=None, proxy=None) -> BrowserSession`.
- Produces: `teardown(session) -> None`.
- Preserves: `inject_stealth` for managed providers only.

- [ ] **Step 1: Write failing common-helper delegation tests**

Create `tests/test_common_browser_provider.py`:

```python
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from common import browser


class CommonBrowserProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_and_connect_returns_provider_session(self):
        expected = Mock(provider="chrome")
        with patch(
            "common.browser.open_browser_session",
            AsyncMock(return_value=expected),
        ) as opener:
            result = await browser.open_and_connect(
                "test", p=Mock(), account_lease=Mock()
            )
        self.assertIs(result, expected)
        opener.assert_awaited_once()

    async def test_chrome_without_lease_gets_explicit_clash_proxy(self):
        expected = Mock(provider="chrome")
        with patch.dict(os.environ, {
            "FINGERPRINT_BROWSER": "chrome",
            "CLASH_PROXY": "http://127.0.0.1:7897",
        }), patch(
            "common.browser.open_browser_session",
            AsyncMock(return_value=expected),
        ) as opener:
            await browser.open_and_connect("test", p=Mock())
        request = opener.await_args.args[1]
        self.assertEqual(
            request.proxy, {"server": "http://127.0.0.1:7897"}
        )

    async def test_teardown_closes_session(self):
        session = Mock(close=AsyncMock())
        await browser.teardown(session)
        session.close.assert_awaited_once()
```

- [ ] **Step 2: Run test and verify failure**

Run:

```powershell
python -m unittest tests.test_common_browser_provider -v
```

Expected: failure because current helpers return a five-item tuple and accept `bb/profile_id` teardown arguments.

- [ ] **Step 3: Replace common lifecycle helpers**

In `common/browser.py`, import the new layer and replace lifecycle functions:

```python
import os

from common.browser_provider import (
    BrowserOpenRequest,
    open_browser_session,
    selected_browser_provider,
)


async def open_and_connect(name, p, account_lease=None, proxy=None,
                           remark="", platform=""):
    if (
        selected_browser_provider() == "chrome"
        and account_lease is None
        and proxy is None
    ):
        clash_proxy = os.environ.get(
            "CLASH_PROXY", "http://127.0.0.1:7897"
        ).strip()
        if not clash_proxy:
            raise RuntimeError(
                "Chrome without an IPMart lease requires CLASH_PROXY"
            )
        proxy = {"server": clash_proxy}
    session = await open_browser_session(
        p,
        BrowserOpenRequest(
            name=name,
            account_lease=account_lease,
            proxy=proxy,
            remark=remark,
            platform=platform,
        ),
    )
    if session.provider != "chrome":
        try:
            await session.context.set_extra_http_headers(
                {"Accept-Language": "en-US,en;q=0.9"}
            )
        except Exception as exc:
            print(f"  set Accept-Language failed: {exc}")
        await inject_stealth(session.context, session.page)
    return session


async def teardown(session):
    await session.close()
```

Keep typing/fill helpers unchanged. Delete `create_browser_with_retry` only after Task 7 removes its remaining callers.

- [ ] **Step 4: Migrate the four common-helper consumers**

Make these exact ownership substitutions while leaving navigation, form filling, cookie export, and OAuth logic unchanged:

- In `register_chatgpt.py`, replace the main `bb, pid, browser, ctx, page` tuple with `session`, `session.context`, and `session.page`. Replace `mail_bb/mail_pid` with `mail_session` in both mailbox-open branches and in the line-764 cleanup block. In the final cleanup, always close Chrome sessions; preserve `KEEP_ON_FAIL` only for managed-provider sessions.
- In `register_github.py`, replace the tuple at the current `open_and_connect` call with `session`. The existing `keep` option may leave a BitBrowser/AdsPower session open, but Chrome must always call `teardown(session)` so its temporary profile is deleted.
- In `oauth_codex.py`, replace `bb/pid/browser/ctx/page` with `session/context/page`; translate the `--keep` condition the same way so it never retains a Chrome profile.
- In `common/oauth_codex.py`, change retry state from `{"bb": None, "pid": None}` to `{"session": None}`. `_new_window` closes the prior session, stores the new one, clears `session.context` cookies, and returns `session.page`; `_cleanup` closes and clears the stored session.

After the edits, all four files must treat `open_and_connect` as a single-return-value function and call `teardown(session)` as a single-argument function. Add assertions to `tests/test_common_browser_provider.py` that managed sessions can be retained only by the caller omitting teardown, while a Chrome consumer's failure path awaits teardown.

Do not change selectors, registration order, cookie extraction, or retry budgets.

- [ ] **Step 5: Run focused common workflow tests**

Run:

```powershell
python -m unittest tests.test_common_browser_provider tests.test_platform_proxy_env tests.test_mailbox_account_proxy -v
```

Expected: all tests pass and mocks observe `BrowserSession.close()`.

- [ ] **Step 6: Commit common helper migration**

```powershell
git add common/browser.py register_chatgpt.py register_github.py oauth_codex.py common/oauth_codex.py tests/test_common_browser_provider.py
git commit -m "refactor: route common browser flows through sessions"
```

---

### Task 6: Migrate Claude and Outlook Account Flows

**Files:**
- Modify: `register.py`
- Modify: `outlook_reg_loop.py`
- Modify: `register_outlook_standalone.py`
- Modify: `run_full_flow.py`
- Modify: `tests/test_claude_ipmart_proxy.py`
- Modify: `tests/test_outlook_ipmart_proxy.py`
- Modify: `tests/test_full_flow_ipmart_proxy.py`

**Interfaces:**
- Consumes: `open_browser_session` from Task 4 and the rich inherited lease from Task 2.
- Produces: `open_claude_session(playwright, name, account_lease) -> BrowserSession` as a focused orchestration seam.
- Produces: Claude and Outlook workflows that receive a ready `BrowserSession` instead of a managed profile ID.
- Preserves: one identical SID lease across Outlook, Graph, mailbox, and Claude.

- [ ] **Step 1: Add failing Chrome-provider orchestration tests**

Add tests that patch `open_browser_session` and assert the inherited lease is passed once. In `tests/test_claude_ipmart_proxy.py`:

```python
def make_lease():
    info = ProxyExitInfo(
        "203.0.113.8", "US", "Iowa", "Bloomfield",
        40.7517, -92.4149, "America/Chicago",
    )
    return ProxyLease(
        "http", "gateway.example", 8080,
        "account-res-US-sid-00000042", "proxy-secret",
        "00000042", info.ip, info,
    )


async def test_chrome_provider_opens_session_with_account_lease(self):
    lease = make_lease()
    session = Mock(
        provider="chrome", context=Mock(), page=AsyncMock(), close=AsyncMock()
    )
    with patch.dict(os.environ, {"FINGERPRINT_BROWSER": "chrome"}), patch(
        "register.open_browser_session", AsyncMock(return_value=session)
    ) as opener:
        result = await register.open_claude_session(
            Mock(), "claude_test", lease
        )
    request = opener.await_args.args[1]
    self.assertIs(request.account_lease, lease)
    self.assertIs(result, session)
```

Import `ProxyExitInfo` in both test modules and import `os` in `tests/test_outlook_ipmart_proxy.py`. In that module, enrich `setUp` with the same metadata and add:

```python
async def test_chrome_one_attempt_uses_session_without_managed_api(self):
    session = unittest.mock.Mock(
        provider="chrome", context=unittest.mock.Mock(),
        page=AsyncMock(), close=AsyncMock(),
    )
    playwright = unittest.mock.Mock()
    apw = unittest.mock.MagicMock()
    apw.__aenter__ = AsyncMock(return_value=playwright)
    apw.__aexit__ = AsyncMock(return_value=False)
    mod = SimpleNamespace(BitBrowserClient=unittest.mock.Mock())
    with patch.dict(os.environ, {"FINGERPRINT_BROWSER": "chrome"}), patch.object(
        outlook_reg_loop, "_apw", return_value=apw
    ), patch.object(
        outlook_reg_loop, "open_browser_session", AsyncMock(return_value=session)
    ) as opener, patch.object(
        outlook_reg_loop, "_run_outlook_on_ctx",
        AsyncMock(return_value=("a@outlook.com", "Pass1!", [])),
    ), patch.object(
        outlook_reg_loop, "_bb_call"
    ) as api_call, patch(
        "common.proxy_switch.set_node"
    ) as set_node:
        result = await outlook_reg_loop.one_attempt(
            mod, "", 1, self.lease
        )
    self.assertEqual(result[0], "a@outlook.com")
    self.assertIs(opener.await_args.args[1].account_lease, self.lease)
    mod.BitBrowserClient.assert_not_called()
    api_call.assert_not_called()
    set_node.assert_not_called()
    session.close.assert_awaited_once()
```

- [ ] **Step 2: Run the new tests and verify failure**

Run:

```powershell
python -m unittest tests.test_claude_ipmart_proxy tests.test_outlook_ipmart_proxy -v
```

Expected: failure because both workflows still create/open managed profiles directly.

- [ ] **Step 3: Refactor Claude to own one BrowserSession**

In `register.py`, replace `create_claude_profile` plus the current profile-ID `register` call with a session-aware entry point:

```python
from common.browser_provider import BrowserOpenRequest, open_browser_session


async def open_claude_session(playwright, name, account_lease):
    return await open_browser_session(
        playwright,
        BrowserOpenRequest(
            name=name,
            account_lease=account_lease,
            remark="claude.ai automatic registration",
            platform="https://claude.ai",
        ),
    )


async def register(session, email="", email_password="", email_token="",
                   account_lease=None):
    context = session.context
    page = session.page
```

Delete the `BitBrowser()` construction, `open_browser(profile_id)`, `async_playwright()`, and `connect_over_cdp` statements from the current `register` function. Keep its timeout, registration, cookie, SMS, and result logic, deindented beneath the new `context/page` assignments. Guard all three existing stealth injection calls with `if session.provider != "chrome"`.

At the current account-task call site near the end of `main`:

```python
session = None
try:
    async with async_playwright() as p:
        session = await open_claude_session(p, name, account_lease)
        sk = await register(
            session, email, email_password, email_token,
            account_lease=account_lease,
        )
finally:
    if session is not None:
        await session.close()
```

Apply the existing large stealth script only when `session.provider != "chrome"`. Remove fixed UA/core-version updates from the Chrome path. Preserve BitBrowser/AdsPower retry categorization around managed sessions only.

- [ ] **Step 4: Refactor Outlook loop to open a session**

In `outlook_reg_loop.py`, make `one_attempt` use the session factory:

```python
async def one_attempt(mod, proxy_str, idx, lease=None):
    session = None
    try:
        async with _apw() as p:
            session = await open_browser_session(
                p,
                BrowserOpenRequest(
                    name=f"outlook_{idx}_{int(time.time())}",
                    account_lease=lease,
                    proxy=None if lease else _proxy_dict(proxy_str),
                    remark="outlook reg loop auto-deleted after use",
                    platform="https://outlook.live.com",
                ),
            )
            return await _run_outlook_on_ctx(mod, session.context, idx)
    finally:
        if session is not None:
            await session.close()
```

Keep `bb_create_for_outlook_reg` as a BitBrowser compatibility helper only until all tests no longer call it. Ensure IPMart continues to skip Clash initialization and rotation.

- [ ] **Step 5: Reuse the session path in standalone browser mode**

In `register_outlook_standalone.py`, change `_register_one_browser` to accept an optional ready session or call the common factory. Keep `_register_one_headless` unchanged because it is a separate explicit headless mode, not the new provider.

```python
async def _register_one_browser(_client, idx, proxy_str, lease=None):
    async with async_playwright() as p:
        session = await open_browser_session(
            p,
            BrowserOpenRequest(
                name=f"outlook_{idx}", account_lease=lease,
                proxy=None if lease else _proxy_for_playwright(proxy_str),
                platform="https://outlook.live.com",
            ),
        )
        try:
            return await register_outlook(
                session.page, session.context, idx,
                captcha_early_abort=False,
            )
        finally:
            await session.close()
```

- [ ] **Step 6: Run all account-proxy orchestration tests**

Run:

```powershell
python -m unittest tests.test_claude_ipmart_proxy tests.test_outlook_ipmart_proxy tests.test_full_flow_ipmart_proxy tests.test_platform_proxy_env -v
```

Expected: all tests pass; assertions prove the same rich lease reaches Outlook and Claude.

- [ ] **Step 7: Commit main account flow migration**

```powershell
git add register.py outlook_reg_loop.py register_outlook_standalone.py run_full_flow.py tests/test_claude_ipmart_proxy.py tests/test_outlook_ipmart_proxy.py tests/test_full_flow_ipmart_proxy.py
git commit -m "feat: run Outlook and Claude with Chrome sessions"
```

---

### Task 7: Migrate Remaining Direct BitBrowser Lifecycles

**Files:**
- Modify: `mailbox_broker.py`
- Modify: `register_grok.py`
- Modify: `unlock_outlook.py`
- Modify: `validate_keys.py`
- Modify: `common/browser.py`
- Create: `tests/test_remaining_browser_providers.py`

**Interfaces:**
- Consumes: `BrowserSession` and `open_browser_session` from Task 4.
- Removes: active workflow dependencies on `bb._post`, raw `ws`, and direct managed-profile lifecycle methods.

- [ ] **Step 1: Write a failing static dependency test**

Create `tests/test_remaining_browser_providers.py`:

```python
import pathlib
import unittest


class RemainingBrowserProviderTests(unittest.TestCase):
    def test_active_workflows_do_not_use_private_bitbrowser_post(self):
        for name in (
            "mailbox_broker.py", "register_grok.py",
            "unlock_outlook.py", "validate_keys.py",
        ):
            text = pathlib.Path(name).read_text(encoding="utf-8")
            self.assertNotIn("._post(\"/browser/update\"", text, name)
            self.assertNotIn("connect_over_cdp(data[\"ws\"])", text, name)
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
python -m unittest tests.test_remaining_browser_providers -v
```

Expected: failure on current private BitBrowser and CDP calls.

- [ ] **Step 3: Migrate mailbox broker sessions**

Replace `Session.pid/browser/ctx/page` ownership with one `browser_session`:

```python
class Session:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.browser_session = None
        self.ctx = None
        self.page = None


# In ensure_session:
s.browser_session = await open_browser_session(
    self.p,
    BrowserOpenRequest(name=f"mbx_{int(time.time())}"),
)
s.ctx = s.browser_session.context
s.page = s.browser_session.page
if s.browser_session.provider != "chrome":
    await inject_stealth(s.ctx, s.page)


# In _close_session:
if s.browser_session:
    await s.browser_session.close()
```

Keep the current `lock`, `last_used`, and login-state assignments after the five fields shown above. When Chrome is selected and no account lease is available for a broker session, pass `{"server": os.environ.get("CLASH_PROXY", "http://127.0.0.1:7897")}` or return a configuration error; never open direct access silently.

- [ ] **Step 4: Migrate Grok, unlock, and validation flows**

Apply these named-function changes:

- `register_grok.py::prelogin_via_direct_browser`: replace `pid/data/browser/ctx/page` with one session. Preserve the current no-proxy managed-profile behavior, but when `selected_browser_provider() == "chrome"` pass `{"server": os.environ.get("CLASH_PROXY", "http://127.0.0.1:7897")}` because native Chrome may not open direct access. Return `(session, session.page)` for reuse.
- `register_grok.py::get_code_via_direct_browser`: consume `(session, page)` and close that session instead of calling `bb.close_browser/delete_browser`; its independent fallback branch applies the same provider-specific proxy choice and opens/closes its own session.
- `register_grok.py::register_one`: pass the current Clash proxy as `{"server": os.environ.get("CLASH_PROXY", "http://127.0.0.1:7897")}`, use `session.context/page`, and close the session in `finally`. Preserve `KEEP_ON_FAIL` for managed providers only; Chrome always closes.
- `unlock_outlook.py::worker`: replace the `create_browser/open_browser/connect_over_cdp` block with `open_browser_session(pw, BrowserOpenRequest(name=f"unlock_{worker_id}", proxy=_proxy_for_playwright(proxy), platform="https://login.live.com"))`; close it in the existing `finally`. Add `_proxy_for_playwright` beside `_parse_proxy`, returning a Playwright proxy dictionary. Remove `create_browser`, `open_browser`, `close_browser`, and `delete_browser` after `worker` no longer calls them; keep `cleanup_stale_browsers` as a managed-provider-only startup cleanup.
- `validate_keys.py::validate_key`: change the signature to `validate_key(sk, playwright, proxy)`, open `BrowserOpenRequest(name=name, proxy=proxy, platform="https://claude.ai")`, retain all current validation checks against `session.page`, and close the session in `finally`. `main` owns one `async_playwright()` block, builds `proxy={"server": os.environ.get("CLASH_PROXY", "http://127.0.0.1:7897")}`, and passes both values to each call.

Do not change Grok HTTP-only flows, captcha logic, cookie formats, validation criteria, or Outlook unlock concurrency.

- [ ] **Step 5: Remove obsolete common retry helper**

After `rg -n "create_browser_with_retry" --glob '*.py'` returns only its definition, delete `create_browser_with_retry` from `common/browser.py`. Keep provider-specific retry logic inside `common/browser_provider.py`.

- [ ] **Step 6: Run remaining workflow and static tests**

Run:

```powershell
python -m unittest tests.test_remaining_browser_providers tests.test_mailbox_account_proxy tests.test_grok_sub2api_flow -v
rg -n "\._post\(\"/browser/update\"|connect_over_cdp\(data\[\"ws\"\]\)" mailbox_broker.py register_grok.py unlock_outlook.py validate_keys.py
```

Expected: unittest passes; `rg` returns no matches and exits 1.

- [ ] **Step 7: Commit remaining migrations**

```powershell
git add mailbox_broker.py register_grok.py unlock_outlook.py validate_keys.py common/browser.py tests/test_remaining_browser_providers.py
git commit -m "refactor: migrate direct browser lifecycles"
```

---

### Task 8: Add Chrome Configuration, WebUI Status, and Documentation

**Files:**
- Modify: `config.py`
- Modify: `.env.example`
- Modify: `webui/scripts.py`
- Modify: `webui/server.py`
- Modify: `webui/static/app.js`
- Modify: `webui/static/index.html`
- Modify: `tests/test_webui_env_reload.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `selected_browser_provider` from Task 4.
- Produces: WebUI provider choice `chrome` and a local installed-Chrome status check.
- Documents: state isolation versus fingerprint isolation boundary.

- [ ] **Step 1: Write failing configuration and status tests**

Add to `tests/test_webui_env_reload.py`:

```python
def test_fingerprint_browser_choices_include_chrome(self):
    group = next(
        item for item in scripts.SETTINGS
        if any(child.get("key") == "FINGERPRINT_BROWSER"
               for child in item["items"])
    )
    provider = next(
        item for item in group["items"]
        if item["key"] == "FINGERPRINT_BROWSER"
    )
    self.assertEqual(provider["choices"], ["bitbrowser", "adspower", "chrome"])


def test_chrome_status_does_not_probe_bitbrowser_api(self):
    with patch.dict(os.environ, {"FINGERPRINT_BROWSER": "chrome"}), patch(
        "webui.server._find_chrome_executable", return_value=r"C:\Chrome\chrome.exe"
    ), patch("webui.server._http_alive") as http_alive:
        status = server.api_status()
    self.assertEqual(status["browser_provider"], "chrome")
    self.assertTrue(status["bitbrowser"])
    http_alive.assert_not_called()
```

- [ ] **Step 2: Run configuration tests and verify failure**

Run:

```powershell
python -m unittest tests.test_webui_env_reload -v
```

Expected: failure because Chrome is absent from choices and status assumes a local API.

- [ ] **Step 3: Add environment settings and safe defaults**

In `config.py`:

```python
FINGERPRINT_BROWSER = _env("FINGERPRINT_BROWSER", "bitbrowser").strip().lower()
IPMART_EXPECTED_COUNTRY = _env("IPMART_EXPECTED_COUNTRY", "US").strip().upper()
IPMART_IP_CHECK_URL = _env("IPMART_IP_CHECK_URL", "https://ipinfo.io/json")
CHROME_EXECUTABLE_PATH = _env("CHROME_EXECUTABLE_PATH", "")
CHROME_LOCALE = _env("CHROME_LOCALE", "en-US")
CHROME_ACCEPT_LANGUAGE = _env(
    "CHROME_ACCEPT_LANGUAGE", "en-US,en;q=0.9"
)
CHROME_WEBRTC_MODE = _env("CHROME_WEBRTC_MODE", "proxy_only")
```

Mirror these safe defaults in `.env.example`; leave proxy username template and password blank.

- [ ] **Step 4: Update WebUI selection and Chrome status**

In `webui/scripts.py`, add `chrome` and the Chrome settings:

```python
{"key": "FINGERPRINT_BROWSER", "type": "choice",
 "choices": ["bitbrowser", "adspower", "chrome"],
 "default": "bitbrowser", "help": "选择浏览器后端"},
{"key": "CHROME_EXECUTABLE_PATH", "help": "可选：chrome.exe 的完整路径"},
{"key": "CHROME_LOCALE", "default": "en-US"},
{"key": "CHROME_ACCEPT_LANGUAGE", "default": "en-US,en;q=0.9"},
{"key": "CHROME_WEBRTC_MODE", "type": "choice",
 "choices": ["proxy_only"], "default": "proxy_only"},
```

In `webui/server.py`, implement executable discovery without launching Chrome:

```python
def _find_chrome_executable():
    configured = _read_config_val("CHROME_EXECUTABLE_PATH", "").strip()
    candidates = [configured] if configured else []
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(key)
        if base:
            candidates.append(os.path.join(
                base, "Google", "Chrome", "Application", "chrome.exe"
            ))
    return next((path for path in candidates if path and os.path.isfile(path)), "")


def _test_fingerprint_browser():
    provider = _fingerprint_provider()
    if provider == "chrome":
        path = _find_chrome_executable()
        return (bool(path), f"Chrome available: {path}" if path
                else "Google Chrome not found")
    return _test_managed_browser(provider)
```

Return the existing `bitbrowser` boolean status key for UI compatibility, but compute it from Chrome availability when provider is `chrome`. Return `browser_provider="chrome"`.

In `webui/static/app.js`, map all three labels explicitly:

```javascript
const labels = {bitbrowser: 'BitBrowser', adspower: 'AdsPower', chrome: 'Chrome'};
const label = labels[s.browser_provider] || 'Browser';
```

Change the initial HTML label to `Browser` so it does not flash a false provider name before status loads.

- [ ] **Step 5: Document behavior and limitations**

Add to README:

```markdown
### Native Chrome provider

Set `FINGERPRINT_BROWSER=chrome` to launch the installed Google Chrome in a
visible window. Each task receives a temporary user-data directory that is
deleted at teardown. With IPMart enabled, Chrome connects directly through the
credentialed SID proxy; timezone is taken from the same SID's IPinfo response,
and WebRTC forbids non-proxied UDP.

Native Chrome isolates cookies, storage, cache, proxy, locale, and timezone. It
does not generate a different Canvas, WebGL, audio, font, GPU, CPU, memory, or
TLS fingerprint for each account. Use BitBrowser or AdsPower when per-account
hardware-fingerprint variation is required.
```

Add a dated CHANGELOG entry listing configuration, lifecycle, privacy behavior, and the explicit non-equivalence to a fingerprint browser.

- [ ] **Step 6: Run WebUI and configuration tests**

Run:

```powershell
python -m unittest tests.test_webui_env_reload tests.test_browser_provider -v
```

Expected: all tests pass without starting Chrome.

- [ ] **Step 7: Commit configuration and docs**

```powershell
git add config.py .env.example webui/scripts.py webui/server.py webui/static/app.js webui/static/index.html tests/test_webui_env_reload.py README.md CHANGELOG.md
git commit -m "docs: expose native Chrome provider"
```

---

### Task 9: Full Verification and Manual Smoke-Test Entry Point

**Files:**
- Create: `scripts/smoke_native_chrome.py`
- Create: `tests/test_native_chrome_smoke.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: public `acquire_proxy` and `open_browser_session` APIs.
- Produces: an explicitly invoked smoke test that consumes one SID but never registers an account.

- [ ] **Step 1: Write a failing smoke-script static test**

Create `tests/test_native_chrome_smoke.py`:

```python
import pathlib
import unittest


class NativeChromeSmokeScriptTests(unittest.TestCase):
    def test_smoke_script_requires_explicit_confirmation(self):
        text = pathlib.Path("scripts/smoke_native_chrome.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("--confirm-consume-one-sid", text)
        self.assertNotIn("register.py", text)
        self.assertNotIn("register_outlook", text)
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
python -m unittest tests.test_native_chrome_smoke -v
```

Expected: `FileNotFoundError` because the smoke script does not exist.

- [ ] **Step 3: Create the explicit smoke script**

Create `scripts/smoke_native_chrome.py`:

```python
import argparse
import asyncio
import os

from playwright.async_api import async_playwright

from common.browser_provider import BrowserOpenRequest, open_browser_session
from common.ipmart_proxy import acquire_proxy


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-consume-one-sid", action="store_true")
    args = parser.parse_args()
    if not args.confirm_consume_one_sid:
        raise SystemExit("pass --confirm-consume-one-sid to run the live check")
    env = dict(os.environ)
    env["FINGERPRINT_BROWSER"] = "chrome"
    lease = await asyncio.to_thread(acquire_proxy, env=env)
    async with async_playwright() as playwright:
        session = await open_browser_session(
            playwright,
            BrowserOpenRequest("native_chrome_smoke", account_lease=lease),
            env=env,
        )
        try:
            info = lease.exit_info
            print(
                "Chrome privacy preflight passed: "
                f"exit={lease.exit_ip} country={info.country} "
                f"timezone={info.timezone_id}"
            )
        finally:
            await session.close()


if __name__ == "__main__":
    asyncio.run(main())
```

The script never prints the gateway username or password.

- [ ] **Step 4: Run the complete automated suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: all tests pass. Live tests that require external credentials remain skipped unless explicitly enabled by their existing gates.

- [ ] **Step 5: Run static secret and provider checks**

Run:

```powershell
rg -n "FINGERPRINT_BROWSER.*bitbrowser.*adspower" config.py .env.example webui README.md
rg -n "proxyPassword|ACCOUNT_PROXY_PASSWORD|IPMART_PROXY_PASSWORD" common scripts README.md CHANGELOG.md
git diff --check
git status --short
```

Expected: provider documentation includes Chrome; credential field names may appear but no configured values or credentialed proxy URLs appear; `git diff --check` is clean; only intentional files and the user's unrelated untracked mail files are shown.

- [ ] **Step 6: Do not run the live smoke test without user authorization**

After confirming that the user has configured fresh credentials and explicitly authorizes consuming one SID, run:

```powershell
python scripts/smoke_native_chrome.py --confirm-consume-one-sid
```

Expected: a visible Chrome window opens, the console prints only exit IP, country, and timezone, preflight passes, Chrome closes, and the temporary profile directory is gone. Do not navigate to Outlook, Claude, ChatGPT, GitHub, or Grok.

- [ ] **Step 7: Commit verification tooling**

```powershell
git add scripts/smoke_native_chrome.py tests/test_native_chrome_smoke.py README.md
git commit -m "test: add native Chrome privacy smoke check"
```

- [ ] **Step 8: Final regression evidence**

Run:

```powershell
python -m unittest discover -s tests -v
git log --oneline -10
git status --short
```

Expected: full suite passes; the task commits are visible; no implementation files remain unstaged; unrelated user mail files remain untouched.
