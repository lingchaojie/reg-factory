# IPMart SID Shared Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace IPMart `getIps` allocation with a credentialed short-lived SID gateway and use one verified SID proxy for Outlook BitBrowser, Microsoft OAuth/Graph, and Claude BitBrowser without Clash.

**Architecture:** `common/ipmart_proxy.py` generates and validates credentialed SID leases while `common/account_proxy.py` transports one lease through child environments and maps it to BitBrowser and requests. Outlook, Microsoft OAuth, Graph mailbox reads (including Claude's `get_magic_link_by_token` path), and Claude consume the same lease; the orchestrators strip inherited HTTP proxy variables only for Outlook/Claude and remove the account lease from ChatGPT/Grok children so unrelated platform behavior stays unchanged.

**Tech Stack:** Python 3.10+, `requests`, `unittest`, BitBrowser local HTTP API, subprocess environment transport.

## Global Constraints

- Use IPMart HTTP username/password gateway authentication and short-lived `sid` mode.
- Generate one cryptographically random eight-digit SID per account round.
- The configured username template must contain exactly one literal `{sid}` placeholder.
- Outlook BitBrowser, Microsoft OAuth token extraction, Graph mailbox reads, and Claude BitBrowser must use one identical lease.
- Perform one initial exit-IP check and one pre-Claude recheck; a normal successful round performs exactly two dedicated IP-check requests.
- Retry initial SID validation at most three times, using a new SID for each attempt.
- If the pre-Claude exit differs, preserve Outlook and stop; never continue with a new SID.
- Do not fall back to Clash, direct access, or an existing BitBrowser profile while IPMart is enabled for Outlook/Claude.
- Preserve current behavior when IPMart is disabled.
- Remove the old access-key `getIps` mode rather than maintaining two modes.
- Never print or persist proxy usernames, passwords, credentialed URLs, or templates.
- Do not perform a real IPMart or account-registration smoke test without explicit user approval.

---

### Task 1: Credentialed SID Provider

**Files:**
- Modify: `common/ipmart_proxy.py`
- Modify: `tests/test_ipmart_proxy.py`

**Interfaces:**
- Produces: `IPMartSettings(enabled, proxy_host, proxy_port, username_template, password, max_attempts, ip_check_url)`.
- Produces: `ProxyLease(proxy_type, host, port, username, password, sid, exit_ip)` with username and password excluded from `repr`.
- Produces: `generate_sid(randbelow=secrets.randbelow) -> str`.
- Produces: `requests_proxy_url(lease: ProxyLease) -> str`.
- Produces: `verify_proxy(lease, expected_exit_ip=None, *, env=None, session_factory=requests.Session) -> str`.
- Produces: `acquire_proxy(used_exit_ips=None, usage_path=None, *, env=None, session_factory=requests.Session, sid_factory=generate_sid, reserve=True, sleep=time.sleep) -> ProxyLease`.

- [ ] **Step 1: Replace API-mode tests with failing SID settings and generation tests**

Replace the old parser/access-key cases in `tests/test_ipmart_proxy.py` with:

```python
import os
import tempfile
import unittest

from common import ipmart_proxy


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
        self.proxies = {"https": "http://inherited.invalid"}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class IPMartProxyTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "IPMART_ENABLED": "1",
            "IPMART_PROXY_HOST": "gateway.example",
            "IPMART_PROXY_PORT": "8080",
            "IPMART_PROXY_USERNAME_TEMPLATE": "account-res-US-sid-{sid}",
            "IPMART_PROXY_PASSWORD": "p@ss/word",
            "IPMART_MAX_ATTEMPTS": "3",
            "IPMART_IP_CHECK_URL": "https://check.example/ip",
        }
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.usage_path = os.path.join(self.tmp.name, "usage.jsonl")

    def test_settings_require_gateway_credentials_and_one_sid_placeholder(self):
        bad = [
            dict(self.env, IPMART_PROXY_HOST=""),
            dict(self.env, IPMART_PROXY_PORT="bad"),
            dict(self.env, IPMART_PROXY_PORT="70000"),
            dict(self.env, IPMART_PROXY_USERNAME_TEMPLATE="account-res-US"),
            dict(self.env, IPMART_PROXY_USERNAME_TEMPLATE="{sid}-{sid}"),
            dict(self.env, IPMART_PROXY_PASSWORD=""),
            dict(self.env, IPMART_MAX_ATTEMPTS="0"),
        ]
        for env in bad:
            with self.subTest(env=env):
                with self.assertRaises(ipmart_proxy.IPMartProxyError):
                    ipmart_proxy.settings_from_env(env)

    def test_generate_sid_is_eight_digits_and_preserves_leading_zeroes(self):
        self.assertEqual(ipmart_proxy.generate_sid(lambda _limit: 42), "00000042")
```

- [ ] **Step 2: Run provider tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_ipmart_proxy.py" -v`

Expected: failures because settings still require `IPMART_ACCESS_KEY` and `generate_sid` does not exist.

- [ ] **Step 3: Implement settings, credential-safe lease, SID generation, and URL encoding**

Replace the API-specific settings/parser portion of `common/ipmart_proxy.py` with:

```python
from dataclasses import dataclass, field, replace
import secrets
from urllib.parse import quote


@dataclass(frozen=True)
class IPMartSettings:
    enabled: bool
    proxy_host: str
    proxy_port: int
    username_template: str = field(repr=False)
    password: str = field(repr=False)
    max_attempts: int
    ip_check_url: str


@dataclass(frozen=True)
class ProxyLease:
    proxy_type: str
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    sid: str
    exit_ip: str


def settings_from_env(env=None) -> IPMartSettings:
    env = os.environ if env is None else env
    enabled = _truthy(env.get("IPMART_ENABLED", "0"))
    host = (env.get("IPMART_PROXY_HOST") or "").strip()
    raw_port = (env.get("IPMART_PROXY_PORT") or "").strip()
    template = (env.get("IPMART_PROXY_USERNAME_TEMPLATE") or "").strip()
    password = env.get("IPMART_PROXY_PASSWORD") or ""
    attempts = _env_int(env, "IPMART_MAX_ATTEMPTS", "3")
    check_url = (env.get("IPMART_IP_CHECK_URL") or DEFAULT_IP_CHECK_URL).strip()
    if enabled:
        if not host:
            raise IPMartProxyError("IPMART_PROXY_HOST is required")
        if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
            raise IPMartProxyError("IPMART_PROXY_PORT must be between 1 and 65535")
        if template.count("{sid}") != 1:
            raise IPMartProxyError(
                "IPMART_PROXY_USERNAME_TEMPLATE must contain exactly one {sid}"
            )
        if not password:
            raise IPMartProxyError("IPMART_PROXY_PASSWORD is required")
    if attempts < 1:
        raise IPMartProxyError("IPMART_MAX_ATTEMPTS must be positive")
    if not check_url:
        raise IPMartProxyError("IPMART_IP_CHECK_URL is required")
    return IPMartSettings(
        enabled=enabled,
        proxy_host=host,
        proxy_port=int(raw_port) if raw_port.isdigit() else 0,
        username_template=template,
        password=password,
        max_attempts=attempts,
        ip_check_url=check_url,
    )


def generate_sid(randbelow=secrets.randbelow) -> str:
    return f"{randbelow(100_000_000):08d}"


def requests_proxy_url(lease: ProxyLease) -> str:
    username = quote(lease.username, safe="")
    password = quote(lease.password, safe="")
    return f"http://{username}:{password}@{lease.host}:{lease.port}"
```

Delete `DEFAULT_API_BASE` and `parse_proxy_text`; no production code should retain the `getIps` request.

- [ ] **Step 4: Run provider tests and verify GREEN for settings/SID**

Run: `python -m unittest discover -s tests -p "test_ipmart_proxy.py" -v`

Expected: the new settings and SID tests pass; acquisition tests are added next.

- [ ] **Step 5: Add failing credentialed verification, retry, ledger, and redaction tests**

Append tests that assert:

```python
    def test_proxy_url_percent_encodes_credentials_without_repr_leak(self):
        lease = ipmart_proxy.ProxyLease(
            "http", "gateway.example", 8080,
            "account-res-US-sid-00000042", "p@ss/word", "00000042", "203.0.113.8",
        )
        self.assertEqual(
            ipmart_proxy.requests_proxy_url(lease),
            "http://account-res-US-sid-00000042:p%40ss%2Fword@gateway.example:8080",
        )
        self.assertNotIn("p@ss/word", repr(lease))
        self.assertNotIn("account-res-US", repr(lease))

    def test_acquire_renders_sid_and_verifies_through_credentialed_proxy(self):
        session = FakeSession([FakeResponse(payload={"ip": "203.0.113.8"})])
        lease = ipmart_proxy.acquire_proxy(
            env=self.env,
            usage_path=self.usage_path,
            session_factory=lambda: session,
            sid_factory=lambda: "00000042",
            reserve=False,
            sleep=lambda _seconds: None,
        )
        self.assertFalse(session.trust_env)
        self.assertEqual(lease.sid, "00000042")
        self.assertEqual(lease.username, "account-res-US-sid-00000042")
        self.assertEqual(lease.exit_ip, "203.0.113.8")
        self.assertEqual(
            session.proxies,
            {
                "http": "http://account-res-US-sid-00000042:p%40ss%2Fword@gateway.example:8080",
                "https": "http://account-res-US-sid-00000042:p%40ss%2Fword@gateway.example:8080",
            },
        )

    def test_duplicate_exit_retries_with_a_new_sid(self):
        session = FakeSession([
            FakeResponse(payload={"ip": "203.0.113.8"}),
            FakeResponse(payload={"ip": "203.0.113.9"}),
        ])
        sids = iter(["00000042", "00000043"])
        lease = ipmart_proxy.acquire_proxy(
            env=self.env,
            used_exit_ips={"203.0.113.8"},
            usage_path=self.usage_path,
            session_factory=lambda: session,
            sid_factory=lambda: next(sids),
            reserve=False,
            sleep=lambda _seconds: None,
        )
        self.assertEqual((lease.sid, lease.exit_ip), ("00000043", "203.0.113.9"))

    def test_ledger_contains_sid_and_exit_but_no_credentials(self):
        lease = ipmart_proxy.ProxyLease(
            "http", "gateway.example", 8080,
            "account-res-US-sid-00000042", "p@ss/word", "00000042", "203.0.113.8",
        )
        ipmart_proxy.reserve_lease(lease, self.usage_path)
        contents = open(self.usage_path, encoding="utf-8").read()
        self.assertIn('"sid": "00000042"', contents)
        self.assertIn('"exit_ip": "203.0.113.8"', contents)
        self.assertNotIn("account-res-US", contents)
        self.assertNotIn("p@ss/word", contents)

    def test_settings_and_errors_do_not_reveal_credentials(self):
        settings = ipmart_proxy.settings_from_env(self.env)
        self.assertNotIn("account-res-US", repr(settings))
        self.assertNotIn("p@ss/word", repr(settings))
        session = FakeSession([FakeResponse(status_code=407, text="denied")])
        with self.assertRaises(ipmart_proxy.IPMartProxyError) as caught:
            ipmart_proxy.acquire_proxy(
                env=dict(self.env, IPMART_MAX_ATTEMPTS="1"),
                usage_path=self.usage_path,
                session_factory=lambda: session,
                sid_factory=lambda: "00000042",
                reserve=False,
                sleep=lambda _seconds: None,
            )
        rendered = str(caught.exception)
        self.assertNotIn("account-res-US", rendered)
        self.assertNotIn("p@ss/word", rendered)

    def test_retry_uses_a_different_sid_and_stops_at_attempt_limit(self):
        session = FakeSession([
            FakeResponse(status_code=502),
            FakeResponse(status_code=502),
            FakeResponse(status_code=502),
        ])
        sids = iter(["00000042", "00000043", "00000044"])
        with self.assertRaises(ipmart_proxy.IPMartProxyError):
            ipmart_proxy.acquire_proxy(
                env=self.env,
                usage_path=self.usage_path,
                session_factory=lambda: session,
                sid_factory=lambda: next(sids),
                reserve=False,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(len(session.calls), 3)

    def test_verify_rejects_changed_exit_through_the_same_proxy(self):
        lease = ipmart_proxy.ProxyLease(
            "http", "gateway.example", 8080,
            "account-res-US-sid-00000042", "p@ss/word", "00000042", "203.0.113.8",
        )
        session = FakeSession([FakeResponse(payload={"ip": "203.0.113.9"})])
        with self.assertRaisesRegex(ipmart_proxy.IPMartProxyError, "exit changed"):
            ipmart_proxy.verify_proxy(
                lease,
                expected_exit_ip=lease.exit_ip,
                env=self.env,
                session_factory=lambda: session,
            )
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies["http"], ipmart_proxy.requests_proxy_url(lease))

    def test_old_ledger_records_still_reserve_exit_ips(self):
        with open(self.usage_path, "w", encoding="utf-8") as stream:
            stream.write('{"endpoint":"old.example:8000","exit_ip":"203.0.113.8"}\n')
        self.assertEqual(
            ipmart_proxy.load_used_exit_ips(self.usage_path),
            {"203.0.113.8"},
        )
```

- [ ] **Step 6: Run tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_ipmart_proxy.py" -v`

Expected: failures because acquisition still calls `getIps`, proxy auth is absent, and the ledger lacks SID.

- [ ] **Step 7: Implement credentialed verification, SID acquisition, and compatible ledger records**

Use `replace(candidate, exit_ip=exit_ip)` to finalize candidates. Replace verification and acquisition with the following shape; retain the existing `_read_exit_ip`, `load_used_exit_ips`, `_USAGE_LOCK`, and `_reserve_if_unique` helpers:

```python
def _credentialed_session(lease, session_factory):
    session = session_factory()
    session.trust_env = False
    proxy_url = requests_proxy_url(lease)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def verify_proxy(
    lease, expected_exit_ip=None, *, env=None,
    session_factory=requests.Session,
):
    settings = settings_from_env(env)
    session = _credentialed_session(lease, session_factory)
    try:
        response = session.get(settings.ip_check_url, timeout=20)
        exit_ip = _read_exit_ip(response)
    except IPMartProxyError:
        raise
    except Exception:
        raise IPMartProxyError("proxy IP check request failed") from None
    if expected_exit_ip and exit_ip != expected_exit_ip:
        raise IPMartProxyError(
            f"proxy exit changed: expected {expected_exit_ip}, observed {exit_ip}"
        )
    return exit_ip


def _next_unique_sid(sid_factory, seen):
    for _ in range(10):
        sid = sid_factory()
        if re.fullmatch(r"\d{8}", sid or "") and sid not in seen:
            seen.add(sid)
            return sid
    raise IPMartProxyError("could not generate a unique eight-digit SID")


def acquire_proxy(
    used_exit_ips=None, usage_path=None, *, env=None,
    session_factory=requests.Session, sid_factory=generate_sid,
    reserve=True, sleep=time.sleep,
):
    settings = settings_from_env(env)
    if not settings.enabled:
        raise IPMartProxyError("IPMart proxy acquisition requested while disabled")
    used = set(used_exit_ips or ()) | load_used_exit_ips(usage_path)
    attempted_sids = set()
    last_error = "proxy validation failed"
    for attempt in range(1, settings.max_attempts + 1):
        try:
            sid = _next_unique_sid(sid_factory, attempted_sids)
            candidate = ProxyLease(
                proxy_type="http",
                host=settings.proxy_host,
                port=settings.proxy_port,
                username=settings.username_template.replace("{sid}", sid),
                password=settings.password,
                sid=sid,
                exit_ip="",
            )
            exit_ip = verify_proxy(
                candidate, env=env, session_factory=session_factory
            )
            if exit_ip in used:
                raise IPMartProxyError(f"duplicate proxy exit IP {exit_ip}")
            lease = replace(candidate, exit_ip=exit_ip)
            if reserve and not _reserve_if_unique(lease, usage_path):
                used.add(exit_ip)
                raise IPMartProxyError(f"duplicate proxy exit IP {exit_ip}")
            return lease
        except IPMartProxyError as exc:
            last_error = str(exc)
        if attempt < settings.max_attempts:
            sleep(attempt)
    raise IPMartProxyError(
        f"IPMart proxy acquisition failed after {settings.max_attempts} "
        f"attempts: {last_error}"
    )
```

Add `import re`. There is no API session, `getIps` URL, access key, country, or sticky-minute parameter in this implementation.

Update `_write_lease` to write only:

```python
record = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "endpoint": f"{lease.host}:{lease.port}",
    "sid": lease.sid,
    "exit_ip": lease.exit_ip,
}
```

Keep `load_used_exit_ips` tolerant of prior records that contain only endpoint and exit IP.

- [ ] **Step 8: Run provider tests and verify GREEN**

Run: `python -m unittest discover -s tests -p "test_ipmart_proxy.py" -v`

Expected: all provider tests pass with no network access.

- [ ] **Step 9: Commit provider migration**

```powershell
git add common/ipmart_proxy.py tests/test_ipmart_proxy.py
git commit -m "feat: migrate IPMart provider to SID credentials"
```

---

### Task 2: Credentialed Runtime Lease And Proxy Environment Isolation

**Files:**
- Modify: `common/account_proxy.py`
- Modify: `tests/test_account_proxy.py`

**Interfaces:**
- Consumes: credentialed `ProxyLease` from Task 1.
- Produces: `lease_to_env`, `lease_from_env`, and `bitbrowser_proxy_fields` with username/password/SID.
- Produces: `strip_http_proxy_env(env: MutableMapping[str, str]) -> MutableMapping[str, str]`.
- Produces: `strip_account_proxy_env(env: MutableMapping[str, str]) -> MutableMapping[str, str]` for non-Claude platform children.

- [ ] **Step 1: Write failing round-trip, BitBrowser credential, validation, and environment-strip tests**

Replace `tests/test_account_proxy.py` with the following focused suite (Task 7 will migrate its final Web UI assertion):

```python
import unittest

from common import account_proxy
from common.ipmart_proxy import IPMartProxyError, ProxyLease
from webui import scripts


def make_lease():
    return ProxyLease(
        "http", "gateway.example", 8080,
        "account-res-US-sid-00000042", "proxy-secret", "00000042", "203.0.113.8",
    )


class AccountProxyTests(unittest.TestCase):
    def test_runtime_lease_round_trip_includes_sid_credentials(self):
        lease = make_lease()
        env = account_proxy.lease_to_env(lease)
        self.assertEqual(env["ACCOUNT_PROXY_SID"], "00000042")
        self.assertEqual(env["ACCOUNT_PROXY_USERNAME"], lease.username)
        self.assertEqual(env["ACCOUNT_PROXY_PASSWORD"], "proxy-secret")
        self.assertEqual(account_proxy.lease_from_env(env), lease)

    def test_missing_runtime_lease_returns_none(self):
        self.assertIsNone(account_proxy.lease_from_env({}))
        self.assertIsNone(
            account_proxy.lease_from_env({"ACCOUNT_PROXY_SOURCE": "clash"})
        )

    def test_bitbrowser_fields_include_credentials(self):
        fields = account_proxy.bitbrowser_proxy_fields(make_lease())
        self.assertEqual(fields["proxyUserName"], "account-res-US-sid-00000042")
        self.assertEqual(fields["proxyPassword"], "proxy-secret")

    def test_invalid_inherited_lease_is_rejected(self):
        valid = account_proxy.lease_to_env(make_lease())
        invalid_cases = [
            dict(valid, ACCOUNT_PROXY_TYPE="socks5"),
            dict(valid, ACCOUNT_PROXY_HOST=""),
            dict(valid, ACCOUNT_PROXY_PORT="bad"),
            dict(valid, ACCOUNT_PROXY_PORT="70000"),
            dict(valid, ACCOUNT_PROXY_USERNAME=""),
            dict(valid, ACCOUNT_PROXY_PASSWORD=""),
            dict(valid, ACCOUNT_PROXY_SID="42"),
            dict(valid, ACCOUNT_PROXY_USERNAME="account-without-session"),
            dict(valid, ACCOUNT_PROXY_EXIT_IP="not-an-ip"),
        ]
        for env in invalid_cases:
            with self.subTest(keys=sorted(env)):
                with self.assertRaises(IPMartProxyError):
                    account_proxy.lease_from_env(env)

    def test_strip_http_proxy_env_removes_all_cases_but_keeps_clash_config(self):
        env = {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "http_proxy": "http://127.0.0.1:7897",
            "https_proxy": "http://127.0.0.1:7897",
            "CLASH_PROXY": "http://127.0.0.1:7897",
        }
        account_proxy.strip_http_proxy_env(env)
        self.assertEqual(env, {"CLASH_PROXY": "http://127.0.0.1:7897"})

    def test_strip_account_proxy_env_removes_the_complete_transient_lease(self):
        env = account_proxy.lease_to_env(make_lease())
        env["CLASH_PROXY"] = "http://127.0.0.1:7897"
        account_proxy.strip_account_proxy_env(env)
        self.assertEqual(env, {"CLASH_PROXY": "http://127.0.0.1:7897"})

    def test_old_ipmart_configuration_keys_remain_until_task_7(self):
        keys = set(scripts.env_keys())
        self.assertIn("IPMART_ACCESS_KEY", keys)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_account_proxy.py" -v`

Expected: failures for missing credential environment fields, credentialless BitBrowser payload, and missing strip helper.

- [ ] **Step 3: Implement the runtime fields and isolation helper**

Replace the transport and BitBrowser mapping with the exact runtime fields below. Validate HTTP type, gateway, port, non-empty username/password, an eight-digit SID, username containing SID, and valid exit IP without including field values in an error:

```python
HTTP_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
ACCOUNT_PROXY_ENV_KEYS = (
    "ACCOUNT_PROXY_SOURCE", "ACCOUNT_PROXY_TYPE", "ACCOUNT_PROXY_HOST",
    "ACCOUNT_PROXY_PORT", "ACCOUNT_PROXY_USERNAME", "ACCOUNT_PROXY_PASSWORD",
    "ACCOUNT_PROXY_SID", "ACCOUNT_PROXY_EXIT_IP",
)


def lease_to_env(lease):
    return {
        "ACCOUNT_PROXY_SOURCE": "ipmart",
        "ACCOUNT_PROXY_TYPE": lease.proxy_type,
        "ACCOUNT_PROXY_HOST": lease.host,
        "ACCOUNT_PROXY_PORT": str(lease.port),
        "ACCOUNT_PROXY_USERNAME": lease.username,
        "ACCOUNT_PROXY_PASSWORD": lease.password,
        "ACCOUNT_PROXY_SID": lease.sid,
        "ACCOUNT_PROXY_EXIT_IP": lease.exit_ip,
    }


def lease_from_env(env=None):
    env = os.environ if env is None else env
    if (env.get("ACCOUNT_PROXY_SOURCE") or "").strip().lower() != "ipmart":
        return None
    proxy_type = (env.get("ACCOUNT_PROXY_TYPE") or "").strip().lower()
    host = (env.get("ACCOUNT_PROXY_HOST") or "").strip()
    raw_port = (env.get("ACCOUNT_PROXY_PORT") or "").strip()
    username = env.get("ACCOUNT_PROXY_USERNAME") or ""
    password = env.get("ACCOUNT_PROXY_PASSWORD") or ""
    sid = (env.get("ACCOUNT_PROXY_SID") or "").strip()
    raw_exit_ip = (env.get("ACCOUNT_PROXY_EXIT_IP") or "").strip()
    if (
        proxy_type != "http" or not host or not raw_port.isdigit()
        or not username or not password or not re.fullmatch(r"\d{8}", sid)
        or sid not in username
    ):
        raise IPMartProxyError("invalid inherited account proxy lease")
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise IPMartProxyError("invalid inherited account proxy port")
    try:
        exit_ip = str(ipaddress.ip_address(raw_exit_ip))
    except ValueError:
        raise IPMartProxyError("invalid inherited account proxy exit IP") from None
    return ProxyLease(
        proxy_type, host, port, username, password, sid, exit_ip
    )


def bitbrowser_proxy_fields(lease):
    return {
        "proxyMethod": 2,
        "proxyType": lease.proxy_type,
        "host": lease.host,
        "port": str(lease.port),
        "proxyUserName": lease.username,
        "proxyPassword": lease.password,
    }


def strip_http_proxy_env(env):
    for key in HTTP_PROXY_ENV_KEYS:
        env.pop(key, None)
    return env


def strip_account_proxy_env(env):
    for key in ACCOUNT_PROXY_ENV_KEYS:
        env.pop(key, None)
    return env
```

Add `import re`. Do not log either mapping; `ACCOUNT_PROXY_PASSWORD` exists only in the child-process environment and is never put on a command line.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m unittest discover -s tests -p "test_account_proxy.py" -v`

Expected: all account-proxy tests pass.

- [ ] **Step 5: Commit runtime transport**

```powershell
git add common/account_proxy.py tests/test_account_proxy.py
git commit -m "feat: transport credentialed account proxy leases"
```

---

### Task 3: Outlook And Microsoft OAuth Use The Account Lease

**Files:**
- Modify: `extract_graph_tokens.py`
- Modify: `outlook_reg_loop.py`
- Modify: `tests/test_outlook_ipmart_proxy.py`
- Create: `tests/test_graph_account_proxy.py`

**Interfaces:**
- Consumes: `requests_proxy_url(lease)` from Task 1.
- Produces: `extract_graph_tokens._oauth_session(proxy_url=None, session_factory=requests.Session)`.
- Changes: `get_graph_token(email, password, idx=0, proxy_url=None)`.
- Changes: `outlook_reg_loop.extract_graph_for_account(email, password, attempts=3, lease=None)`.
- Produces: `outlook_reg_loop.prepare_outlook_network(env=None, *, lease=None, ipmart_enabled=False) -> str`.

- [ ] **Step 1: Write failing OAuth session tests**

Create `tests/test_graph_account_proxy.py`:

```python
import unittest
from unittest.mock import patch

import extract_graph_tokens


class FakeSession:
    def __init__(self):
        self.trust_env = True
        self.proxies = {}


class RaisingSession:
    def post(self, *_args, **_kwargs):
        raise RuntimeError(
            "connect failed via http://user:proxy-secret@gateway.example:8080"
        )
        self.headers = {}


class GraphAccountProxyTests(unittest.TestCase):
    def test_oauth_session_uses_explicit_account_proxy(self):
        fake = FakeSession()
        session = extract_graph_tokens._oauth_session(
            "http://user:pass@gateway.example:8080",
            session_factory=lambda: fake,
        )
        self.assertIs(session, fake)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {
            "http": "http://user:pass@gateway.example:8080",
            "https": "http://user:pass@gateway.example:8080",
        })

    def test_oauth_session_preserves_legacy_environment_mode_without_proxy(self):
        fake = FakeSession()
        session = extract_graph_tokens._oauth_session(
            session_factory=lambda: fake
        )
        self.assertTrue(session.trust_env)
        self.assertEqual(session.proxies, {})

    def test_oauth_exception_output_does_not_reveal_proxy_credentials(self):
        class RaisingSession:
            def get(self, *_args, **_kwargs):
                raise RuntimeError(
                    "connect failed via http://user:proxy-secret@gateway.example:8080"
                )

        with patch.object(
            extract_graph_tokens, "_oauth_session", return_value=RaisingSession()
        ), patch("builtins.print") as printer:
            result = extract_graph_tokens.get_graph_token(
                "a@outlook.com",
                "Pass1!",
                proxy_url="http://user:proxy-secret@gateway.example:8080",
            )
        self.assertIsNone(result)
        rendered = " ".join(str(call) for call in printer.call_args_list)
        self.assertNotIn("user", rendered)
        self.assertNotIn("proxy-secret", rendered)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run OAuth tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_graph_account_proxy.py" -v`

Expected: error because `_oauth_session` does not exist.

- [ ] **Step 3: Extract the OAuth session builder and accept an explicit proxy**

In `extract_graph_tokens.py`:

```python
def _oauth_session(proxy_url=None, session_factory=requests.Session):
    session = session_factory()
    if proxy_url:
        session.trust_env = False
        session.proxies = {"http": proxy_url, "https": proxy_url}
    else:
        session.trust_env = True
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
    })
    return session


def get_graph_token(email, password, idx=0, proxy_url=None):
    session = _oauth_session(proxy_url)
    # Keep the existing OAuth steps unchanged below this point.
```

Replace the final broad exception output in `get_graph_token` so a `requests` proxy exception cannot print its credentialed URL:

```python
except Exception as exc:
    print(f"  {tag} error: {type(exc).__name__}")
    return None
```

- [ ] **Step 4: Add failing Outlook credential, forwarding, and no-Clash tests**

Replace `tests/test_outlook_ipmart_proxy.py` with:

```python
import unittest
from unittest.mock import patch

import outlook_reg_loop
from common import account_proxy
from common.ipmart_proxy import ProxyLease


def make_lease():
    return ProxyLease(
        "http", "gateway.example", 8080,
        "account-res-US-sid-00000042", "proxy-secret", "00000042", "203.0.113.8",
    )


class OutlookIPMartProxyTests(unittest.TestCase):
    def test_profile_creation_applies_raw_ipmart_credentials(self):
        response = {"success": True, "data": {"id": "profile-1"}}
        with patch.object(
            outlook_reg_loop, "_fingerprint_provider", return_value="bitbrowser"
        ), patch.object(outlook_reg_loop, "_bb_call", return_value=response) as call:
            outlook_reg_loop.bb_create_for_outlook_reg("outlook-1", make_lease())
        body = call.call_args.args[1]
        self.assertEqual(body["proxyUserName"], "account-res-US-sid-00000042")
        self.assertEqual(body["proxyPassword"], "proxy-secret")

    def test_profile_creation_keeps_noproxy_without_a_lease(self):
        response = {"success": True, "data": {"id": "profile-1"}}
        with patch.object(
            outlook_reg_loop, "_fingerprint_provider", return_value="bitbrowser"
        ), patch.object(outlook_reg_loop, "_bb_call", return_value=response) as call:
            outlook_reg_loop.bb_create_for_outlook_reg("outlook-1", None)
        body = call.call_args.args[1]
        self.assertEqual(body["proxyType"], "noproxy")
        self.assertNotIn("host", body)

    def test_graph_extraction_forwards_the_lease_proxy(self):
        lease = make_lease()
        with patch("extract_graph_tokens.get_graph_token", return_value={
            "refresh_token": "rt", "client_id": "cid"
        }) as get_token:
            result = outlook_reg_loop.extract_graph_for_account(
                "a@outlook.com", "Pass1!", attempts=1, lease=lease
            )
        self.assertEqual(result["refresh_token"], "rt")
        proxy_url = get_token.call_args.kwargs["proxy_url"]
        self.assertIn("account-res-US-sid-00000042", proxy_url)
        self.assertIn("gateway.example:8080", proxy_url)

    def test_ipmart_network_setup_removes_inherited_clash_proxy(self):
        env = account_proxy.lease_to_env(make_lease())
        env.update({
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "CLASH_PROXY": "http://127.0.0.1:7897",
        })
        with patch.object(outlook_reg_loop, "ensure_clash_proxy_env") as ensure:
            result = outlook_reg_loop.prepare_outlook_network(
                env, lease=make_lease(), ipmart_enabled=True
            )
        self.assertEqual(result, "")
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["CLASH_PROXY"], "http://127.0.0.1:7897")
        ensure.assert_not_called()

    def test_graph_retry_does_not_rotate_clash_with_an_account_lease(self):
        responses = [None, {"refresh_token": "rt", "client_id": "cid"}]
        with patch("extract_graph_tokens.get_graph_token", side_effect=responses), patch(
            "common.proxy_switch.set_node"
        ) as set_node, patch.object(outlook_reg_loop.time, "sleep"):
            result = outlook_reg_loop.extract_graph_for_account(
                "a@outlook.com", "Pass1!", attempts=2, lease=make_lease()
            )
        self.assertEqual(result["refresh_token"], "rt")
        set_node.assert_not_called()

    def test_runtime_lease_disables_clash_rotation(self):
        self.assertTrue(
            outlook_reg_loop.should_skip_clash_rotation(
                account_proxy.lease_to_env(make_lease())
            )
        )
        self.assertFalse(outlook_reg_loop.should_skip_clash_rotation({}))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 5: Run focused Outlook tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_outlook_ipmart_proxy.py" -v`

Expected: profile credentials are absent, `extract_graph_for_account` has no lease parameter, and `prepare_outlook_network` does not exist.

- [ ] **Step 6: Implement Outlook forwarding and skip Clash injection for IPMart**

Import `strip_http_proxy_env` and `requests_proxy_url`. First make the existing helper accept an explicit environment by replacing each `os.environ` access with `env`:

```python
def ensure_clash_proxy_env(env=None):
    env = os.environ if env is None else env
    existing = (
        env.get("HTTPS_PROXY") or env.get("https_proxy")
        or env.get("HTTP_PROXY") or env.get("http_proxy") or ""
    ).strip()
    proxy = existing or env.get("CLASH_PROXY", "").strip()
    if not proxy:
        return ""
    if not existing:
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = env["https_proxy"] = proxy
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
    parts = [part.strip() for part in no_proxy.split(",") if part.strip()]
    for item in ("127.0.0.1", "localhost", "::1"):
        if item not in parts:
            parts.append(item)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(parts)
    return proxy
```

Then add the network preparation helper and change Graph extraction as follows; keep the existing Clash retry block only inside `if lease is None`:

```python
def prepare_outlook_network(env=None, *, lease=None, ipmart_enabled=False):
    env = os.environ if env is None else env
    if lease is not None or ipmart_enabled:
        strip_http_proxy_env(env)
        return ""
    return ensure_clash_proxy_env(env)


def extract_graph_for_account(email, password, attempts=3, lease=None):
    try:
        from extract_graph_tokens import get_graph_token
        proxy_url = requests_proxy_url(lease) if lease is not None else None
        for attempt in range(attempts):
            res = get_graph_token(email, password, proxy_url=proxy_url)
            if res and res.get("refresh_token"):
                graph = {
                    "refresh_token": res["refresh_token"],
                    "client_id": res.get("client_id") or "",
                }
                log(f"graph token extracted for {email}", "OK")
                return graph
            if attempt < attempts - 1:
                if lease is None:
                    try:
                        from common import proxy_switch as proxy_switch_module
                        import random
                        current = proxy_switch_module.current_node()
                        candidates = [
                            node for node in proxy_switch_module.concrete_nodes()
                            if node != current
                        ]
                        if candidates:
                            proxy_switch_module.set_node(random.choice(candidates))
                    except Exception:
                        log("graph retry node switch failed", "WARN")
                time.sleep(3 * (attempt + 1))
    except Exception as exc:
        log(f"graph token extraction error: {type(exc).__name__}", "WARN")
    return None
```

The Clash retry block must run only when `lease is None`. Do not interpolate `exc`, `proxy_url`, the lease, username, or password into logs.

In `main`, replace the unconditional environment setup with:

```python
injected_proxy = prepare_outlook_network(
    os.environ,
    lease=inherited_lease,
    ipmart_enabled=ipmart_settings.enabled,
)
```

Change the success path in `main` to:

```python
graph = extract_graph_for_account(
    email, password, lease=attempt_lease
)
```

- [ ] **Step 7: Run OAuth and Outlook tests and verify GREEN**

Run:

```powershell
python -m unittest discover -s tests -p "test_graph_account_proxy.py" -v
python -m unittest discover -s tests -p "test_outlook_ipmart_proxy.py" -v
```

Expected: both focused suites pass.

- [ ] **Step 8: Commit Outlook/OAuth integration**

```powershell
git add extract_graph_tokens.py outlook_reg_loop.py tests/test_graph_account_proxy.py tests/test_outlook_ipmart_proxy.py
git commit -m "feat: route Outlook OAuth through the account SID"
```

---

### Task 4: Graph Mailbox Reads Use The Account Lease

**Files:**
- Modify: `common/mailbox.py`
- Modify: `register.py`
- Create: `tests/test_mailbox_account_proxy.py`

**Interfaces:**
- Consumes: `lease_from_env` and `requests_proxy_url`.
- Changes: `_ms_session(env=None, session_factory=requests.Session)`.
- Preserves: `register.get_magic_link_by_token(...)`, implemented through `common.mailbox.get_link_by_token` so Claude's Graph token refresh and mailbox reads share `_ms_session`.

- [ ] **Step 1: Write failing mailbox session routing tests**

Create `tests/test_mailbox_account_proxy.py`:

```python
import unittest
from unittest.mock import patch

import register
from common import account_proxy, mailbox
from common.ipmart_proxy import ProxyLease, requests_proxy_url


class FakeSession:
    def __init__(self):
        self.trust_env = True
        self.proxies = {}


def make_lease():
    return ProxyLease(
        "http", "gateway.example", 8080,
        "account-res-US-sid-00000042", "proxy-secret", "00000042", "203.0.113.8",
    )


class MailboxAccountProxyTests(unittest.TestCase):
    def test_ms_session_uses_inherited_account_proxy(self):
        fake = FakeSession()
        lease = make_lease()
        session = mailbox._ms_session(
            account_proxy.lease_to_env(lease), session_factory=lambda: fake
        )
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {
            "http": requests_proxy_url(lease),
            "https": requests_proxy_url(lease),
        })

    def test_ms_session_remains_direct_without_account_lease(self):
        fake = FakeSession()
        session = mailbox._ms_session({}, session_factory=lambda: fake)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {"http": None, "https": None})

    def test_claude_magic_link_helper_delegates_to_lease_aware_mailbox(self):
        with patch(
            "common.mailbox.get_link_by_token", return_value="https://claude.ai/magic-link#abc"
        ) as get_link:
            result = register.get_magic_link_by_token(
                "a@outlook.com", "refresh-token", client_id="cid", max_wait=60
            )
        self.assertEqual(result, "https://claude.ai/magic-link#abc")
        get_link.assert_called_once_with(
            "a@outlook.com",
            "refresh-token",
            client_id="cid",
            link_regex=r"https://claude\.ai/magic-link#[A-Za-z0-9_\-:=+/]+",
            sender_contains=("anthropic", "claude"),
            subject_contains=("magic", "verify", "sign in", "login"),
            must_contain="claude.ai/magic-link#",
            max_wait=60,
            poll=5,
        )

    def test_graph_network_errors_do_not_print_proxy_credentials(self):
        with patch.object(
            mailbox, "_ms_session", return_value=RaisingSession()
        ), patch.object(mailbox.time, "sleep"), patch("builtins.print") as printer:
            result = mailbox._get_access_token("refresh-token")
        self.assertIsNone(result)
        rendered = " ".join(str(call) for call in printer.call_args_list)
        self.assertNotIn("user", rendered)
        self.assertNotIn("proxy-secret", rendered)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run mailbox tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_mailbox_account_proxy.py" -v`

Expected: `_ms_session` rejects the new arguments and Claude's helper still calls module-level `requests.post/get`.

- [ ] **Step 3: Implement lease-aware Microsoft sessions**

Import `lease_from_env` and `requests_proxy_url` in `common/mailbox.py`, then update `_ms_session`:

```python
def _ms_session(env=None, session_factory=requests.Session):
    session = session_factory()
    session.trust_env = False
    lease = lease_from_env(os.environ if env is None else env)
    if lease is None:
        session.proxies = _MS_NO_PROXY
    else:
        proxy_url = requests_proxy_url(lease)
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session
```

Leave token refresh and inbox/junk request behavior unchanged; they already obtain their sessions through `_ms_session`.

Add a sanitized error helper and use it for every exception printed by `_get_access_token` and `fetch_messages` (connection retries, final exhaustion, generic request errors, and parse errors):

```python
def _safe_error(exc):
    return type(exc).__name__ if exc is not None else "unknown error"

# Examples of the required replacements:
print(f"  [mail] token error: {_safe_error(exc)}")
print(f"  [mail] token request retries exhausted: {_safe_error(last_err)}")
print(f"  [mail] fetch {folder} retries exhausted: {_safe_error(exc)}")
print(f"  [mail] fetch {folder} error: {_safe_error(exc)}")
print(f"  [mail] fetch {folder} parse error: {_safe_error(exc)}")
```

No Graph request exception may be formatted with `str(exc)` or `{exc}` because a proxy error can embed the credentialed proxy URL.

Replace the custom `requests.post/get` implementation of `register.get_magic_link_by_token` with:

```python
def get_magic_link_by_token(
    email,
    refresh_token,
    client_id="9e5f94bc-e8a4-4e73-b8be-63364c29d753",
    max_wait=90,
):
    from common.mailbox import get_link_by_token
    return get_link_by_token(
        email,
        refresh_token,
        client_id=client_id,
        link_regex=r"https://claude\.ai/magic-link#[A-Za-z0-9_\-:=+/]+",
        sender_contains=("anthropic", "claude"),
        subject_contains=("magic", "verify", "sign in", "login"),
        must_contain="claude.ai/magic-link#",
        max_wait=max_wait,
        poll=5,
    )
```

- [ ] **Step 4: Run mailbox tests and verify GREEN**

Run: `python -m unittest discover -s tests -p "test_mailbox_account_proxy.py" -v`

Expected: both tests pass.

- [ ] **Step 5: Commit mailbox routing**

```powershell
git add common/mailbox.py register.py tests/test_mailbox_account_proxy.py
git commit -m "feat: route Graph mailbox reads through the account SID"
```

---

### Task 5: Full-Flow Clash Isolation And Platform-Specific Environments

**Files:**
- Modify: `run_full_flow.py`
- Modify: `register_three_platforms.py`
- Modify: `tests/test_full_flow_ipmart_proxy.py`
- Create: `tests/test_platform_proxy_env.py`

**Interfaces:**
- Consumes: `strip_http_proxy_env`, `strip_account_proxy_env`, and runtime lease fields.
- Produces: `register_three_platforms.platform_child_env(platform, base_env) -> dict[str, str]`.

- [ ] **Step 1: Extend full-flow tests to require a credentialed lease and clean account environment**

Replace the `FullFlowIPMartProxyTests` class with:

```python
class FullFlowIPMartProxyTests(unittest.TestCase):
    def setUp(self):
        self.lease = ProxyLease(
            "http", "gateway.example", 8080,
            "account-res-US-sid-00000042", "proxy-secret", "00000042", "203.0.113.8",
        )
        self.base_env = {
            "IPMART_ENABLED": "1",
            "IPMART_PROXY_HOST": "gateway.example",
            "IPMART_PROXY_PORT": "8080",
            "IPMART_PROXY_USERNAME_TEMPLATE": "account-res-US-sid-{sid}",
            "IPMART_PROXY_PASSWORD": "proxy-secret",
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "CLASH_PROXY": "http://127.0.0.1:7897",
        }

    def test_one_lease_reaches_both_stages_and_is_rechecked(self):
        captured = []

        def fake_email(_args, env):
            captured.append(dict(env))
            return ("a@outlook.com", "Pass1!", "rt", "cid")

        def fake_platforms(_args, env, *_account):
            captured.append(dict(env))
            return 0

        verify_calls = []

        def fake_verify(lease, expected_exit_ip=None, **_kwargs):
            verify_calls.append((lease, expected_exit_ip))
            return expected_exit_ip

        with patch.object(
            run_full_flow, "stage_email", side_effect=fake_email
        ), patch.object(
            run_full_flow, "stage_platforms", side_effect=fake_platforms
        ):
            rc, email = run_full_flow.run_once(
                args_for_test(), self.base_env,
                acquire=lambda **_kwargs: self.lease,
                verify=fake_verify,
            )

        self.assertEqual((rc, email), (0, "a@outlook.com"))
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0], captured[1])
        self.assertNotIn("HTTP_PROXY", captured[0])
        self.assertNotIn("HTTPS_PROXY", captured[0])
        self.assertEqual(captured[0]["ACCOUNT_PROXY_SID"], "00000042")
        self.assertEqual(captured[0]["ACCOUNT_PROXY_PASSWORD"], "proxy-secret")
        self.assertEqual(verify_calls, [(self.lease, "203.0.113.8")])

    def test_changed_exit_aborts_before_platform_stage(self):
        with patch.object(
            run_full_flow,
            "stage_email",
            return_value=("a@outlook.com", "Pass1!", "rt", "cid"),
        ), patch.object(run_full_flow, "stage_platforms") as platforms:
            rc, email = run_full_flow.run_once(
                args_for_test(),
                self.base_env,
                acquire=lambda **_kwargs: self.lease,
                verify=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    IPMartProxyError("proxy exit changed")
                ),
            )
        self.assertEqual((rc, email), (1, "a@outlook.com"))
        platforms.assert_not_called()

    def test_acquisition_failure_aborts_before_email_stage(self):
        with patch.object(run_full_flow, "stage_email") as email_stage:
            rc, email = run_full_flow.run_once(
                args_for_test(),
                self.base_env,
                acquire=lambda **_kwargs: (_ for _ in ()).throw(
                    IPMartProxyError("provider unavailable")
                ),
            )
        self.assertEqual((rc, email), (1, ""))
        email_stage.assert_not_called()

    def test_dry_run_does_not_generate_a_sid_or_probe(self):
        with patch.object(
            run_full_flow,
            "stage_email",
            return_value=("dry-run@outlook.com", "Pass1!", "", ""),
        ), patch.object(run_full_flow, "stage_platforms", return_value=0):
            rc, _email = run_full_flow.run_once(
                args_for_test(dry_run=True),
                self.base_env,
                acquire=lambda **_kwargs: self.fail(
                    "dry-run consumed IPMart acquisition"
                ),
                verify=lambda **_kwargs: self.fail(
                    "dry-run consumed IPMart verification"
                ),
            )
        self.assertEqual(rc, 0)
```

- [ ] **Step 2: Run full-flow tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_full_flow_ipmart_proxy.py" -v`

Expected: failure because inherited HTTP proxy variables remain in both stages.

- [ ] **Step 3: Strip inherited HTTP proxies from the IPMart account environment**

After acquisition and `lease_to_env` in `run_once`, call:

```python
round_env.update(lease_to_env(account_lease))
strip_http_proxy_env(round_env)
```

Do not remove `CLASH_PROXY`, `CLASH_API`, `CLASH_SECRET`, or `CLASH_GROUP`; non-Claude platform children need the configuration but the Outlook/Claude child processes must not inherit active HTTP proxy variables.

- [ ] **Step 4: Write failing platform-specific environment tests**

Create `tests/test_platform_proxy_env.py`:

```python
import unittest

import register_three_platforms


class PlatformProxyEnvTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "ACCOUNT_PROXY_SOURCE": "ipmart",
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://stale.invalid",
            "HTTPS_PROXY": "http://stale.invalid",
        }

    def test_claude_child_has_no_environment_http_proxy(self):
        env = register_three_platforms.platform_child_env("claude", self.env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["ACCOUNT_PROXY_SOURCE"], "ipmart")

    def test_chatgpt_and_grok_restore_existing_clash_behavior(self):
        for platform in ("chatgpt", "grok"):
            with self.subTest(platform=platform):
                env = register_three_platforms.platform_child_env(platform, self.env)
                self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7897")
                self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7897")
                self.assertNotIn("ACCOUNT_PROXY_SOURCE", env)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 5: Run platform environment tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_platform_proxy_env.py" -v`

Expected: error because `platform_child_env` does not exist.

- [ ] **Step 6: Implement platform-specific child environments**

In `register_three_platforms.py`:

```python
def platform_child_env(platform, base_env):
    env = dict(base_env)
    if platform == "claude" and env.get("ACCOUNT_PROXY_SOURCE") == "ipmart":
        strip_http_proxy_env(env)
        return env
    if platform in {"chatgpt", "grok"}:
        strip_account_proxy_env(env)
        clash_proxy = (env.get("CLASH_PROXY") or "").strip()
        if clash_proxy:
            env["HTTP_PROXY"] = env["HTTPS_PROXY"] = clash_proxy
            env["http_proxy"] = env["https_proxy"] = clash_proxy
    return env
```

Pass `platform_child_env(platform, child_env)` to each `run_platform` call in sequential and parallel modes. Removing `ACCOUNT_PROXY_*` from ChatGPT/Grok is required: otherwise their calls into `common.mailbox` would incorrectly use the Claude SID even after their browser HTTP proxy was restored to Clash.

- [ ] **Step 7: Run orchestration tests and verify GREEN**

Run:

```powershell
python -m unittest discover -s tests -p "test_full_flow_ipmart_proxy.py" -v
python -m unittest discover -s tests -p "test_platform_proxy_env.py" -v
```

Expected: both suites pass.

- [ ] **Step 8: Commit orchestration isolation**

```powershell
git add run_full_flow.py register_three_platforms.py tests/test_full_flow_ipmart_proxy.py tests/test_platform_proxy_env.py
git commit -m "feat: isolate Outlook and Claude from Clash"
```

---

### Task 6: Claude Uses Credentialed SID Profiles

**Files:**
- Modify: `register.py`
- Modify: `tests/test_claude_ipmart_proxy.py`

**Interfaces:**
- Consumes: credentialed runtime lease and `bitbrowser_proxy_fields` from Task 2.
- Preserves: `configure_claude_proxy` and `create_claude_profile` public helper names.
- Produces: `prepare_claude_network(env=None, *, account_lease=None, ipmart_enabled=False)`.

- [ ] **Step 1: Update Claude tests to require credentials and no inherited environment proxy**

Replace `tests/test_claude_ipmart_proxy.py` with:

```python
import unittest
from unittest.mock import Mock, patch

import register
from common.ipmart_proxy import ProxyLease


def make_lease():
    return ProxyLease(
        "http", "gateway.example", 8080,
        "account-res-US-sid-00000042", "proxy-secret", "00000042", "203.0.113.8",
    )


class ClaudeIPMartProxyTests(unittest.TestCase):
    def test_create_profile_uses_inherited_credentialed_lease(self):
        bb = Mock()
        bb.create_browser.return_value = "profile-1"
        profile_id = register.create_claude_profile(bb, "claude-1", make_lease())
        self.assertEqual(profile_id, "profile-1")
        self.assertEqual(bb.create_browser.call_args.kwargs, {
            "name": "claude-1",
            "proxyMethod": 2,
            "proxyType": "http",
            "host": "gateway.example",
            "port": "8080",
            "proxyUserName": "account-res-US-sid-00000042",
            "proxyPassword": "proxy-secret",
        })

    def test_create_profile_preserves_default_without_lease(self):
        bb = Mock()
        register.create_claude_profile(bb, "claude-1", None)
        bb.create_browser.assert_called_once_with(name="claude-1")

    def test_inherited_lease_suppresses_clash_selection(self):
        with patch.object(register, "_pick_claude_node") as pick, patch.object(
            register.proxy_switch, "set_node"
        ) as set_node:
            register.configure_claude_proxy("auto", make_lease())
        pick.assert_not_called()
        set_node.assert_not_called()
        self.assertIsNone(register.CLAUDE_PROXY_NODE)

    def test_enabled_ipmart_suppresses_clash_before_direct_acquisition(self):
        with patch.object(register, "_pick_claude_node") as pick:
            register.configure_claude_proxy(
                "auto", account_lease=None, ipmart_enabled=True
            )
        pick.assert_not_called()
        self.assertIsNone(register.CLAUDE_PROXY_NODE)

    def test_ipmart_strips_process_http_proxy_before_acquisition(self):
        env = {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "CLASH_PROXY": "http://127.0.0.1:7897",
        }
        register.prepare_claude_network(
            env, account_lease=make_lease(), ipmart_enabled=False
        )
        self.assertEqual(env, {"CLASH_PROXY": "http://127.0.0.1:7897"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run Claude tests and verify RED**

Run: `python -m unittest discover -s tests -p "test_claude_ipmart_proxy.py" -v`

Expected: BitBrowser payload mismatch and missing `prepare_claude_network`.

- [ ] **Step 3: Implement process isolation and use credentialed fields**

`create_claude_profile` already delegates to `bitbrowser_proxy_fields`; Task 2 supplies credentials. Import `strip_http_proxy_env` and add:

```python
def prepare_claude_network(
    env=None, *, account_lease=None, ipmart_enabled=False
):
    env = os.environ if env is None else env
    if account_lease is not None or ipmart_enabled:
        strip_http_proxy_env(env)
    return env
```

Call it after loading settings and before `configure_claude_proxy` or direct acquisition:

```python
prepare_claude_network(
    os.environ,
    account_lease=inherited_lease,
    ipmart_enabled=ipmart_settings.enabled,
)
configure_claude_proxy(
    args.node,
    inherited_lease,
    ipmart_enabled=ipmart_settings.enabled,
)
```

Keep `configure_claude_proxy` behavior: inherited lease or enabled IPMart suppresses all Clash node selection and updates.

- [ ] **Step 4: Run Claude tests and verify GREEN**

Run: `python -m unittest discover -s tests -p "test_claude_ipmart_proxy.py" -v`

Expected: all Claude proxy tests pass.

- [ ] **Step 5: Commit Claude integration**

```powershell
git add register.py tests/test_claude_ipmart_proxy.py
git commit -m "feat: use credentialed SID proxies for Claude"
```

---

### Task 7: Configuration, Web UI, Documentation, And Final Verification

**Files:**
- Modify: `.env.example`
- Modify: `config.py`
- Modify: `webui/scripts.py`
- Modify: `tests/test_account_proxy.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Exposes: new SID gateway settings in the template, config module, and Web UI.
- Removes: access-key/API/country/sticky settings from active configuration surfaces.

- [ ] **Step 1: Write failing Web UI schema migration test**

Replace the old schema assertion inside `AccountProxyTests` with this class-body method:

```python
    def test_ipmart_sid_configuration_is_exposed_in_webui(self):
        groups = scripts.ENV_SCHEMA
        items = {
            item["key"]: item
            for group in groups
            for item in group.get("items", [])
        }
        expected = {
            "IPMART_ENABLED",
            "IPMART_PROXY_HOST",
            "IPMART_PROXY_PORT",
            "IPMART_PROXY_USERNAME_TEMPLATE",
            "IPMART_PROXY_PASSWORD",
            "IPMART_MAX_ATTEMPTS",
            "IPMART_IP_CHECK_URL",
        }
        self.assertTrue(expected.issubset(items))
        self.assertTrue(items["IPMART_PROXY_USERNAME_TEMPLATE"]["secret"])
        self.assertTrue(items["IPMART_PROXY_PASSWORD"]["secret"])
        for obsolete in (
            "IPMART_ACCESS_KEY", "IPMART_API_BASE",
            "IPMART_COUNTRY", "IPMART_STICKY_MINUTES",
        ):
            self.assertNotIn(obsolete, items)
```

- [ ] **Step 2: Run schema test and verify RED**

Run: `python -m unittest discover -s tests -p "test_account_proxy.py" -v`

Expected: missing new keys and obsolete keys still present.

- [ ] **Step 3: Replace configuration surfaces**

Replace the obsolete `.env.example` block with:

```dotenv
IPMART_ENABLED=0
IPMART_PROXY_HOST=
IPMART_PROXY_PORT=
IPMART_PROXY_USERNAME_TEMPLATE=
IPMART_PROXY_PASSWORD=
IPMART_MAX_ATTEMPTS=3
IPMART_IP_CHECK_URL=https://api.ipify.org?format=json
```

Replace the IPMart exports in `config.py` with:

```python
IPMART_ENABLED = _env("IPMART_ENABLED", "0").strip().lower() in (
    "1", "true", "yes", "on"
)
IPMART_PROXY_HOST = _env("IPMART_PROXY_HOST", "")
IPMART_PROXY_PORT = _env("IPMART_PROXY_PORT", "")
IPMART_PROXY_USERNAME_TEMPLATE = _env("IPMART_PROXY_USERNAME_TEMPLATE", "")
IPMART_PROXY_PASSWORD = _env("IPMART_PROXY_PASSWORD", "")
IPMART_MAX_ATTEMPTS = int(_env("IPMART_MAX_ATTEMPTS", "3") or "3")
IPMART_IP_CHECK_URL = _env(
    "IPMART_IP_CHECK_URL", "https://api.ipify.org?format=json"
)
```

Replace the IPMart `ENV_SCHEMA` items in `webui/scripts.py` with these keys and properties; the group must not define a `tests` entry:

```python
{"key": "IPMART_ENABLED", "type": "choice", "choices": ["0", "1"], "default": "0"},
{"key": "IPMART_PROXY_HOST"},
{"key": "IPMART_PROXY_PORT", "type": "int"},
{"key": "IPMART_PROXY_USERNAME_TEMPLATE", "secret": True},
{"key": "IPMART_PROXY_PASSWORD", "secret": True},
{"key": "IPMART_MAX_ATTEMPTS", "type": "int", "default": 3},
{"key": "IPMART_IP_CHECK_URL", "default": "https://api.ipify.org?format=json"},
```

Add concise Chinese `help` strings explaining where each value comes from, but never include an actual username, password, or template copied from the operator. Do not add a connection-test button.

- [ ] **Step 4: Run schema and complete unit suite**

Run:

```powershell
python -m unittest discover -s tests -p "test_account_proxy.py" -v
python -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 5: Update README and CHANGELOG**

README must document:

- How to copy the fixed gateway credentials from the IPMart console.
- How to replace only the SID digits with `{sid}` in the username template.
- Two normal check requests per account and the 5-30 minute sticky caveat.
- One SID shared by Outlook, OAuth/Graph, mailbox reads, and Claude.
- No Clash requirement for default Outlook-to-Claude when IPMart is enabled.
- ChatGPT/Grok remain outside this migration.
- Old access-key settings are unsupported.
- Dry-run and real one-account commands.

Add a `2026-07-18` CHANGELOG entry describing the SID migration and corrected network boundary. Do not rewrite the historical access-key entry; mark the newer entry as superseding it.

Use this README example with placeholders only:

```dotenv
IPMART_ENABLED=1
IPMART_PROXY_HOST=your-fixed-gateway-host
IPMART_PROXY_PORT=your-fixed-gateway-port
IPMART_PROXY_USERNAME_TEMPLATE=your-console-username-with-sid-{sid}
IPMART_PROXY_PASSWORD=your-proxy-password
IPMART_MAX_ATTEMPTS=3
IPMART_IP_CHECK_URL=https://api.ipify.org?format=json
```

State explicitly that `{sid}` replaces only the SID digits shown by the console; it is not appended to an unrelated username. The documented dry-run command must be `python run_full_flow.py --platforms claude --dry-run`; it creates no SID and performs no IP check. The real one-account command must be labeled as consuming proxy traffic and creating external accounts.

- [ ] **Step 6: Run final static and behavioral verification**

Run:

```powershell
python -m unittest discover -s tests -v
python -m py_compile common/ipmart_proxy.py common/account_proxy.py common/mailbox.py extract_graph_tokens.py outlook_reg_loop.py run_full_flow.py register.py register_three_platforms.py config.py webui/scripts.py
$env:IPMART_ENABLED='1'
$env:IPMART_PROXY_HOST='gateway.example'
$env:IPMART_PROXY_PORT='8080'
$env:IPMART_PROXY_USERNAME_TEMPLATE='account-res-US-sid-{sid}'
$env:IPMART_PROXY_PASSWORD='dry-run-placeholder'
python run_full_flow.py --platforms claude --dry-run
rg -n "IPMART_ACCESS_KEY|IPMART_API_BASE|IPMART_COUNTRY|IPMART_STICKY_MINUTES|getIps" .env.example config.py webui/scripts.py README.md common tests
git diff --check
git status --short
```

Expected: all tests pass, compilation exits zero, dry-run prints Stage A/B commands without contacting IPMart, the `rg` command returns exit code 1 with no matches, diff check is clean, and status lists only intended task files.

- [ ] **Step 7: Commit configuration and documentation**

```powershell
git add .env.example config.py webui/scripts.py tests/test_account_proxy.py README.md CHANGELOG.md
git commit -m "docs: configure IPMart SID proxy mode"
```

- [ ] **Step 8: Do not run a real smoke test without approval**

Record in the handoff that no IPMart credentials, proxy traffic, Outlook account, or Claude account were consumed by automated verification.

---

## Completion Checklist

- [ ] Every new production behavior was preceded by a failing test.
- [ ] Normal completed rounds issue two dedicated IP-check requests.
- [ ] Proxy username/password never appear in logs, exceptions, reprs, commands, or the ledger.
- [ ] Outlook BitBrowser, OAuth token extraction, Graph mailbox reads, and Claude BitBrowser receive one lease.
- [ ] IPMart-enabled Outlook/Claude paths do not use Clash or inherited HTTP proxy variables.
- [ ] Disabled mode and ChatGPT/Grok proxy behavior remain unchanged.
- [ ] Full unit suite, compile check, dry-run, and `git diff --check` pass.
