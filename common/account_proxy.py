from __future__ import annotations

from collections.abc import MutableMapping
import ipaddress
import os
import re

from common.ipmart_proxy import IPMartProxyError, ProxyLease


HTTP_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
IPMART_BITBROWSER_ERROR = (
    "BitBrowser profile creation failed with IPMart account proxy"
)
_BITBROWSER_QUOTA_MARKERS = (
    "maximum quota", "quota exceeded", "too many", "最大创建窗口数", "超过",
)
_BITBROWSER_TRANSIENT_MARKERS = (
    "tls", "socket", "econnreset", "connection", "network", "timed out",
    "timeout", "max retries", "remotedisconnected",
)
ACCOUNT_PROXY_ENV_KEYS = (
    "ACCOUNT_PROXY_SOURCE", "ACCOUNT_PROXY_TYPE", "ACCOUNT_PROXY_HOST",
    "ACCOUNT_PROXY_PORT", "ACCOUNT_PROXY_USERNAME", "ACCOUNT_PROXY_PASSWORD",
    "ACCOUNT_PROXY_SID", "ACCOUNT_PROXY_EXIT_IP",
)


class IPMartBitBrowserError(RuntimeError):
    def __init__(self, category: str):
        if category not in {"quota", "transient", "configuration"}:
            category = "configuration"
        super().__init__(IPMART_BITBROWSER_ERROR)
        self.category = category


def sanitized_bitbrowser_error(exc: Exception) -> IPMartBitBrowserError:
    message = str(exc).lower()
    if any(marker in message for marker in _BITBROWSER_QUOTA_MARKERS):
        category = "quota"
    elif any(marker in message for marker in _BITBROWSER_TRANSIENT_MARKERS):
        category = "transient"
    else:
        category = "configuration"
    return IPMartBitBrowserError(category)


def lease_to_env(lease: ProxyLease) -> dict[str, str]:
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


def lease_from_env(env=None) -> ProxyLease | None:
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
        raise IPMartProxyError(
            "invalid inherited account proxy exit IP"
        ) from None

    return ProxyLease(
        proxy_type, host, port, username, password, sid, exit_ip
    )


def bitbrowser_proxy_fields(lease: ProxyLease) -> dict[str, object]:
    return {
        "proxyMethod": 2,
        "proxyType": lease.proxy_type,
        "host": lease.host,
        "port": str(lease.port),
        "proxyUserName": lease.username,
        "proxyPassword": lease.password,
    }


def strip_http_proxy_env(
    env: MutableMapping[str, str],
) -> MutableMapping[str, str]:
    for key in HTTP_PROXY_ENV_KEYS:
        env.pop(key, None)
    return env


def strip_account_proxy_env(
    env: MutableMapping[str, str],
) -> MutableMapping[str, str]:
    for key in ACCOUNT_PROXY_ENV_KEYS:
        env.pop(key, None)
    return env
