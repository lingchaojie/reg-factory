import os
import re
import time

import requests


_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_ENV_VALUES


try:
    from config import (
        OCTO_API_TOKEN,
        OCTO_LOCAL_API_BASE,
        OCTO_PUBLIC_API_BASE,
    )
except Exception:
    OCTO_API_TOKEN = os.environ.get("OCTO_API_TOKEN", "")
    OCTO_PUBLIC_API_BASE = os.environ.get(
        "OCTO_PUBLIC_API_BASE"
    ) or os.environ.get(
        "OCTO_PUBLIC_API",
        "https://app.octobrowser.net/api/v2/automation",
    )
    OCTO_LOCAL_API_BASE = os.environ.get(
        "OCTO_LOCAL_API_BASE"
    ) or os.environ.get(
        "OCTO_LOCAL_API", "http://127.0.0.1:58888"
    )


class OctoBrowser:
    provider_name = "octo"
    public_automation_path = "/api/v2/automation"

    def __init__(
        self,
        *,
        public_api=None,
        local_api=None,
        api_token=None,
        session=None,
    ):
        public_base = (
            public_api
            or OCTO_PUBLIC_API_BASE
            or "https://app.octobrowser.net/api/v2/automation"
        ).rstrip("/")
        self.public_api = self._normalize_public_api_base(public_base)
        self.local_api = (
            local_api
            or OCTO_LOCAL_API_BASE
            or "http://127.0.0.1:58888"
        ).rstrip("/")
        self.api_token = OCTO_API_TOKEN if api_token is None else api_token
        self.session = session or requests.Session()
        self.session.trust_env = False

    @classmethod
    def _normalize_public_api_base(cls, value):
        base = str(value).rstrip("/")
        if base.endswith(cls.public_automation_path):
            return base
        return base + cls.public_automation_path

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
            raise RuntimeError("OCTO_API_TOKEN is required for Octo Public API")
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
                        f"Octo transport error at {url}: {exc}", protected
                    )
                ) from None
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            failed = response.status_code >= 400 or (
                isinstance(payload, dict) and payload.get("success") is False
            )
            if failed:
                detail = (
                    payload.get("msg")
                    or payload.get("error")
                    or f"HTTP {response.status_code}"
                )
                raise RuntimeError(
                    self._redact(
                        f"Octo API error at {url}: {detail}", protected
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
        value = value.replace(",", "@", 1) if "@" not in value and "," in value else value
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
            "login": data.get("proxyUserName") or data.get("proxy_user") or "",
            "password": (
                data.get("proxyPassword") or data.get("proxy_password") or ""
            ),
        }

    def _profile_payload(self, name, data):
        payload = {"title": name, "fingerprint": {"os": "win"}}
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

    def create_browser(
        self, name="claude_register", proxy_str=None, _retries=5, **kwargs
    ):
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
            self.public_api + "/profiles",
            public=True,
            json_body=payload,
            retries=_retries,
            secrets=(proxy.get("login"), proxy.get("password")),
        )
        profile_id = (result.get("data") or {}).get("uuid")
        if not profile_id:
            raise RuntimeError("Octo create returned no profile UUID")
        return str(profile_id)

    def update_browser(self, profile_id, name=None, _retries=5, **kwargs):
        payload = self._profile_payload(
            name or kwargs.pop("title", "reg_factory"), kwargs
        )
        proxy = payload.get("proxy") or {}
        self._request(
            "PATCH",
            self.public_api + "/profiles/" + str(profile_id),
            public=True,
            json_body=payload,
            retries=_retries,
            secrets=(proxy.get("login"), proxy.get("password")),
        )
        return {"id": str(profile_id)}

    def open_browser(self, profile_id, _retries=5):
        data = self._request(
            "POST",
            self.local_api + "/api/profiles/start",
            json_body={
                "uuid": str(profile_id),
                "headless": _env_bool("OCTO_HEADLESS"),
                "debug_port": True,
                "only_local": True,
                "flags": [],
                "timeout": 120,
                "password": "",
            },
            retries=_retries,
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

    def close_browser(self, profile_id, _retries=5):
        return self._request(
            "POST",
            self.local_api + "/api/profiles/stop",
            json_body={"uuid": str(profile_id)},
            retries=_retries,
        )

    def delete_browser(self, profile_id, _retries=5):
        return self._request(
            "DELETE",
            self.public_api + "/profiles",
            public=True,
            json_body={"uuids": [str(profile_id)], "skip_trash_bin": True},
            retries=_retries,
        )

    def list_browsers(self, page=0, page_size=100, _retries=5):
        result = self._request(
            "GET",
            self.public_api + "/profiles",
            public=True,
            params={
                "page_len": int(page_size),
                "page": int(page),
                "fields": "title,status",
            },
            timeout=30,
            retries=_retries,
        )
        raw_items = result.get("data") or []
        items = []
        for index, item in enumerate(raw_items):
            mapped = dict(item)
            mapped["id"] = str(item.get("uuid") or "")
            mapped.setdefault("name", item.get("title") or "")
            mapped.setdefault("seq", index)
            items.append(mapped)
        return {
            "success": True,
            "data": {
                "list": items,
                "totalNum": result.get("total_count", len(items)),
            },
        }

    def cleanup_browsers(self, keep=0):
        browsers = self.list_browsers(page=0, page_size=200)["data"]["list"]
        browsers.sort(key=lambda item: item.get("seq", 0) or 0, reverse=True)
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
                _retries=_retries,
            )
        profile_id = data.get("id") or data.get("browserId")
        if path == "/browser/open":
            return {
                "success": True,
                "data": self.open_browser(profile_id, _retries=_retries),
            }
        if path == "/browser/close":
            return {
                "success": True,
                "data": self.close_browser(profile_id, _retries=_retries),
            }
        if path == "/browser/delete":
            return {
                "success": True,
                "data": self.delete_browser(profile_id, _retries=_retries),
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
                self.update_browser(
                    existing, name=name, _retries=_retries, **body
                )
                return {
                    "success": True,
                    "data": {"id": str(existing), "browserId": str(existing)},
                }
            created = self.create_browser(name=name, _retries=_retries, **body)
            return {
                "success": True,
                "data": {"id": created, "browserId": created},
            }
        raise NotImplementedError(
            f"Octo compatibility endpoint not supported: {path}"
        )
