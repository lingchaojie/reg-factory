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


def lease_from_env(env=None) -> ProxyLease | None:
    env = os.environ if env is None else env
    if (env.get("ACCOUNT_PROXY_SOURCE") or "").strip().lower() != "ipmart":
        return None

    proxy_type = (env.get("ACCOUNT_PROXY_TYPE") or "").strip().lower()
    host = (env.get("ACCOUNT_PROXY_HOST") or "").strip()
    raw_port = (env.get("ACCOUNT_PROXY_PORT") or "").strip()
    raw_exit_ip = (env.get("ACCOUNT_PROXY_EXIT_IP") or "").strip()

    if proxy_type != "http" or not host or not raw_port.isdigit():
        raise IPMartProxyError("invalid inherited account proxy lease")
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise IPMartProxyError("invalid inherited account proxy port")
    try:
        exit_ip = str(ipaddress.ip_address(raw_exit_ip))
    except ValueError as exc:
        raise IPMartProxyError(
            "invalid inherited account proxy exit IP"
        ) from exc

    return ProxyLease(proxy_type, host, port, exit_ip)


def bitbrowser_proxy_fields(lease: ProxyLease) -> dict[str, object]:
    return {
        "proxyMethod": 2,
        "proxyType": lease.proxy_type,
        "host": lease.host,
        "port": str(lease.port),
    }
