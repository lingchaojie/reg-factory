from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import quote

import requests


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


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(env, key: str, default: str) -> int:
    try:
        return int(env.get(key, default))
    except (TypeError, ValueError) as exc:
        raise IPMartProxyError(f"{key} must be an integer") from exc


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


def _credentialed_session(lease, session_factory):
    session = session_factory()
    session.trust_env = False
    proxy_url = requests_proxy_url(lease)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def _read_exit_ip(response) -> str:
    if response.status_code != 200:
        raise IPMartProxyError(
            f"proxy IP check failed with HTTP {response.status_code}"
        )
    try:
        value = response.json().get("ip", "")
    except (ValueError, AttributeError):
        value = (response.text or "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise IPMartProxyError(
            "proxy IP check returned an invalid address"
        ) from exc


def verify_proxy(
    lease: ProxyLease,
    expected_exit_ip: str | None = None,
    *,
    env=None,
    session_factory=requests.Session,
) -> str:
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


def load_used_exit_ips(path: str | None = None) -> set[str]:
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


def _write_lease(lease: ProxyLease, path: str) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoint": f"{lease.host}:{lease.port}",
        "sid": lease.sid,
        "exit_ip": lease.exit_ip,
    }
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")


def reserve_lease(lease: ProxyLease, path: str | None = None) -> None:
    path = path or DEFAULT_USAGE_PATH
    with _USAGE_LOCK:
        _write_lease(lease, path)


def _reserve_if_unique(lease: ProxyLease, path: str | None = None) -> bool:
    path = path or DEFAULT_USAGE_PATH
    with _USAGE_LOCK:
        if lease.exit_ip in load_used_exit_ips(path):
            return False
        _write_lease(lease, path)
        return True


def _next_unique_sid(sid_factory, seen):
    for _ in range(10):
        sid = sid_factory()
        if re.fullmatch(r"\d{8}", sid or "") and sid not in seen:
            seen.add(sid)
            return sid
    raise IPMartProxyError("could not generate a unique eight-digit SID")


def acquire_proxy(
    used_exit_ips: set[str] | None = None,
    usage_path: str | None = None,
    *,
    env=None,
    session_factory=requests.Session,
    sid_factory=generate_sid,
    reserve: bool = True,
    sleep=time.sleep,
) -> ProxyLease:
    settings = settings_from_env(env)
    if not settings.enabled:
        raise IPMartProxyError(
            "IPMart proxy acquisition requested while disabled"
        )

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
