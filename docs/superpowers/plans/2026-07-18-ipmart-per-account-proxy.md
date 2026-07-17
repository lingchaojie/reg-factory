# IPMart Per-Account Shared Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Acquire one verified, unique US HTTP proxy from IPMart for each account round and use that exact proxy for both Outlook and Claude BitBrowser profiles.

**Architecture:** A provider module owns IPMart requests, exit-IP verification, retries, and the persistent uniqueness ledger. A provider-neutral runtime lease module serializes the selected proxy through child-process environment variables. `run_full_flow.py` acquires once before Outlook, rechecks before Claude, and both browser entry points consume the same lease without falling back to Clash.

**Tech Stack:** Python 3.10+, `requests`, `unittest`, BitBrowser local HTTP API, subprocess environment transport.

## Global Constraints

- IPMart API requests must bypass `HTTP_PROXY`/`HTTPS_PROXY` with `Session.trust_env = False` so source-IP whitelist authentication sees the local public IP.
- Use `num=1`, `cntryCode=US`, `time=30`, and `format=1` by default.
- The BitBrowser proxy type is `http`, without proxy username or password.
- The same lease must be used for Outlook and Claude in one full-flow round.
- Validate the real exit IP before Stage A and again before Stage B; abort if it changes.
- Retry acquisition at most three times and never fall back to Clash, direct access, or an existing BitBrowser profile when IPMart is enabled.
- Never print, persist, or include `IPMART_ACCESS_KEY` in an exception message.
- Existing behavior must remain unchanged when `IPMART_ENABLED=0` or unset.
- Runtime uniqueness is strict for one orchestrator process; independent concurrent orchestrators are unsupported.

---

### Task 1: IPMart Provider, Validation, And Usage Ledger

**Files:**
- Create: `common/ipmart_proxy.py`
- Create: `tests/test_ipmart_proxy.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `ProxyLease(proxy_type: str, host: str, port: int, exit_ip: str)`.
- Produces: `parse_proxy_text(text: str) -> tuple[str, int]`.
- Produces: `verify_proxy(lease: ProxyLease, expected_exit_ip: str | None = None, ...) -> str`.
- Produces: `acquire_proxy(used_exit_ips: set[str] | None = None, usage_path: str | None = None, ...) -> ProxyLease`.
- Produces: `load_used_exit_ips(path: str | None = None) -> set[str]` and `reserve_lease(lease: ProxyLease, path: str | None = None) -> None`.

- [ ] **Step 1: Write failing parser and configuration tests**

```python
# tests/test_ipmart_proxy.py
import unittest

from common import ipmart_proxy


class IPMartProxyTests(unittest.TestCase):
    def test_parse_proxy_text_accepts_one_http_endpoint(self):
        self.assertEqual(
            ipmart_proxy.parse_proxy_text("proxy.example.com:3128\n"),
            ("proxy.example.com", 3128),
        )

    def test_parse_proxy_text_rejects_html_and_invalid_ports(self):
        for body in ("<html>error</html>", "host:not-a-port", "host:70000", ""):
            with self.subTest(body=body):
                with self.assertRaises(ipmart_proxy.IPMartProxyError):
                    ipmart_proxy.parse_proxy_text(body)

    def test_settings_reject_missing_key_when_enabled(self):
        env = {"IPMART_ENABLED": "1", "IPMART_ACCESS_KEY": ""}
        with self.assertRaisesRegex(ipmart_proxy.IPMartProxyError, "IPMART_ACCESS_KEY"):
            ipmart_proxy.settings_from_env(env)
```

- [ ] **Step 2: Run the parser tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_ipmart_proxy.py" -v`

Expected: import failure for missing `common.ipmart_proxy`.

- [ ] **Step 3: Implement the settings model and strict TXT parser**

```python
# common/ipmart_proxy.py
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
import threading
import time
from datetime import datetime, timezone

import requests


DEFAULT_API_BASE = "https://api.ipmart.io/ipmart/common/getIps"
DEFAULT_IP_CHECK_URL = "https://api.ipify.org?format=json"
DEFAULT_USAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ipmart_proxy_usage.jsonl",
)
_USAGE_LOCK = threading.Lock()


class IPMartProxyError(RuntimeError):
    pass


@dataclass(frozen=True)
class IPMartSettings:
    enabled: bool
    access_key: str
    api_base: str
    country: str
    sticky_minutes: int
    max_attempts: int
    ip_check_url: str


@dataclass(frozen=True)
class ProxyLease:
    proxy_type: str
    host: str
    port: int
    exit_ip: str


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def settings_from_env(env=None) -> IPMartSettings:
    env = os.environ if env is None else env
    enabled = _truthy(env.get("IPMART_ENABLED", "0"))
    access_key = (env.get("IPMART_ACCESS_KEY") or "").strip()
    sticky = int(env.get("IPMART_STICKY_MINUTES", "30"))
    attempts = int(env.get("IPMART_MAX_ATTEMPTS", "3"))
    if enabled and not access_key:
        raise IPMartProxyError("IPMART_ACCESS_KEY is required when IPMart is enabled")
    if not 5 <= sticky <= 30:
        raise IPMartProxyError("IPMART_STICKY_MINUTES must be between 5 and 30")
    if attempts < 1:
        raise IPMartProxyError("IPMART_MAX_ATTEMPTS must be positive")
    return IPMartSettings(
        enabled=enabled,
        access_key=access_key,
        api_base=(env.get("IPMART_API_BASE") or DEFAULT_API_BASE).strip(),
        country=(env.get("IPMART_COUNTRY") or "US").strip().upper(),
        sticky_minutes=sticky,
        max_attempts=attempts,
        ip_check_url=(env.get("IPMART_IP_CHECK_URL") or DEFAULT_IP_CHECK_URL).strip(),
    )


def parse_proxy_text(text: str) -> tuple[str, int]:
    body = (text or "").strip()
    if not body or "<html" in body.lower():
        raise IPMartProxyError("IPMart returned no usable proxy endpoint")
    line = next((item.strip() for item in body.splitlines() if item.strip()), "")
    host, sep, raw_port = line.rpartition(":")
    if not sep or not host or not raw_port.isdigit():
        raise IPMartProxyError("IPMart returned a malformed proxy endpoint")
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise IPMartProxyError("IPMart returned an invalid proxy port")
    return host.strip("[]"), port
```

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m unittest discover -s tests -p "test_ipmart_proxy.py" -v`

Expected: 3 tests pass.

- [ ] **Step 5: Add failing acquisition, verification, retry, redaction, and ledger tests**

```python
# append to tests/test_ipmart_proxy.py
import os
import tempfile
from unittest.mock import patch


class FakeResponse:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.trust_env = True
        self.proxies = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class IPMartAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "IPMART_ENABLED": "1",
            "IPMART_ACCESS_KEY": "top-secret-key",
            "IPMART_API_BASE": "https://api.example/getIps",
            "IPMART_COUNTRY": "US",
            "IPMART_STICKY_MINUTES": "30",
            "IPMART_MAX_ATTEMPTS": "3",
            "IPMART_IP_CHECK_URL": "https://check.example/ip",
        }

    def test_acquire_uses_direct_api_and_verifies_through_returned_proxy(self):
        api = FakeSession([FakeResponse(text="edge.example:8080\n")])
        probe = FakeSession([FakeResponse(payload={"ip": "203.0.113.8"})])
        lease = ipmart_proxy.acquire_proxy(
            env=self.env,
            used_exit_ips=set(),
            api_session_factory=lambda: api,
            probe_session_factory=lambda: probe,
            reserve=False,
            sleep=lambda _seconds: None,
        )
        self.assertFalse(api.trust_env)
        self.assertEqual(api.calls[0][1]["params"], {
            "accessKey": "top-secret-key", "num": 1, "cntryCode": "US",
            "time": 30, "format": 1,
        })
        self.assertFalse(probe.trust_env)
        self.assertEqual(probe.proxies, {
            "http": "http://edge.example:8080",
            "https": "http://edge.example:8080",
        })
        self.assertEqual(lease.exit_ip, "203.0.113.8")

    def test_duplicate_exit_retries_and_never_leaks_access_key(self):
        api = FakeSession([
            FakeResponse(text="edge1.example:8001"),
            FakeResponse(text="edge2.example:8002"),
        ])
        probe = FakeSession([
            FakeResponse(payload={"ip": "203.0.113.8"}),
            FakeResponse(payload={"ip": "203.0.113.9"}),
        ])
        lease = ipmart_proxy.acquire_proxy(
            env=self.env,
            used_exit_ips={"203.0.113.8"},
            api_session_factory=lambda: api,
            probe_session_factory=lambda: probe,
            reserve=False,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(lease.exit_ip, "203.0.113.9")
        self.assertNotIn("top-secret-key", repr(lease))

    def test_three_failed_attempts_raise_sanitized_error(self):
        api = FakeSession([FakeResponse(status_code=500, text="failure")] * 3)
        with self.assertRaises(ipmart_proxy.IPMartProxyError) as caught:
            ipmart_proxy.acquire_proxy(
                env=self.env,
                api_session_factory=lambda: api,
                reserve=False,
                sleep=lambda _seconds: None,
            )
        self.assertNotIn("top-secret-key", str(caught.exception))

    def test_usage_ledger_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.jsonl")
            lease = ipmart_proxy.ProxyLease("http", "edge.example", 8080, "203.0.113.8")
            ipmart_proxy.reserve_lease(lease, path)
            self.assertEqual(ipmart_proxy.load_used_exit_ips(path), {"203.0.113.8"})
```

- [ ] **Step 6: Run acquisition tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_ipmart_proxy.py" -v`

Expected: failures for missing `acquire_proxy`, `verify_proxy`, `reserve_lease`, and `load_used_exit_ips`.

- [ ] **Step 7: Implement direct acquisition, proxy verification, and JSONL reservation**

```python
# append to common/ipmart_proxy.py
def _new_direct_session(factory):
    session = factory()
    session.trust_env = False
    session.proxies = {}
    return session


def _read_exit_ip(response) -> str:
    if response.status_code != 200:
        raise IPMartProxyError(f"proxy IP check failed with HTTP {response.status_code}")
    try:
        value = response.json().get("ip", "")
    except (ValueError, AttributeError):
        value = (response.text or "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise IPMartProxyError("proxy IP check returned an invalid address") from exc


def verify_proxy(lease, expected_exit_ip=None, *, env=None, session_factory=requests.Session):
    settings = settings_from_env(env)
    session = _new_direct_session(session_factory)
    proxy_url = f"http://{lease.host}:{lease.port}"
    session.proxies = {"http": proxy_url, "https": proxy_url}
    try:
        response = session.get(settings.ip_check_url, timeout=20)
        exit_ip = _read_exit_ip(response)
    except IPMartProxyError:
        raise
    except Exception as exc:
        raise IPMartProxyError("proxy IP check request failed") from exc
    if expected_exit_ip and exit_ip != expected_exit_ip:
        raise IPMartProxyError(
            f"proxy exit changed: expected {expected_exit_ip}, observed {exit_ip}"
        )
    return exit_ip


def load_used_exit_ips(path=None):
    path = path or DEFAULT_USAGE_PATH
    used = set()
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                    used.add(str(ipaddress.ip_address(value["exit_ip"])))
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    continue
    except FileNotFoundError:
        pass
    return used


def reserve_lease(lease, path=None):
    path = path or DEFAULT_USAGE_PATH
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoint": f"{lease.host}:{lease.port}",
        "exit_ip": lease.exit_ip,
    }
    with _USAGE_LOCK:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=True) + "\n")


def acquire_proxy(
    used_exit_ips=None, usage_path=None, *, env=None,
    api_session_factory=requests.Session,
    probe_session_factory=requests.Session,
    reserve=True, sleep=time.sleep,
):
    settings = settings_from_env(env)
    if not settings.enabled:
        raise IPMartProxyError("IPMart proxy acquisition requested while disabled")
    used = set(used_exit_ips or ()) | load_used_exit_ips(usage_path)
    last_error = "unknown error"
    for attempt in range(1, settings.max_attempts + 1):
        try:
            session = _new_direct_session(api_session_factory)
            response = session.get(settings.api_base, params={
                "accessKey": settings.access_key,
                "num": 1,
                "cntryCode": settings.country,
                "time": settings.sticky_minutes,
                "format": 1,
            }, timeout=20)
            if response.status_code != 200:
                raise IPMartProxyError(f"IPMart API returned HTTP {response.status_code}")
            host, port = parse_proxy_text(response.text)
            candidate = ProxyLease("http", host, port, "")
            exit_ip = verify_proxy(
                candidate, env=env, session_factory=probe_session_factory
            )
            if exit_ip in used:
                raise IPMartProxyError(f"IPMart returned duplicate exit IP {exit_ip}")
            lease = ProxyLease("http", host, port, exit_ip)
            if reserve:
                reserve_lease(lease, usage_path)
            return lease
        except IPMartProxyError as exc:
            last_error = str(exc)
            if attempt < settings.max_attempts:
                sleep(attempt)
        except Exception:
            last_error = "IPMart API request failed"
            if attempt < settings.max_attempts:
                sleep(attempt)
    raise IPMartProxyError(
        f"IPMart proxy acquisition failed after {settings.max_attempts} attempts: {last_error}"
    )
```

- [ ] **Step 8: Ignore the runtime usage ledger and run provider tests**

Add to `.gitignore`:

```gitignore
ipmart_proxy_usage.jsonl
```

Run: `python -m unittest discover -s tests -p "test_ipmart_proxy.py" -v`

Expected: all provider tests pass.

- [ ] **Step 9: Commit the provider task**

```powershell
git add common/ipmart_proxy.py tests/test_ipmart_proxy.py .gitignore
git commit -m "feat: add IPMart proxy provider"
```

---

### Task 2: Runtime Account Proxy Lease

**Files:**
- Create: `common/account_proxy.py`
- Create: `tests/test_account_proxy.py`

**Interfaces:**
- Consumes: `common.ipmart_proxy.ProxyLease`.
- Produces: `lease_to_env(lease: ProxyLease) -> dict[str, str]`.
- Produces: `lease_from_env(env=None) -> ProxyLease | None`.
- Produces: `bitbrowser_proxy_fields(lease: ProxyLease) -> dict[str, object]`.

- [ ] **Step 1: Write failing environment round-trip tests**

```python
# tests/test_account_proxy.py
import unittest

from common.account_proxy import bitbrowser_proxy_fields, lease_from_env, lease_to_env
from common.ipmart_proxy import ProxyLease


class AccountProxyTests(unittest.TestCase):
    def test_runtime_lease_round_trip(self):
        lease = ProxyLease("http", "edge.example", 8080, "203.0.113.8")
        self.assertEqual(lease_from_env(lease_to_env(lease)), lease)

    def test_missing_runtime_lease_returns_none(self):
        self.assertIsNone(lease_from_env({}))

    def test_bitbrowser_fields_have_no_credentials(self):
        lease = ProxyLease("http", "edge.example", 8080, "203.0.113.8")
        fields = bitbrowser_proxy_fields(lease)
        self.assertEqual(fields, {
            "proxyMethod": 2,
            "proxyType": "http",
            "host": "edge.example",
            "port": "8080",
        })
```

- [ ] **Step 2: Run runtime lease tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_account_proxy.py" -v`

Expected: import failure for missing `common.account_proxy`.

- [ ] **Step 3: Implement strict runtime lease serialization**

```python
# common/account_proxy.py
from __future__ import annotations

import ipaddress
import os

from common.ipmart_proxy import IPMartProxyError, ProxyLease


def lease_to_env(lease: ProxyLease) -> dict[str, str]:
    return {
        "ACCOUNT_PROXY_SOURCE": "ipmart",
        "ACCOUNT_PROXY_TYPE": lease.proxy_type,
        "ACCOUNT_PROXY_HOST": lease.host,
        "ACCOUNT_PROXY_PORT": str(lease.port),
        "ACCOUNT_PROXY_EXIT_IP": lease.exit_ip,
    }


def lease_from_env(env=None):
    env = os.environ if env is None else env
    if (env.get("ACCOUNT_PROXY_SOURCE") or "").strip().lower() != "ipmart":
        return None
    proxy_type = (env.get("ACCOUNT_PROXY_TYPE") or "").strip().lower()
    host = (env.get("ACCOUNT_PROXY_HOST") or "").strip()
    raw_port = (env.get("ACCOUNT_PROXY_PORT") or "").strip()
    exit_ip = (env.get("ACCOUNT_PROXY_EXIT_IP") or "").strip()
    if proxy_type != "http" or not host or not raw_port.isdigit():
        raise IPMartProxyError("invalid inherited account proxy lease")
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise IPMartProxyError("invalid inherited account proxy port")
    try:
        exit_ip = str(ipaddress.ip_address(exit_ip))
    except ValueError as exc:
        raise IPMartProxyError("invalid inherited account proxy exit IP") from exc
    return ProxyLease(proxy_type, host, port, exit_ip)


def bitbrowser_proxy_fields(lease):
    return {
        "proxyMethod": 2,
        "proxyType": lease.proxy_type,
        "host": lease.host,
        "port": str(lease.port),
    }
```

- [ ] **Step 4: Run runtime lease and provider tests**

Run: `python -m unittest discover -s tests -p "test_*proxy.py" -v`

Expected: all tests pass.

- [ ] **Step 5: Commit the runtime lease task**

```powershell
git add common/account_proxy.py tests/test_account_proxy.py
git commit -m "feat: add runtime account proxy lease"
```

---

### Task 3: Apply The Shared Lease To Outlook BitBrowser Profiles

**Files:**
- Modify: `outlook_reg_loop.py:361-410,622-669,688-757`
- Create: `tests/test_outlook_ipmart_proxy.py`

**Interfaces:**
- Consumes: `lease_from_env()` and `bitbrowser_proxy_fields()`.
- Changes: `bb_create_for_outlook_reg(name, lease=None)` applies the explicit HTTP proxy.
- Changes: `one_attempt(mod, proxy_str, idx, lease=None)` forwards the inherited/acquired lease.

- [ ] **Step 1: Write failing Outlook profile and rotation tests**

```python
# tests/test_outlook_ipmart_proxy.py
import unittest
from unittest.mock import patch

import outlook_reg_loop
from common.ipmart_proxy import ProxyLease


class OutlookIPMartProxyTests(unittest.TestCase):
    def test_profile_creation_applies_ipmart_http_proxy(self):
        lease = ProxyLease("http", "edge.example", 8080, "203.0.113.8")
        with patch.object(outlook_reg_loop, "_bb_call", return_value={
            "success": True, "data": {"id": "profile-1"}
        }) as call:
            profile_id = outlook_reg_loop.bb_create_for_outlook_reg("outlook-1", lease)
        self.assertEqual(profile_id, "profile-1")
        body = call.call_args.args[1]
        self.assertEqual(body["proxyType"], "http")
        self.assertEqual(body["host"], "edge.example")
        self.assertEqual(body["port"], "8080")
        self.assertNotIn("proxyUserName", body)
        self.assertNotIn("proxyPassword", body)

    def test_ipmart_runtime_lease_disables_clash_rotation(self):
        env = {
            "ACCOUNT_PROXY_SOURCE": "ipmart",
            "ACCOUNT_PROXY_TYPE": "http",
            "ACCOUNT_PROXY_HOST": "edge.example",
            "ACCOUNT_PROXY_PORT": "8080",
            "ACCOUNT_PROXY_EXIT_IP": "203.0.113.8",
        }
        self.assertTrue(outlook_reg_loop.should_skip_clash_rotation(env))
```

- [ ] **Step 2: Run Outlook integration tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_outlook_ipmart_proxy.py" -v`

Expected: failures because the creation function has no lease parameter and `should_skip_clash_rotation` does not exist.

- [ ] **Step 3: Apply inherited lease fields to Outlook profile creation**

```python
# outlook_reg_loop.py imports
from common.account_proxy import bitbrowser_proxy_fields, lease_from_env
from common.ipmart_proxy import acquire_proxy, settings_from_env


def should_skip_clash_rotation(env=None):
    return lease_from_env(os.environ if env is None else env) is not None


def bb_create_for_outlook_reg(name, lease=None):
    proxy_fields = (
        bitbrowser_proxy_fields(lease)
        if lease is not None
        else {"proxyMethod": 2, "proxyType": "noproxy"}
    )
    body = {
        "name": name,
        "remark": "outlook reg loop - auto-deleted after use",
        "platform": "https://outlook.live.com",
        "platformIcon": "outlook.live.com",
        **proxy_fields,
        "browserFingerPrint": {
            "ostype": "PC",
            "os": "Win32",
            "coreVersion": BB_CORE_VERSION,
            "isIpCreateTimeZone": True,
            "isIpCreateLanguage": True,
            "isIpCreateDisplayLanguage": True,
            "isIpCreatePosition": True,
            "isIpCountry": True,
        },
    }
    # Keep the existing AdsPower branch and BitBrowser response validation.
```

Update `one_attempt` to accept `lease` and call:

```python
profile_id = bb_create_for_outlook_reg(f"outlook_loop_{ts}_{idx}", lease)
```

At startup, prefer an inherited lease. Only direct standalone loop usage acquires a lease per attempt:

```python
inherited_lease = lease_from_env()
ipmart_settings = settings_from_env()

# inside the attempt loop
attempt_lease = inherited_lease
if attempt_lease is None and ipmart_settings.enabled:
    attempt_lease = acquire_proxy()

email, password, cookies = asyncio.run(
    asyncio.wait_for(one_attempt(mod, proxy, n, attempt_lease), timeout=args.timeout)
)
```

Set `no_rotate = True` whenever `inherited_lease` is present or IPMart is enabled. Do not initialize or call the Clash controller in that mode.

- [ ] **Step 4: Run Outlook integration and existing proxy tests**

Run: `python -m unittest discover -s tests -p "test_outlook_ipmart_proxy.py" -v`

Expected: Outlook integration tests pass.

Run: `python -m unittest discover -s tests -p "test_proxy_switch.py" -v`

Expected: existing Clash tests pass.

- [ ] **Step 5: Commit the Outlook task**

```powershell
git add outlook_reg_loop.py tests/test_outlook_ipmart_proxy.py
git commit -m "feat: apply account proxy to Outlook profiles"
```

---

### Task 4: Apply The Shared Lease To Claude BitBrowser Profiles

**Files:**
- Modify: `register.py:3827-3960`
- Create: `tests/test_claude_ipmart_proxy.py`

**Interfaces:**
- Consumes: inherited `ProxyLease`, or `acquire_proxy()` for direct usage.
- Produces: `proxy_fields_for_account(lease) -> dict[str, object]` through the shared helper.
- Behavior: an IPMart lease suppresses all Clash node selection and profile overwrites.

- [ ] **Step 1: Write failing Claude lease precedence tests**

```python
# tests/test_claude_ipmart_proxy.py
import unittest
from unittest.mock import patch

import register
from common.ipmart_proxy import ProxyLease


class ClaudeIPMartProxyTests(unittest.TestCase):
    def test_create_profile_uses_inherited_lease(self):
        lease = ProxyLease("http", "edge.example", 8080, "203.0.113.8")
        bb = unittest.mock.Mock()
        bb.create_browser.return_value = "profile-1"
        profile_id = register.create_claude_profile(bb, "claude-1", lease)
        self.assertEqual(profile_id, "profile-1")
        kwargs = bb.create_browser.call_args.kwargs
        self.assertEqual(kwargs["proxyType"], "http")
        self.assertEqual(kwargs["host"], "edge.example")
        self.assertEqual(kwargs["port"], "8080")

    def test_inherited_lease_suppresses_clash_selection(self):
        lease = ProxyLease("http", "edge.example", 8080, "203.0.113.8")
        with patch.object(register, "_pick_claude_node") as pick:
            register.configure_claude_proxy("auto", lease)
        pick.assert_not_called()
```

- [ ] **Step 2: Run Claude tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_claude_ipmart_proxy.py" -v`

Expected: missing `create_claude_profile` and `configure_claude_proxy` failures.

- [ ] **Step 3: Extract testable Claude profile and proxy-selection helpers**

```python
# register.py imports
from common.account_proxy import bitbrowser_proxy_fields, lease_from_env
from common.ipmart_proxy import acquire_proxy, settings_from_env


def configure_claude_proxy(node_arg, account_lease):
    global CLAUDE_PROXY_NODE
    if account_lease is not None:
        CLAUDE_PROXY_NODE = None
        print(
            f"  [proxy] IPMart account proxy {account_lease.host}:"
            f"{account_lease.port} exit={account_lease.exit_ip}"
        )
        return
    # Move the existing none/auto/specific Clash selection block here unchanged.


def create_claude_profile(bb, name, account_lease):
    kwargs = bitbrowser_proxy_fields(account_lease) if account_lease else {}
    return bb.create_browser(name=name, **kwargs)
```

At `main` startup, load `inherited_lease = lease_from_env()` and call `configure_claude_proxy(args.node, inherited_lease)`.

Inside each `run_one`, use the inherited lease, or acquire a new one for direct multi-account Claude runs:

```python
account_lease = inherited_lease
if account_lease is None and settings_from_env().enabled:
    account_lease = await asyncio.to_thread(acquire_proxy)

profile_id = create_claude_profile(bb, name, account_lease)
```

Retain the existing Clash `/browser/update` only when `account_lease is None and CLAUDE_PROXY_NODE`.

- [ ] **Step 4: Run Claude, runtime proxy, and provider tests**

Run: `python -m unittest discover -s tests -p "test_claude_ipmart_proxy.py" -v`

Expected: Claude tests pass.

Run: `python -m unittest discover -s tests -p "test_*proxy.py" -v`

Expected: all proxy-related tests pass.

- [ ] **Step 5: Commit the Claude task**

```powershell
git add register.py tests/test_claude_ipmart_proxy.py
git commit -m "feat: apply account proxy to Claude profiles"
```

---

### Task 5: Acquire Once Per Full-Flow Round And Recheck Before Claude

**Files:**
- Modify: `run_full_flow.py:81-224,277-310`
- Modify: `register_three_platforms.py:157-164`
- Create: `tests/test_full_flow_ipmart_proxy.py`

**Interfaces:**
- Consumes: `acquire_proxy`, `verify_proxy`, `lease_to_env`, and `lease_from_env`.
- Changes: `run_once(args, env, acquire=acquire_proxy, verify=verify_proxy)`.
- Guarantee: the same lease environment reaches both subprocess stages.

- [ ] **Step 1: Write failing full-flow orchestration tests**

```python
# tests/test_full_flow_ipmart_proxy.py
import argparse
import unittest
from unittest.mock import patch

import run_full_flow
from common.ipmart_proxy import IPMartProxyError, ProxyLease


def args_for_test(dry_run=False):
    return argparse.Namespace(
        dry_run=dry_run, skip_email=False, email="", password="",
        platforms=["claude"], node="auto", platform_timeout=600,
        broker="", keep_on_fail=False, import_c2a=False, codex=False,
        codex_group=None, codex_manual_phone=False, grok_sub2api=False,
        grok_sub2api_group=None, email_attempts=1, email_timeout=180,
        email_total_timeout=300, max_press="3",
        email_confirm_before_register=False,
    )


class FullFlowIPMartProxyTests(unittest.TestCase):
    def setUp(self):
        self.lease = ProxyLease("http", "edge.example", 8080, "203.0.113.8")

    def test_one_lease_reaches_both_stages_and_is_rechecked(self):
        args = args_for_test()
        captured = []

        def fake_email(_args, env):
            captured.append(dict(env))
            return ("a@outlook.com", "Pass1!", "rt", "cid")

        def fake_platforms(_args, env, *_account):
            captured.append(dict(env))
            return 0

        with patch.object(run_full_flow, "stage_email", side_effect=fake_email), \
             patch.object(run_full_flow, "stage_platforms", side_effect=fake_platforms):
            rc, _ = run_full_flow.run_once(
                args, {"IPMART_ENABLED": "1", "IPMART_ACCESS_KEY": "secret"},
                acquire=lambda **_kwargs: self.lease,
                verify=lambda lease, expected_exit_ip, **_kwargs: expected_exit_ip,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(captured[0]["ACCOUNT_PROXY_HOST"], "edge.example")
        self.assertEqual(captured[0], captured[1])

    def test_changed_exit_aborts_before_platform_stage(self):
        args = args_for_test()
        with patch.object(run_full_flow, "stage_email", return_value=(
            "a@outlook.com", "Pass1!", "rt", "cid"
        )), patch.object(run_full_flow, "stage_platforms") as platforms:
            rc, _ = run_full_flow.run_once(
                args, {"IPMART_ENABLED": "1", "IPMART_ACCESS_KEY": "secret"},
                acquire=lambda **_kwargs: self.lease,
                verify=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    IPMartProxyError("proxy exit changed")
                ),
            )
        self.assertEqual(rc, 1)
        platforms.assert_not_called()

    def test_dry_run_does_not_consume_ipmart_allocation(self):
        args = args_for_test(dry_run=True)
        with patch.object(run_full_flow, "stage_email", return_value=(
            "dry-run@outlook.com", "Pass1!", "", ""
        )), patch.object(run_full_flow, "stage_platforms", return_value=0):
            rc, _ = run_full_flow.run_once(
                args, {"IPMART_ENABLED": "1", "IPMART_ACCESS_KEY": "secret"},
                acquire=lambda **_kwargs: self.fail("dry-run acquired a proxy"),
            )
        self.assertEqual(rc, 0)
```

- [ ] **Step 2: Run full-flow tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_full_flow_ipmart_proxy.py" -v`

Expected: failures because `run_once` has no injectable acquisition/recheck flow.

- [ ] **Step 3: Acquire a per-round lease and pass it through both stages**

```python
# run_full_flow.py imports
from common.account_proxy import lease_to_env
from common.ipmart_proxy import (
    IPMartProxyError, acquire_proxy, settings_from_env, verify_proxy,
)


def run_once(args, env, acquire=acquire_proxy, verify=verify_proxy):
    t0 = time.time()
    round_env = dict(env)
    account_lease = None
    settings = settings_from_env(round_env)
    if settings.enabled and not args.dry_run:
        try:
            account_lease = acquire(env=round_env)
        except IPMartProxyError as exc:
            log(f"IPMart proxy acquisition failed: {exc}", "ERR")
            return 1, ""
        round_env.update(lease_to_env(account_lease))

    # Existing Stage A logic uses round_env instead of env.

    if account_lease is not None:
        try:
            verify(
                account_lease,
                expected_exit_ip=account_lease.exit_ip,
                env=round_env,
            )
        except IPMartProxyError as exc:
            log(f"IPMart proxy changed before platform registration: {exc}", "ERR")
            return 1, email

    rc = stage_platforms(
        args, round_env, email, password, token, client_id
    )
```

Keep `build_child_env` unchanged for non-IPMart traffic. `register_three_platforms.child_env_for` already copies `os.environ`; add a focused regression test or assertion that all `ACCOUNT_PROXY_*` keys remain present rather than reconstructing the environment.

- [ ] **Step 4: Run full-flow tests and dry-run command**

Run: `python -m unittest discover -s tests -p "test_full_flow_ipmart_proxy.py" -v`

Expected: all full-flow tests pass.

Run: `python run_full_flow.py --dry-run --rounds 1 --platforms claude --node auto`

Expected: Stage A and Stage B commands print; no IPMart API request is made.

- [ ] **Step 5: Commit the orchestration task**

```powershell
git add run_full_flow.py register_three_platforms.py tests/test_full_flow_ipmart_proxy.py
git commit -m "feat: share one IPMart proxy across account flow"
```

---

### Task 6: Configuration, Web UI, Documentation, And Full Verification

**Files:**
- Modify: `.env.example`
- Modify: `config.py:43-55`
- Modify: `webui/scripts.py:286-305`
- Modify: `README.md:210-290,646-653`
- Modify: `CHANGELOG.md`
- Test: `tests/test_ipmart_proxy.py`
- Test: `tests/test_account_proxy.py`
- Test: `tests/test_outlook_ipmart_proxy.py`
- Test: `tests/test_claude_ipmart_proxy.py`
- Test: `tests/test_full_flow_ipmart_proxy.py`

**Interfaces:**
- Documents the exact `.env` contract consumed by `settings_from_env`.
- Exposes `IPMART_ACCESS_KEY` as a secret Web UI field.
- Leaves `IPMART_ENABLED=0` as the backward-compatible default.

- [ ] **Step 1: Add failing schema coverage test**

```python
# append to tests/test_account_proxy.py
from webui import scripts


class IPMartWebUISchemaTests(unittest.TestCase):
    def test_ipmart_configuration_keys_are_exposed(self):
        keys = set(scripts.env_keys())
        self.assertTrue({
            "IPMART_ENABLED", "IPMART_ACCESS_KEY", "IPMART_API_BASE",
            "IPMART_COUNTRY", "IPMART_STICKY_MINUTES",
            "IPMART_MAX_ATTEMPTS", "IPMART_IP_CHECK_URL",
        }.issubset(keys))
```

- [ ] **Step 2: Run schema test and verify RED**

Run: `python -m unittest discover -s tests -p "test_account_proxy.py" -v`

Expected: schema assertion fails because IPMart keys are absent.

- [ ] **Step 3: Add configuration defaults and Web UI fields**

Add to `.env.example`:

```dotenv
# ---------------- IPMart per-account proxy ----------------
IPMART_ENABLED=0
IPMART_ACCESS_KEY=
IPMART_API_BASE=https://api.ipmart.io/ipmart/common/getIps
IPMART_COUNTRY=US
IPMART_STICKY_MINUTES=30
IPMART_MAX_ATTEMPTS=3
IPMART_IP_CHECK_URL=https://api.ipify.org?format=json
```

Add matching constants to `config.py` so `.env` loading establishes the process environment before provider imports:

```python
IPMART_ENABLED = _env("IPMART_ENABLED", "0")
IPMART_ACCESS_KEY = _env("IPMART_ACCESS_KEY", "")
IPMART_API_BASE = _env("IPMART_API_BASE", "https://api.ipmart.io/ipmart/common/getIps")
IPMART_COUNTRY = _env("IPMART_COUNTRY", "US")
IPMART_STICKY_MINUTES = _env("IPMART_STICKY_MINUTES", "30")
IPMART_MAX_ATTEMPTS = _env("IPMART_MAX_ATTEMPTS", "3")
IPMART_IP_CHECK_URL = _env("IPMART_IP_CHECK_URL", "https://api.ipify.org?format=json")
```

Add a Web UI group with `IPMART_ENABLED` as a `1/0` choice, `IPMART_ACCESS_KEY` with `secret: True`, and the remaining exact defaults. Do not add a connectivity-test button because it would consume an IP allocation.

- [ ] **Step 4: Document enablement and operational limits**

Add README instructions containing these commands and guarantees:

```powershell
# Configuration-only preview; does not consume an IPMart allocation
python run_full_flow.py --dry-run --rounds 1 --platforms claude

# Real one-account run; consumes one IPMart allocation
python run_full_flow.py --rounds 1 --platforms claude --node none
```

State that the source public IP must be whitelisted in IPMart, one orchestrator process is required for strict uniqueness, maximum stickiness is 30 minutes, and the flow aborts before Claude if the exit IP changed.

Add a dated CHANGELOG entry summarizing the provider, shared lease, no-fallback behavior, and tests.

- [ ] **Step 5: Run focused and full automated verification**

Run: `python -m unittest discover -s tests -p "test_ipmart_proxy.py" -v`

Run: `python -m unittest discover -s tests -p "test_account_proxy.py" -v`

Run: `python -m unittest discover -s tests -p "test_outlook_ipmart_proxy.py" -v`

Run: `python -m unittest discover -s tests -p "test_claude_ipmart_proxy.py" -v`

Run: `python -m unittest discover -s tests -p "test_full_flow_ipmart_proxy.py" -v`

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass with no unhandled warnings or tracebacks.

- [ ] **Step 6: Run static and diff checks**

Run: `python -m py_compile common/ipmart_proxy.py common/account_proxy.py run_full_flow.py outlook_reg_loop.py register.py register_three_platforms.py`

Run: `git diff --check`

Expected: both commands exit 0 with no output.

- [ ] **Step 7: Perform an explicit real-provider smoke test only with user authorization**

Do not run this step automatically. After the user confirms that `IPMART_ACCESS_KEY` is configured and authorizes consuming one allocation, run a one-shot helper through the public module API, verify the exit IP, create one temporary BitBrowser profile with `bitbrowser_proxy_fields(lease)`, open the IP-check URL, then close and delete that profile. Do not start Outlook or Claude registration in this smoke test.

- [ ] **Step 8: Commit configuration and documentation**

```powershell
git add .env.example config.py webui/scripts.py README.md CHANGELOG.md tests/test_account_proxy.py
git commit -m "docs: configure IPMart account proxies"
```

---

## Plan Self-Review Checklist

- Provider-specific code is isolated in `common/ipmart_proxy.py`.
- Runtime environment transport is isolated in `common/account_proxy.py`.
- The full flow acquires once and both browser stages consume the same lease.
- Pre-Claude verification covers the 30-minute stickiness boundary.
- Direct entry points acquire only when no inherited lease exists.
- Disabled mode remains backward compatible.
- Every new function is introduced by a failing test first.
- No automated test consumes IPMart quota.
- No step logs or persists the access key.
