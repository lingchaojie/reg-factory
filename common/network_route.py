from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
import os
import socket
from typing import Callable, Literal
from urllib.parse import urlparse

from common.account_proxy import strip_http_proxy_env


RESOLVED_ROUTE_ENV_KEY = "NETWORK_ROUTE_MODE"


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
    inherited_mode = (env.get(RESOLVED_ROUTE_ENV_KEY) or "").strip().lower()
    if inherited_mode == "direct":
        route = NetworkRoute("direct", reason="inherited")
    elif inherited_mode == "clash":
        proxy_url = (env.get("CLASH_PROXY") or "").strip()
        if _proxy_endpoint(proxy_url) is None:
            route = NetworkRoute(
                "direct", reason="invalid_inherited_clash"
            )
        else:
            route = NetworkRoute("clash", proxy_url, "inherited")
    else:
        route = resolve_clash_route(env, connector=connector, timeout=timeout)
        env[RESOLVED_ROUTE_ENV_KEY] = route.mode
    apply_clash_route(env, route)
    return route
