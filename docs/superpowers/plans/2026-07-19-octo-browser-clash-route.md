# Octo Browser And Clash Route Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add Octo Browser as a fingerprint-browser provider and select direct networking automatically when IPMart is disabled and the configured Clash proxy port is unavailable.

**Architecture:** Keep the existing BitBrowser-compatible browser contract and add an Octo adapter that translates profile lifecycle calls to Octo Public and Local APIs. Centralize Clash endpoint probing and proxy-environment cleanup in a focused helper, then call it from existing entry points without changing the established IPMart coverage.

**Tech Stack:** Python 3.10+, requests, socket, urllib.parse, unittest/unittest.mock, FastAPI WebUI metadata, Playwright over CDP.

## Global Constraints

- Preserve BitBrowser and AdsPower behavior and keep bitbrowser as the default provider.
- Preserve IPMart coverage exactly: Outlook, Graph token extraction, Graph mailbox access, and Claude share the lease; ChatGPT and Grok do not.
- An enabled IPMart flow remains fail-closed and never falls back to Clash, direct access, or an existing profile.
- When IPMart is disabled, probe Clash before work begins; a missing, invalid, refused, or timed-out endpoint means direct mode.
- Do not switch routes in the middle of an account attempt.
- Do not log OCTO_API_TOKEN, IPMart usernames, or IPMart passwords.
- Octo Local API defaults to http://127.0.0.1:58888 and Public API defaults to https://app.octobrowser.net.
- Do not use Octo one-time profiles in this implementation.

---

## File Structure

- Create common/network_route.py: pure Clash endpoint parsing, reachability probing, and environment application.
- Create octobrowser.py: Octo Public/Local API adapter implementing the existing browser contract.
- Create tests/test_network_route.py: unit tests for route resolution and environment mutation.
- Create tests/test_octobrowser.py: isolated API mapping, proxy mapping, normalization, and redaction tests.
- Create tests/test_octo_provider_integration.py: factory and direct BitBrowser-bypass integration tests.
- Modify run_full_flow.py, outlook_reg_loop.py, register_outlook_standalone.py, register.py, register_three_platforms.py, register_grok_http.py, and webui/server.py: apply the preflight route decision.
- Modify bitbrowser.py, config.py, outlook_reg_loop.py, register_outlook_standalone.py, unlock_outlook.py, webui/scripts.py, webui/server.py, webui/static/app.js, .env.example, README.md, and CHANGELOG.md: expose and wire Octo.

---

### Task 1: Central Clash-Or-Direct Route Resolver

**Files:**
- Create: common/network_route.py
- Create: tests/test_network_route.py

**Interfaces:**
- Consumes: common.account_proxy.strip_http_proxy_env(env).
- Produces: NetworkRoute, resolve_clash_route(env, connector, timeout), apply_clash_route(env, route), prepare_clash_or_direct(env, connector, timeout).

- [ ] **Step 1: Write failing route-resolution tests**

Create tests/test_network_route.py:

~~~~python
import unittest

from common import network_route


class FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class NetworkRouteTests(unittest.TestCase):
    def test_reachable_clash_endpoint_is_selected(self):
        calls = []
        sock = FakeSocket()

        def connector(address, timeout):
            calls.append((address, timeout))
            return sock

        env = {"CLASH_PROXY": "http://127.0.0.1:7897"}
        route = network_route.resolve_clash_route(
            env, connector=connector, timeout=0.25
        )

        self.assertEqual(route.mode, "clash")
        self.assertEqual(route.proxy_url, "http://127.0.0.1:7897")
        self.assertEqual(calls, [(("127.0.0.1", 7897), 0.25)])
        self.assertTrue(sock.closed)

    def test_unreachable_clash_endpoint_selects_direct(self):
        def connector(_address, _timeout):
            raise ConnectionRefusedError("refused")

        route = network_route.resolve_clash_route(
            {"CLASH_PROXY": "http://127.0.0.1:7897"},
            connector=connector,
        )

        self.assertEqual(route.mode, "direct")
        self.assertEqual(route.reason, "unreachable")

    def test_missing_and_invalid_endpoints_select_direct(self):
        for env in (
            {},
            {"CLASH_PROXY": ""},
            {"CLASH_PROXY": "not a proxy"},
            {"CLASH_PROXY": "http://127.0.0.1:0"},
        ):
            with self.subTest(env=env):
                route = network_route.resolve_clash_route(env)
                self.assertEqual(route.mode, "direct")

    def test_direct_route_removes_all_inherited_proxy_variables(self):
        env = {
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://stale",
            "HTTPS_PROXY": "http://stale",
            "http_proxy": "http://stale",
            "https_proxy": "http://stale",
        }

        network_route.apply_clash_route(
            env, network_route.NetworkRoute("direct", reason="unreachable")
        )

        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertNotIn(key, env)
        self.assertEqual(env["CLASH_PROXY"], "http://127.0.0.1:7897")

    def test_clash_route_exports_proxy_and_localhost_bypass(self):
        env = {"NO_PROXY": "example.test"}
        network_route.apply_clash_route(
            env,
            network_route.NetworkRoute(
                "clash", "socks5://127.0.0.1:7897", "reachable"
            ),
        )

        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(env[key], "socks5://127.0.0.1:7897")
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertIn(host, env["NO_PROXY"].split(","))
        self.assertEqual(env["NO_PROXY"], env["no_proxy"])


if __name__ == "__main__":
    unittest.main()
~~~~

- [ ] **Step 2: Run the tests and verify RED**

Run:

~~~~powershell
python -m unittest tests.test_network_route -v
~~~~

Expected: ERROR with ImportError because common.network_route does not exist.

- [ ] **Step 3: Implement the minimal route helper**

Create common/network_route.py:

~~~~python
from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
import os
import socket
from typing import Callable, Literal
from urllib.parse import urlparse

from common.account_proxy import strip_http_proxy_env


@dataclass(frozen=True)
class NetworkRoute:
    mode: Literal["clash", "direct"]
    proxy_url: str = ""
    reason: str = ""


def _proxy_endpoint(raw: str) -> tuple[str, int] | None:
    value = (raw or "").strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else "http://" + value)
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https", "socks4", "socks5"}:
        return None
    if not parsed.hostname or port is None or not 1 <= port <= 65535:
        return None
    return parsed.hostname, port


def resolve_clash_route(
    env=None,
    *,
    connector: Callable | None = None,
    timeout: float = 0.5,
) -> NetworkRoute:
    env = os.environ if env is None else env
    connector = connector or socket.create_connection
    proxy_url = (env.get("CLASH_PROXY") or "").strip()
    endpoint = _proxy_endpoint(proxy_url)
    if not proxy_url:
        return NetworkRoute("direct", reason="not_configured")
    if endpoint is None:
        return NetworkRoute("direct", reason="invalid")
    sock = None
    try:
        sock = connector(endpoint, timeout)
    except OSError:
        return NetworkRoute("direct", reason="unreachable")
    finally:
        if sock is not None:
            sock.close()
    return NetworkRoute("clash", proxy_url=proxy_url, reason="reachable")


def apply_clash_route(
    env: MutableMapping[str, str], route: NetworkRoute
) -> MutableMapping[str, str]:
    if route.mode == "direct":
        return strip_http_proxy_env(env)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[key] = route.proxy_url
    existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
    parts = [part.strip() for part in existing.split(",") if part.strip()]
    for host in ("127.0.0.1", "localhost", "::1"):
        if host not in parts:
            parts.append(host)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(parts)
    return env


def prepare_clash_or_direct(
    env=None,
    *,
    connector: Callable | None = None,
    timeout: float = 0.5,
) -> NetworkRoute:
    env = os.environ if env is None else env
    route = resolve_clash_route(env, connector=connector, timeout=timeout)
    apply_clash_route(env, route)
    return route
~~~~

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

~~~~powershell
python -m unittest tests.test_network_route -v
~~~~

Expected: 5 tests pass.

- [ ] **Step 5: Run existing proxy helper tests**

Run:

~~~~powershell
python -m unittest tests.test_account_proxy tests.test_ipmart_proxy -v
~~~~

Expected: all existing tests pass.

- [ ] **Step 6: Commit Task 1**

~~~~powershell
git add common/network_route.py tests/test_network_route.py
git commit -m "feat: select Clash or direct route"
~~~~

---

### Task 2: Apply Route Resolution To All Non-IPMart Entry Points

**Files:**
- Modify: run_full_flow.py:70-160
- Modify: outlook_reg_loop.py:85-125
- Modify: register_outlook_standalone.py:55-80, 2115-2135
- Modify: register.py:135-185, 4260-4290
- Modify: register_three_platforms.py:260-285
- Modify: register_grok_http.py:70-85, 340-390
- Modify: webui/server.py:45-60, 800-815
- Modify: tests/test_full_flow_ipmart_proxy.py
- Modify: tests/test_outlook_ipmart_proxy.py
- Modify: tests/test_claude_ipmart_proxy.py
- Modify: tests/test_platform_proxy_env.py
- Create: tests/test_network_route_integration.py

**Interfaces:**
- Consumes: common.network_route.prepare_clash_or_direct and NetworkRoute.
- Produces: every IPMart-disabled entry point starts with either a verified Clash proxy environment or no HTTP proxy environment.

- [ ] **Step 1: Write failing integration tests**

Create tests/test_network_route_integration.py:

~~~~python
import argparse
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import outlook_reg_loop
import register
import register_three_platforms
import run_full_flow
from common.network_route import NetworkRoute


class NetworkRouteIntegrationTests(unittest.TestCase):
    def test_full_flow_strips_dead_clash_when_ipmart_is_disabled(self):
        args = argparse.Namespace(
            proxy="http://127.0.0.1:7897",
            clash_api="http://127.0.0.1:9097",
            clash_secret="",
            clash_group="GLOBAL",
        )
        base = {
            "IPMART_ENABLED": "0",
            "CLASH_PROXY": "http://127.0.0.1:7897",
        }
        with patch.object(run_full_flow.os, "environ", base), patch(
            "common.network_route.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ):
            env = run_full_flow.build_child_env(args)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertNotIn(key, env)

    def test_outlook_uses_direct_when_clash_is_unreachable(self):
        env = {
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://127.0.0.1:7897",
        }
        with patch(
            "common.network_route.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ):
            selected = outlook_reg_loop.prepare_outlook_network(env)
        self.assertEqual(selected, "")
        self.assertNotIn("HTTP_PROXY", env)

    def test_claude_skips_node_selection_in_direct_mode(self):
        with patch.object(register, "_pick_claude_node") as pick:
            register.configure_claude_proxy(
                "auto",
                account_lease=None,
                ipmart_enabled=False,
                clash_available=False,
            )
        pick.assert_not_called()
        self.assertIsNone(register.CLAUDE_PROXY_NODE)

    def test_platform_child_env_does_not_rebuild_dead_clash(self):
        env = {
            "IPMART_ENABLED": "0",
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://127.0.0.1:7897",
        }
        with patch(
            "common.network_route.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ):
            child = register_three_platforms.platform_child_env(
                "grok", env, ["grok"]
            )
        self.assertNotIn("HTTP_PROXY", child)

    def test_ipmart_branch_does_not_probe_or_change_coverage(self):
        env = {"IPMART_ENABLED": "1", "HTTP_PROXY": "http://legacy"}
        lease = SimpleNamespace(host="gateway", port=8000, exit_ip="203.0.113.8")
        with patch("common.network_route.resolve_clash_route") as resolve:
            register.prepare_claude_network(
                env, account_lease=lease, ipmart_enabled=True
            )
        resolve.assert_not_called()
        self.assertNotIn("HTTP_PROXY", env)


if __name__ == "__main__":
    unittest.main()
~~~~

- [ ] **Step 2: Run the integration test and verify RED**

Run:

~~~~powershell
python -m unittest tests.test_network_route_integration -v
~~~~

Expected: failures show dead Clash is still injected and configure_claude_proxy does not accept clash_available.

- [ ] **Step 3: Wire run_full_flow startup**

Add these imports:

~~~~python
# run_full_flow.py
from common.network_route import prepare_clash_or_direct

# outlook_reg_loop.py, register_outlook_standalone.py,
# register_three_platforms.py, register_grok_http.py
from common.network_route import prepare_clash_or_direct

# register.py
from common.network_route import NetworkRoute, prepare_clash_or_direct

# webui/server.py
from common.ipmart_proxy import settings_from_env
from common.network_route import prepare_clash_or_direct
~~~~

run_full_flow.py already imports settings_from_env; do not add a duplicate
import there. Place the webui/server.py common imports after ROOT has been
inserted into sys.path.

At the end of run_full_flow.build_child_env, after CLASH configuration is
present, apply:

~~~~python
    ipmart_enabled = settings_from_env(env).enabled
    if not ipmart_enabled:
        route = prepare_clash_or_direct(env)
        log(
            "network route: Clash"
            if route.mode == "clash"
            else f"network route: direct ({route.reason})"
        )
    return env
~~~~

Keep the IPMart-enabled branch unchanged so run_once can strip the proxy for Outlook/Claude and restore the original non-Claude route exactly as it does today.

- [ ] **Step 4: Replace Outlook dead-proxy injection**

In outlook_reg_loop.py, implement ensure_clash_proxy_env through the shared helper:

~~~~python
def ensure_clash_proxy_env(env=None):
    env = os.environ if env is None else env
    route = prepare_clash_or_direct(env)
    return route.proxy_url if route.mode == "clash" else ""
~~~~

Keep prepare_outlook_network fail-closed for lease/IPMart:

~~~~python
def prepare_outlook_network(env=None, *, lease=None, ipmart_enabled=False):
    env = os.environ if env is None else env
    if lease is not None or ipmart_enabled:
        strip_http_proxy_env(env)
        return ""
    return ensure_clash_proxy_env(env)
~~~~

In register_outlook_standalone.py replace ensure_clash_proxy_env with:

~~~~python
def ensure_clash_proxy_env():
    route = prepare_clash_or_direct(os.environ)
    return route.proxy_url if route.mode == "clash" else ""
~~~~

- [ ] **Step 5: Make Claude route selection explicit**

Change prepare_claude_network to return a NetworkRoute or None:

~~~~python
def prepare_claude_network(
    env=None, *, account_lease=None, ipmart_enabled=False
):
    env = os.environ if env is None else env
    if account_lease is not None or ipmart_enabled:
        strip_http_proxy_env(env)
        return None
    return prepare_clash_or_direct(env)
~~~~

Add clash_available to configure_claude_proxy:

~~~~python
def configure_claude_proxy(
    node_arg,
    account_lease=None,
    *,
    ipmart_enabled=False,
    clash_available=True,
):
    global CLAUDE_PROXY_NODE
    CLAUDE_PROXY_NODE = None
    if account_lease is not None:
        print(
            "  [proxy] IPMart account proxy "
            f"{account_lease.host}:{account_lease.port} "
            f"exit={account_lease.exit_ip}"
        )
        return
    if ipmart_enabled:
        print("  [proxy] IPMart enabled; skipping Clash node selection")
        return
    if not clash_available:
        print("  [proxy] Clash unavailable; using direct connection")
        return
    if not node_arg or node_arg.lower() == "none":
        return
    if proxy_switch is None:
        print(
            "  [proxy] proxy_switch unavailable; "
            "Claude may be region-blocked"
        )
        return
    try:
        if node_arg.lower() == "auto":
            print("  [proxy] probing Clash nodes for Claude...")
            node = _pick_claude_node()
            if not node:
                print(
                    "  [proxy] no working Claude node found; "
                    "continuing without a browser proxy"
                )
                return
            CLAUDE_PROXY_NODE = node
            _record_claude_node(node)
            print(f"  [proxy] selected node: {node}")
            return
        proxy_switch.set_node(node_arg)
        time.sleep(2)
        CLAUDE_PROXY_NODE = node_arg
        print(f"  [proxy] selected node: {proxy_switch.current_node()}")
    except Exception as exc:
        log_claude_flow_error(
            "[proxy] clash_node_selection_failed",
            exc,
            provider=EMAIL_PROVIDER,
        )
~~~~

In main, retain the returned route and pass clash_available:

~~~~python
    legacy_route = prepare_claude_network(
        os.environ,
        account_lease=inherited_lease,
        ipmart_enabled=ipmart_settings.enabled,
    )
    configure_claude_proxy(
        args.node,
        inherited_lease,
        ipmart_enabled=ipmart_settings.enabled,
        clash_available=(
            legacy_route is None or legacy_route.mode == "clash"
        ),
    )
~~~~

- [ ] **Step 6: Prevent child processes and Grok from reconstructing dead Clash**

Replace register_three_platforms.platform_child_env with:

~~~~python
def platform_child_env(platform, base_env, platforms=None):
    env = dict(base_env)
    if platform == "claude":
        if env.get("ACCOUNT_PROXY_SOURCE") == "ipmart":
            strip_http_proxy_env(env)
        else:
            prepare_clash_or_direct(env)
        if (
            platforms is not None
            and set(platforms) != {"claude"}
            and normalize_email_provider(env.get("EMAIL_PROVIDER"))
            == "NINEMALL"
        ):
            env["EMAIL_PROVIDER"] = "OUTLOOK"
        return env
    if (
        platform in {"chatgpt", "grok"}
        and env.get("ACCOUNT_PROXY_SOURCE") == "ipmart"
    ):
        strip_account_proxy_env(env)
        if not any(
            (env.get(key) or "").strip()
            for key in HTTP_PROXY_ENV_KEYS
        ):
            clash_proxy = (env.get("CLASH_PROXY") or "").strip()
            if clash_proxy:
                for key in HTTP_PROXY_ENV_KEYS:
                    env[key] = clash_proxy
        return env
    prepare_clash_or_direct(env)
    return env
~~~~

In register_grok_http.main, resolve the route before node selection and client construction:

~~~~python
    global CLASH_PROXY
    route = prepare_clash_or_direct(os.environ)
    CLASH_PROXY = route.proxy_url if route.mode == "clash" else None
    clash_available = route.mode == "clash"
    if not clash_available:
        print(
            "  [proxy] Clash unavailable; "
            "Grok HTTP is using direct connection"
        )
~~~~

Replace the node-selection try block with:

~~~~python
    if clash_available:
        try:
            if args.node and args.node.lower() not in {"auto", "none"}:
                target = _resolve_node(args.node)
                if target != args.node:
                    print(
                        f"  node resolved: '{args.node}' -> '{target}'"
                    )
                proxy_switch.set_node(target)
                time.sleep(2)
                print(
                    f"  selected node -> {proxy_switch.current_node()}"
                )
            elif args.node.lower() == "auto":
                print("  probing xAI-compatible Clash nodes...")
                node = _find_signup_node()
                if not node:
                    print("  no working Grok Clash node found")
                    return 1
                print(f"  selected node: {node}")
        except Exception as exc:
            print(f"  Clash node selection failed: {exc}")
            return 1
~~~~

Add clash_available to both rotation conditions in the account loop:

~~~~python
if (
    clash_available
    and i > 1
    and args.node.lower() == "auto"
    and args.rotate_every > 0
    and (i - 1) % args.rotate_every == 0
    and not last_attempt_failed
):
    rotated = _find_signup_node()

if (
    clash_available
    and last_attempt_failed
    and args.node.lower() == "auto"
    and i < args.count
):
    rotated = _find_signup_node()
~~~~

The existing register_one call already passes the module-level CLASH_PROXY to
XConsoleAuthClient; None therefore selects direct transport.

In webui/server.py replace _ensure_proxy_env and the final proxy block in
_child_env with:

~~~~python
def _ensure_proxy_env():
    prepare_clash_or_direct(os.environ)


def _child_env():
    env = dict(os.environ)
    for key, value in _parse_env_file(ENV_PATH).items():
        if key not in BOOT_ENV:
            env[key] = value
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if not settings_from_env(env).enabled:
        prepare_clash_or_direct(env)
    return env
~~~~

- [ ] **Step 7: Update existing assertions for the new direct behavior**

Change the old disabled-IPMart preservation assertion in tests/test_claude_ipmart_proxy.py:

~~~~python
    def test_disabled_ipmart_without_clash_listener_strips_http_proxy(self):
        env = {
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://127.0.0.1:7897",
        }
        with patch(
            "common.network_route.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ):
            route = register.prepare_claude_network(
                env, account_lease=None, ipmart_enabled=False
            )
        self.assertEqual(route.mode, "direct")
        self.assertNotIn("HTTP_PROXY", env)
~~~~

Change tests/test_platform_proxy_env.py so its no-lease cases mock a reachable connector when expecting proxy preservation and a refused connector when expecting direct cleanup. Do not change any IPMart lease assertions.

- [ ] **Step 8: Run route and proxy suites**

Run:

~~~~powershell
python -m unittest tests.test_network_route tests.test_network_route_integration tests.test_full_flow_ipmart_proxy tests.test_outlook_ipmart_proxy tests.test_claude_ipmart_proxy tests.test_platform_proxy_env -v
~~~~

Expected: all tests pass.

- [ ] **Step 9: Commit Task 2**

~~~~powershell
git add run_full_flow.py outlook_reg_loop.py register_outlook_standalone.py register.py register_three_platforms.py register_grok_http.py webui/server.py tests/test_network_route_integration.py tests/test_full_flow_ipmart_proxy.py tests/test_outlook_ipmart_proxy.py tests/test_claude_ipmart_proxy.py tests/test_platform_proxy_env.py
git commit -m "feat: use direct route when Clash is unavailable"
~~~~

---

### Task 3: Octo Browser API Adapter

**Files:**
- Create: octobrowser.py
- Create: tests/test_octobrowser.py

**Interfaces:**
- Consumes: OCTO_API_TOKEN, OCTO_PUBLIC_API, OCTO_LOCAL_API and existing BitBrowser-shaped profile keyword arguments.
- Produces: OctoBrowser with create_browser, update_browser, open_browser, close_browser, delete_browser, list_browsers, cleanup_browsers, and _post.

- [ ] **Step 1: Write failing adapter tests**

Create tests/test_octobrowser.py with a small fake session:

~~~~python
import unittest

from octobrowser import OctoBrowser


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.trust_env = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


class OctoBrowserTests(unittest.TestCase):
    def make_browser(self, responses, token="token-value"):
        session = FakeSession(responses)
        browser = OctoBrowser(
            public_api="https://app.octobrowser.net",
            local_api="http://127.0.0.1:58888",
            api_token=token,
            session=session,
        )
        return browser, session

    def test_create_direct_profile_omits_proxy(self):
        browser, session = self.make_browser([
            FakeResponse({
                "success": True,
                "data": {"uuid": "profile-1"},
                "msg": "",
            }, 201)
        ])
        profile_id = browser.create_browser(
            name="direct", proxyType="noproxy"
        )
        self.assertEqual(profile_id, "profile-1")
        body = session.calls[0][2]["json"]
        self.assertEqual(body["title"], "direct")
        self.assertEqual(body["fingerprint"]["os"], "win")
        self.assertNotIn("proxy", body)

    def test_create_maps_ipmart_proxy(self):
        browser, session = self.make_browser([
            FakeResponse({
                "success": True,
                "data": {"uuid": "profile-2"},
                "msg": "",
            }, 201)
        ])
        browser.create_browser(
            name="leased",
            proxyType="http",
            host="gateway.example",
            port="8080",
            proxyUserName="account-sid-00000042",
            proxyPassword="secret",
        )
        self.assertEqual(session.calls[0][2]["json"]["proxy"], {
            "type": "http",
            "host": "gateway.example",
            "port": 8080,
            "login": "account-sid-00000042",
            "password": "secret",
        })

    def test_start_normalizes_ws_endpoint(self):
        browser, session = self.make_browser([
            FakeResponse({
                "uuid": "profile-1",
                "ws_endpoint": "ws://127.0.0.1:55000/devtools/browser/id",
                "debug_port": "55000",
            })
        ])
        result = browser.open_browser("profile-1")
        self.assertEqual(
            result["ws"], "ws://127.0.0.1:55000/devtools/browser/id"
        )
        self.assertEqual(
            session.calls[0][1],
            "http://127.0.0.1:58888/api/profiles/start",
        )

    def test_list_and_delete_use_public_api(self):
        browser, session = self.make_browser([
            FakeResponse({
                "success": True,
                "data": [{"uuid": "p1", "title": "one"}],
                "total_count": 1,
            }),
            FakeResponse({
                "success": True,
                "data": {"deleted_uuids": ["p1"]},
            }),
        ])
        listed = browser.list_browsers()
        self.assertEqual(listed["data"]["list"][0]["id"], "p1")
        browser.delete_browser("p1")
        self.assertEqual(session.calls[1][2]["json"]["uuids"], ["p1"])

    def test_stop_uses_local_api(self):
        browser, session = self.make_browser([
            FakeResponse({"msg": "Profile stopped"})
        ])
        browser.close_browser("p1")
        self.assertEqual(
            session.calls[0][1],
            "http://127.0.0.1:58888/api/profiles/stop",
        )
        self.assertEqual(session.calls[0][2]["json"], {"uuid": "p1"})

    def test_cleanup_honors_keep_count(self):
        browser, session = self.make_browser([
            FakeResponse({
                "success": True,
                "data": [
                    {"uuid": "old", "title": "old"},
                    {"uuid": "new", "title": "new"},
                ],
                "total_count": 2,
            }),
            FakeResponse({"msg": "Profile stopped"}),
            FakeResponse({
                "success": True,
                "data": {"deleted_uuids": ["old"]},
            }),
        ])
        deleted = browser.cleanup_browsers(keep=1)
        self.assertEqual(deleted, 1)
        self.assertEqual(session.calls[1][2]["json"], {"uuid": "old"})

    def test_legacy_update_patches_existing_profile(self):
        browser, session = self.make_browser([
            FakeResponse({
                "success": True,
                "data": {"uuid": "existing"},
            })
        ])
        result = browser._post(
            "/browser/update",
            {
                "id": "existing",
                "name": "legacy",
                "proxyType": "http",
                "host": "127.0.0.1",
                "port": "7897",
            },
        )
        self.assertEqual(result, {
            "success": True,
            "data": {"id": "existing", "browserId": "existing"},
        })
        self.assertEqual(session.calls[0][0], "PATCH")
        self.assertTrue(session.calls[0][1].endswith(
            "/api/v2/automation/profiles/existing"
        ))

    def test_missing_token_fails_before_public_request(self):
        browser, session = self.make_browser([], token="")
        with self.assertRaisesRegex(RuntimeError, "OCTO_API_TOKEN"):
            browser.create_browser("missing-token")
        self.assertEqual(session.calls, [])

    def test_errors_redact_token_and_proxy_credentials(self):
        browser, _session = self.make_browser([
            FakeResponse({
                "success": False,
                "msg": (
                    "token-value account-sid-00000042 secret"
                ),
            }, 400)
        ])
        with self.assertRaises(RuntimeError) as caught:
            browser.create_browser(
                "leased",
                proxyType="http",
                host="gateway.example",
                port="8080",
                proxyUserName="account-sid-00000042",
                proxyPassword="secret",
            )
        rendered = str(caught.exception)
        for secret in ("token-value", "account-sid-00000042", "secret"):
            self.assertNotIn(secret, rendered)

    def test_local_api_error_includes_configured_base_url(self):
        browser, _session = self.make_browser([
            FakeResponse({"error": "client unavailable"}, 503)
        ])
        with self.assertRaises(RuntimeError) as caught:
            browser.open_browser("p1")
        self.assertIn("http://127.0.0.1:58888", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
~~~~

- [ ] **Step 2: Run adapter tests and verify RED**

Run:

~~~~powershell
python -m unittest tests.test_octobrowser -v
~~~~

Expected: ERROR because octobrowser.py does not exist.

- [ ] **Step 3: Implement OctoBrowser requests and profile translation**

Create octobrowser.py with these imports, configuration fallbacks, constructor,
request helper, redaction, and proxy translation:

~~~~python
import os
import re
import time

import requests

try:
    from config import (
        OCTO_API_TOKEN,
        OCTO_LOCAL_API,
        OCTO_PUBLIC_API,
    )
except Exception:
    OCTO_API_TOKEN = os.environ.get("OCTO_API_TOKEN", "")
    OCTO_PUBLIC_API = os.environ.get(
        "OCTO_PUBLIC_API", "https://app.octobrowser.net"
    )
    OCTO_LOCAL_API = os.environ.get(
        "OCTO_LOCAL_API", "http://127.0.0.1:58888"
    )


class OctoBrowser:
    provider_name = "octo"

    def __init__(
        self,
        *,
        public_api=None,
        local_api=None,
        api_token=None,
        session=None,
    ):
        self.public_api = (
            public_api or OCTO_PUBLIC_API or "https://app.octobrowser.net"
        ).rstrip("/")
        self.local_api = (
            local_api or OCTO_LOCAL_API or "http://127.0.0.1:58888"
        ).rstrip("/")
        self.api_token = (
            OCTO_API_TOKEN if api_token is None else api_token
        )
        self.session = session or requests.Session()
        self.session.trust_env = False

    @staticmethod
    def _redact(message, secrets=()):
        rendered = str(message)
        for secret in secrets:
            if secret:
                rendered = rendered.replace(str(secret), "[redacted]")
        return rendered

    def _request(
        self,
        method,
        url,
        *,
        public=False,
        params=None,
        json_body=None,
        timeout=120,
        retries=5,
        secrets=(),
    ):
        if public and not self.api_token:
            raise RuntimeError(
                "OCTO_API_TOKEN is required for Octo Public API"
            )
        headers = {"Content-Type": "application/json"}
        if public:
            headers["X-Octo-Api-Token"] = self.api_token
        protected = tuple(secrets) + (self.api_token,)
        for attempt in range(retries):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                if attempt + 1 < retries:
                    time.sleep(2 + attempt)
                    continue
                raise RuntimeError(
                    self._redact(
                        f"Octo transport error at {url}: {exc}",
                        protected,
                    )
                ) from None
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            failed = (
                response.status_code >= 400
                or (
                    isinstance(payload, dict)
                    and payload.get("success") is False
                )
            )
            if failed:
                detail = (
                    payload.get("msg")
                    or payload.get("error")
                    or f"HTTP {response.status_code}"
                )
                raise RuntimeError(
                    self._redact(
                        f"Octo API error at {url}: {detail}",
                        protected,
                    )
                )
            return payload
        raise RuntimeError("Octo request retry loop exhausted")

    @staticmethod
    def _parse_proxy(proxy_str):
        if not proxy_str:
            return None
        value = str(proxy_str).strip()
        proxy_type = "http"
        for prefix in ("socks5://", "socks4://", "http://", "https://"):
            if value.lower().startswith(prefix):
                proxy_type = prefix.split("://", 1)[0]
                value = value[len(prefix):]
                break
        value = (
            value.replace(",", "@", 1)
            if "@" not in value and "," in value
            else value
        )
        match = re.match(r"^(.+):(.+)@(.+):(\d+)$", value)
        if match:
            return {
                "type": proxy_type,
                "login": match.group(1),
                "password": match.group(2),
                "host": match.group(3),
                "port": int(match.group(4)),
            }
        match = re.match(r"^(.+):(\d+)$", value)
        if match:
            return {
                "type": proxy_type,
                "host": match.group(1),
                "port": int(match.group(2)),
                "login": "",
                "password": "",
            }
        return None

    def _proxy_payload(self, data):
        proxy_type = str(
            data.get("proxyType") or data.get("proxy_type") or "noproxy"
        ).lower()
        if proxy_type in {"noproxy", "no_proxy", "none", "direct"}:
            return None
        host = data.get("host") or data.get("proxyHost")
        raw_port = data.get("port") or data.get("proxyPort")
        if not host or not str(raw_port).isdigit():
            return None
        port = int(raw_port)
        if not 1 <= port <= 65535:
            return None
        return {
            "type": proxy_type,
            "host": str(host),
            "port": port,
            "login": (
                data.get("proxyUserName") or data.get("proxy_user") or ""
            ),
            "password": (
                data.get("proxyPassword")
                or data.get("proxy_password")
                or ""
            ),
        }
~~~~

Only requests.RequestException transport failures are retried; HTTP/API
business failures fail immediately. Public requests send:

~~~~python
headers = {
    "Content-Type": "application/json",
    "X-Octo-Api-Token": self.api_token,
}
~~~~

Add profile payload construction and create_browser:

~~~~python
    def _profile_payload(self, name, data):
        payload = {
            "title": name,
            "fingerprint": {"os": "win"},
        }
        remark = data.get("remark")
        if remark:
            payload["description"] = remark
        proxy = self._proxy_payload(data)
        if proxy is not None:
            payload["proxy"] = proxy
        fingerprint = data.get("browserFingerPrint") or {}
        if fingerprint.get("isIpCreateLanguage"):
            payload["fingerprint"]["languages"] = {"type": "ip"}
        if fingerprint.get("isIpCreateTimeZone"):
            payload["fingerprint"]["timezone"] = {"type": "ip"}
        if fingerprint.get("isIpCreatePosition"):
            payload["fingerprint"]["geolocation"] = {"type": "ip"}
        if proxy is not None:
            payload["fingerprint"]["webrtc"] = {"type": "ip"}
        return payload

    def create_browser(self, name="claude_register", proxy_str=None, **kwargs):
        data = dict(kwargs)
        if proxy_str:
            parsed = self._parse_proxy(proxy_str)
            if parsed:
                data.update({
                    "proxyType": parsed["type"],
                    "host": parsed["host"],
                    "port": parsed["port"],
                    "proxyUserName": parsed.get("login", ""),
                    "proxyPassword": parsed.get("password", ""),
                })
        payload = self._profile_payload(name, data)
        proxy = payload.get("proxy") or {}
        result = self._request(
            "POST",
            self.public_api + "/api/v2/automation/profiles",
            public=True,
            json_body=payload,
            secrets=(proxy.get("login"), proxy.get("password")),
        )
        response_data = result.get("data") or {}
        profile_id = response_data.get("uuid")
        if not profile_id:
            raise RuntimeError("Octo create returned no profile UUID")
        return str(profile_id)

    def update_browser(self, profile_id, name=None, **kwargs):
        payload = self._profile_payload(
            name or kwargs.pop("title", "reg_factory"),
            kwargs,
        )
        proxy = payload.get("proxy") or {}
        self._request(
            "PATCH",
            (
                self.public_api
                + "/api/v2/automation/profiles/"
                + str(profile_id)
            ),
            public=True,
            json_body=payload,
            secrets=(proxy.get("login"), proxy.get("password")),
        )
        return {"id": str(profile_id)}
~~~~

This code returns no proxy for noproxy/no_proxy/none/direct and maps a valid
proxy to the exact type/host/integer port/login/password fields asserted by the
tests.

- [ ] **Step 4: Implement lifecycle normalization and compatibility**

Implement:

~~~~python
def open_browser(self, profile_id):
    data = self._request(
        "POST",
        self.local_api + "/api/profiles/start",
        json_body={
            "uuid": str(profile_id),
            "headless": False,
            "debug_port": True,
            "only_local": True,
            "flags": [],
            "timeout": 120,
            "password": "",
        },
    )
    ws = data.get("ws_endpoint") or ""
    if not ws:
        raise RuntimeError("Octo start returned no CDP endpoint")
    return {
        "ws": ws,
        "http": (
            f"http://127.0.0.1:{data.get('debug_port')}"
            if data.get("debug_port") else ""
        ),
        "debug_port": data.get("debug_port"),
        "raw": data,
    }


def close_browser(self, profile_id):
    return self._request(
        "POST",
        self.local_api + "/api/profiles/stop",
        json_body={"uuid": str(profile_id)},
    )


def delete_browser(self, profile_id):
    return self._request(
        "DELETE",
        self.public_api + "/api/v2/automation/profiles",
        public=True,
        json_body={"uuids": [str(profile_id)], "skip_trash_bin": True},
    )
~~~~

Add list, cleanup, and legacy compatibility:

~~~~python
    def list_browsers(self, page=0, page_size=100):
        result = self._request(
            "GET",
            self.public_api + "/api/v2/automation/profiles",
            public=True,
            params={
                "page_len": int(page_size),
                "page": int(page),
                "fields": "title,status",
            },
            timeout=30,
        )
        raw_items = result.get("data") or []
        items = []
        for index, item in enumerate(raw_items):
            mapped = dict(item)
            mapped["id"] = str(item.get("uuid") or "")
            mapped.setdefault("name", item.get("title") or "")
            mapped.setdefault("seq", index)
            items.append(mapped)
        total = result.get("total_count", len(items))
        return {
            "success": True,
            "data": {"list": items, "totalNum": total},
        }

    def cleanup_browsers(self, keep=0):
        browsers = self.list_browsers(
            page=0, page_size=200
        )["data"]["list"]
        browsers.sort(
            key=lambda item: item.get("seq", 0) or 0,
            reverse=True,
        )
        deleted = 0
        for item in browsers[int(keep):]:
            profile_id = item.get("id")
            if not profile_id:
                continue
            try:
                self.close_browser(profile_id)
            except Exception:
                pass
            try:
                self.delete_browser(profile_id)
                deleted += 1
            except Exception:
                pass
        return deleted

    def _post(self, path, data=None, _retries=5):
        data = data or {}
        if path == "/browser/list":
            return self.list_browsers(
                page=int(data.get("page", 0) or 0),
                page_size=int(data.get("pageSize", 100) or 100),
            )
        profile_id = data.get("id") or data.get("browserId")
        if path == "/browser/open":
            return {
                "success": True,
                "data": self.open_browser(profile_id),
            }
        if path == "/browser/close":
            return {
                "success": True,
                "data": self.close_browser(profile_id),
            }
        if path == "/browser/delete":
            return {
                "success": True,
                "data": self.delete_browser(profile_id),
            }
        if path == "/browser/update":
            body = dict(data)
            name = body.pop("name", "reg_factory")
            existing = (
                body.pop("id", None)
                or body.pop("browserId", None)
                or body.pop("user_id", None)
            )
            if existing:
                self.update_browser(existing, name=name, **body)
                return {
                    "success": True,
                    "data": {
                        "id": str(existing),
                        "browserId": str(existing),
                    },
                }
            created = self.create_browser(name=name, **body)
            return {
                "success": True,
                "data": {"id": created, "browserId": created},
            }
        raise NotImplementedError(
            f"Octo compatibility endpoint not supported: {path}"
        )
~~~~

- [ ] **Step 5: Run adapter tests and verify GREEN**

Run:

~~~~powershell
python -m unittest tests.test_octobrowser -v
~~~~

Expected: 10 tests pass.

- [ ] **Step 6: Commit Task 3**

~~~~powershell
git add octobrowser.py tests/test_octobrowser.py
git commit -m "feat: add Octo Browser API adapter"
~~~~

---

### Task 4: Wire Octo Through Factories, Legacy Entrypoints, And WebUI

**Files:**
- Modify: config.py:44-55
- Modify: bitbrowser.py:16-45
- Modify: outlook_reg_loop.py:371-450
- Modify: register_outlook_standalone.py:82-150
- Modify: unlock_outlook.py:60-80
- Modify: webui/scripts.py:325-335
- Modify: webui/server.py:331-365, 690-710
- Modify: webui/static/app.js:30-38
- Create: tests/test_octo_provider_integration.py
- Modify: tests/test_webui_env_reload.py

**Interfaces:**
- Consumes: OctoBrowser from Task 3.
- Produces: FINGERPRINT_BROWSER=octo works from common and legacy browser paths; WebUI exposes and reports Octo.

- [ ] **Step 1: Write failing provider and WebUI tests**

Create tests/test_octo_provider_integration.py:

~~~~python
import os
import unittest
from unittest.mock import patch

import bitbrowser
import outlook_reg_loop
import register_outlook_standalone
import unlock_outlook
from octobrowser import OctoBrowser
from webui import scripts, server


class OctoProviderIntegrationTests(unittest.TestCase):
    def test_factory_selects_octo(self):
        with patch.dict(os.environ, {"FINGERPRINT_BROWSER": "octo"}):
            browser = bitbrowser.BitBrowser()
        self.assertIsInstance(browser, OctoBrowser)

    def test_outlook_loop_uses_provider_adapter_for_octo(self):
        with patch.object(
            outlook_reg_loop, "_fingerprint_provider", return_value="octo"
        ), patch("bitbrowser.BitBrowser") as factory:
            factory.return_value.create_browser.return_value = "p1"
            result = outlook_reg_loop._bb_create_for_outlook_reg("outlook")
        self.assertEqual(result, "p1")
        factory.return_value.create_browser.assert_called_once()

    def test_standalone_factory_selects_shared_provider_for_octo(self):
        with patch.object(
            register_outlook_standalone,
            "_fingerprint_provider",
            return_value="octo",
        ), patch("bitbrowser.BitBrowser") as factory:
            client = register_outlook_standalone.BitBrowserClient()
        self.assertIs(client, factory.return_value)

    def test_webui_metadata_exposes_octo_secrets_and_urls(self):
        group = next(
            group for group in scripts.CONFIG_GROUPS
            if any(
                item.get("key") == "FINGERPRINT_BROWSER"
                for item in group["items"]
            )
        )
        items = {item["key"]: item for item in group["items"]}
        self.assertIn("octo", items["FINGERPRINT_BROWSER"]["choices"])
        self.assertTrue(items["OCTO_API_TOKEN"]["secret"])
        self.assertEqual(
            items["OCTO_LOCAL_API"]["default"],
            "http://127.0.0.1:58888",
        )

    def test_status_uses_octo_local_api(self):
        with patch.object(server, "_fingerprint_provider", return_value="octo"), patch.object(
            server, "_read_config_val", return_value="http://127.0.0.1:58888"
        ), patch.object(server, "_direct_get", return_value=(200, "{}")) as get:
            ok, _message = server._test_bitbrowser()
        self.assertTrue(ok)
        self.assertIn("127.0.0.1:58888", get.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
~~~~

- [ ] **Step 2: Run provider integration tests and verify RED**

Run:

~~~~powershell
python -m unittest tests.test_octo_provider_integration -v
~~~~

Expected: factory returns BitBrowser and WebUI metadata lacks Octo settings.

- [ ] **Step 3: Add configuration and factory selection**

In config.py add:

~~~~python
# Fingerprint browser provider: bitbrowser / adspower / octo
FINGERPRINT_BROWSER = _env(
    "FINGERPRINT_BROWSER", "bitbrowser"
).strip().lower()

OCTO_API_TOKEN = _env("OCTO_API_TOKEN", "")
OCTO_PUBLIC_API = _env(
    "OCTO_PUBLIC_API", "https://app.octobrowser.net"
)
OCTO_LOCAL_API = _env(
    "OCTO_LOCAL_API", "http://127.0.0.1:58888"
)
~~~~

In bitbrowser.py add:

~~~~python
def _use_octobrowser():
    return _selected_provider() in {"octo", "octobrowser", "octo_browser"}


class BitBrowser:
    provider_name = "bitbrowser"

    def __new__(cls, api_base=None):
        if cls is BitBrowser and _use_adspower():
            from adspower import AdsPower
            return AdsPower(api_base=api_base)
        if cls is BitBrowser and _use_octobrowser():
            from octobrowser import OctoBrowser
            return OctoBrowser()
        return super().__new__(cls)
~~~~

Do not pass BitBrowser's api_base argument into OctoBrowser; legacy callers supply the BitBrowser default there.

- [ ] **Step 4: Route direct BitBrowser bypasses through the shared factory**

In outlook_reg_loop._bb_create_for_outlook_reg, use the shared factory whenever the provider is not a BitBrowser alias:

~~~~python
    provider = _fingerprint_provider()
    if provider not in {"bitbrowser", "bit", "bb"}:
        from bitbrowser import BitBrowser
        return BitBrowser().create_browser(
            name=name,
            remark="outlook reg loop auto-deleted after use",
            platform="https://outlook.live.com",
            platformIcon="outlook.live.com",
            **proxy_fields,
            browserFingerPrint={
                "ostype": "PC",
                "os": "Win32",
                "coreVersion": BB_CORE_VERSION,
                "isIpCreateTimeZone": True,
                "isIpCreateLanguage": True,
                "isIpCreateDisplayLanguage": True,
                "isIpCreatePosition": True,
                "isIpCountry": True,
            },
        )
~~~~

In register_outlook_standalone.BitBrowserClient.__new__ use:

~~~~python
    def __new__(cls, api_base=None):
        provider = _fingerprint_provider()
        if cls is BitBrowserClient and provider not in {
            "bitbrowser", "bit", "bb"
        }:
            from bitbrowser import BitBrowser
            return BitBrowser()
        return super().__new__(cls)
~~~~

In unlock_outlook._bb_post use:

~~~~python
def _bb_post(path, data=None):
    global _BROWSER_CLIENT
    provider = _fingerprint_provider()
    if provider not in {"bitbrowser", "bit", "bb"}:
        if _BROWSER_CLIENT is None:
            from bitbrowser import BitBrowser
            _BROWSER_CLIENT = BitBrowser()
        return _BROWSER_CLIENT._post(path, data or {})
    response = requests.post(
        f"{BITBROWSER_API}{path}", json=data or {}, timeout=120
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("success"):
        raise Exception(f"BitBrowser: {result.get('msg', '?')}")
    return result
~~~~

These conditions ensure Octo never receives a BitBrowser /browser request at
port 54345.

- [ ] **Step 5: Add WebUI metadata, health, and label mapping**

Add Octo fields to webui/scripts.py:

~~~~python
{"key": "FINGERPRINT_BROWSER", "type": "choice",
 "choices": ["bitbrowser", "adspower", "octo"],
 "default": "bitbrowser", "help": "选择当前指纹浏览器"},
{"key": "OCTO_API_TOKEN", "secret": True,
 "help": "Octo 主账号 Additional 页面中的 API token"},
{"key": "OCTO_PUBLIC_API",
 "default": "https://app.octobrowser.net",
 "help": "Octo Public API"},
{"key": "OCTO_LOCAL_API",
 "default": "http://127.0.0.1:58888",
 "help": "Octo 本机 Local API"},
~~~~

In webui/server.py, add an Octo branch before BitBrowser:

~~~~python
    if provider in {"octo", "octobrowser", "octo_browser"}:
        api = _read_config_val(
            "OCTO_LOCAL_API", "http://127.0.0.1:58888"
        ).rstrip("/")
        name = "Octo Browser"
        paths = ("/api/update",)
~~~~

Replace the provider selection at the start of api_status with:

~~~~python
    provider = _fingerprint_provider()
    if provider in {"octo", "octobrowser", "octo_browser"}:
        bb = _read_config_val(
            "OCTO_LOCAL_API", "http://127.0.0.1:58888"
        )
        provider_label = "octo"
    elif provider in {"adspower", "ads_power", "ads"}:
        bb = _read_config_val(
            "ADSPOWER_API", "http://127.0.0.1:50325"
        )
        provider_label = "adspower"
    else:
        bb = _read_config_val(
            "BITBROWSER_API", "http://127.0.0.1:54345"
        )
        provider_label = "bitbrowser"
~~~~

In webui/static/app.js replace the binary label expression with:

~~~~javascript
const browserNames = {
  bitbrowser: 'BitBrowser',
  adspower: 'AdsPower',
  octo: 'Octo Browser',
};
const label = browserNames[s.browser_provider] || 'BitBrowser';
~~~~

- [ ] **Step 6: Run provider and WebUI tests**

Run:

~~~~powershell
python -m unittest tests.test_octobrowser tests.test_octo_provider_integration tests.test_webui_env_reload -v
~~~~

Expected: all tests pass.

- [ ] **Step 7: Commit Task 4**

~~~~powershell
git add config.py bitbrowser.py outlook_reg_loop.py register_outlook_standalone.py unlock_outlook.py webui/scripts.py webui/server.py webui/static/app.js tests/test_octo_provider_integration.py tests/test_webui_env_reload.py
git commit -m "feat: expose Octo browser provider"
~~~~

---

### Task 5: Documentation, Regression Verification, And Local Smoke Checks

**Files:**
- Modify: .env.example:10-35
- Modify: README.md:55-85, 205-230, 290-365
- Modify: CHANGELOG.md

**Interfaces:**
- Consumes: all behavior from Tasks 1-4.
- Produces: operator-facing configuration and fresh verification evidence.

- [ ] **Step 1: Update .env.example**

Add:

~~~~dotenv
# Fingerprint browser: bitbrowser / adspower / octo
FINGERPRINT_BROWSER=bitbrowser
OCTO_API_TOKEN=
OCTO_PUBLIC_API=https://app.octobrowser.net
OCTO_LOCAL_API=http://127.0.0.1:58888
~~~~

Leave the BitBrowser, AdsPower, Clash, and IPMart example values unchanged.
Add this comment immediately above CLASH_PROXY:

~~~~dotenv
# IPMart 关闭时，仅在该端口可连接时使用 Clash；否则自动直连。
~~~~

- [ ] **Step 2: Update README and CHANGELOG**

Document these exact operator rules:

~~~~text
IPMART_ENABLED=1:
  Outlook -> Graph -> mailbox -> Claude uses the existing account lease.
  ChatGPT and Grok keep their existing route behavior.

IPMART_ENABLED=0:
  Reachable CLASH_PROXY -> existing Clash route.
  Missing/unreachable CLASH_PROXY -> direct connection.
~~~~

Document Octo prerequisites: Base-or-higher API access, master-account token, running local client, Public API URL, Local API URL, and FINGERPRINT_BROWSER=octo.

Add a dated CHANGELOG entry covering the Octo adapter and preflight Clash/direct selection.

- [ ] **Step 3: Run the complete automated test suite**

Run:

~~~~powershell
python -m unittest discover -s tests -v
~~~~

Expected: exit code 0 with no failed or errored tests.

- [ ] **Step 4: Run syntax compilation**

Run:

~~~~powershell
python -m compileall -q .
~~~~

Expected: exit code 0.

- [ ] **Step 5: Run read-only Octo Local API health check**

Run:

~~~~powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:58888/api/update' -TimeoutSec 5
~~~~

Expected on the configured workstation: JSON containing current, latest, and update_required. If the client is intentionally stopped, record the connection failure without treating automated tests as failed.

- [ ] **Step 6: Run a mocked lifecycle verification without consuming a profile**

Run:

~~~~powershell
python -m unittest tests.test_octobrowser tests.test_octo_provider_integration -v
~~~~

Expected: all adapter lifecycle and provider integration tests pass without making Public API calls.

- [ ] **Step 7: Check diff scope and secret safety**

Run:

~~~~powershell
git diff --check
git status --short
rg -n "OCTO_API_TOKEN=.+|IPMART_PROXY_PASSWORD=.+" .env.example README.md CHANGELOG.md octobrowser.py tests
~~~~

Expected: git diff check succeeds; only planned files are modified; secret scan finds no populated credentials.

- [ ] **Step 8: Commit Task 5**

~~~~powershell
git add .env.example README.md CHANGELOG.md
git commit -m "docs: explain Octo and automatic direct routing"
~~~~

- [ ] **Step 9: Final verification after all commits**

Run:

~~~~powershell
python -m unittest discover -s tests -v
python -m compileall -q .
git status --short
~~~~

Expected: all tests pass, compilation exits 0, and the worktree is clean.
