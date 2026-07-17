from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import os
import threading
import time

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


def _env_int(env, key: str, default: str) -> int:
    try:
        return int(env.get(key, default))
    except (TypeError, ValueError) as exc:
        raise IPMartProxyError(f"{key} must be an integer") from exc


def settings_from_env(env=None) -> IPMartSettings:
    env = os.environ if env is None else env
    enabled = _truthy(env.get("IPMART_ENABLED", "0"))
    access_key = (env.get("IPMART_ACCESS_KEY") or "").strip()
    sticky_minutes = _env_int(env, "IPMART_STICKY_MINUTES", "30")
    max_attempts = _env_int(env, "IPMART_MAX_ATTEMPTS", "3")

    if enabled and not access_key:
        raise IPMartProxyError(
            "IPMART_ACCESS_KEY is required when IPMart is enabled"
        )
    if not 5 <= sticky_minutes <= 30:
        raise IPMartProxyError(
            "IPMART_STICKY_MINUTES must be between 5 and 30"
        )
    if max_attempts < 1:
        raise IPMartProxyError("IPMART_MAX_ATTEMPTS must be positive")

    return IPMartSettings(
        enabled=enabled,
        access_key=access_key,
        api_base=(env.get("IPMART_API_BASE") or DEFAULT_API_BASE).strip(),
        country=(env.get("IPMART_COUNTRY") or "US").strip().upper(),
        sticky_minutes=sticky_minutes,
        max_attempts=max_attempts,
        ip_check_url=(
            env.get("IPMART_IP_CHECK_URL") or DEFAULT_IP_CHECK_URL
        ).strip(),
    )


def parse_proxy_text(text: str) -> tuple[str, int]:
    body = (text or "").strip()
    if not body or "<html" in body.lower():
        raise IPMartProxyError("IPMart returned no usable proxy endpoint")

    line = next((item.strip() for item in body.splitlines() if item.strip()), "")
    host, separator, raw_port = line.rpartition(":")
    if not separator or not host or not raw_port.isdigit():
        raise IPMartProxyError("IPMart returned a malformed proxy endpoint")

    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise IPMartProxyError("IPMart returned an invalid proxy port")
    return host.strip("[]"), port


def _new_direct_session(factory):
    session = factory()
    session.trust_env = False
    session.proxies = {}
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


def acquire_proxy(
    used_exit_ips: set[str] | None = None,
    usage_path: str | None = None,
    *,
    env=None,
    api_session_factory=requests.Session,
    probe_session_factory=requests.Session,
    reserve: bool = True,
    sleep=time.sleep,
) -> ProxyLease:
    settings = settings_from_env(env)
    if not settings.enabled:
        raise IPMartProxyError(
            "IPMart proxy acquisition requested while disabled"
        )

    used = set(used_exit_ips or ()) | load_used_exit_ips(usage_path)
    last_error = "unknown error"
    for attempt in range(1, settings.max_attempts + 1):
        try:
            session = _new_direct_session(api_session_factory)
            response = session.get(
                settings.api_base,
                params={
                    "accessKey": settings.access_key,
                    "num": 1,
                    "cntryCode": settings.country,
                    "time": settings.sticky_minutes,
                    "format": 1,
                },
                timeout=20,
            )
            if response.status_code != 200:
                raise IPMartProxyError(
                    f"IPMart API returned HTTP {response.status_code}"
                )

            host, port = parse_proxy_text(response.text)
            candidate = ProxyLease("http", host, port, "")
            exit_ip = verify_proxy(
                candidate,
                env=env,
                session_factory=probe_session_factory,
            )
            if exit_ip in used:
                raise IPMartProxyError(
                    f"IPMart returned duplicate exit IP {exit_ip}"
                )

            lease = ProxyLease("http", host, port, exit_ip)
            if reserve and not _reserve_if_unique(lease, usage_path):
                used.add(exit_ip)
                raise IPMartProxyError(
                    f"IPMart returned duplicate exit IP {exit_ip}"
                )
            return lease
        except IPMartProxyError as exc:
            last_error = str(exc)
        except Exception:
            last_error = "IPMart API request failed"

        if attempt < settings.max_attempts:
            sleep(attempt)

    raise IPMartProxyError(
        "IPMart proxy acquisition failed after "
        f"{settings.max_attempts} attempts: {last_error}"
    )
