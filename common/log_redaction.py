"""Shared redaction helpers for mailbox addresses in orchestrator output."""

import hashlib
import re


_COMBINING_MARKS = "\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"
_LOCAL_ATOM = rf"[\w{_COMBINING_MARKS}.!#$%&'*+/=?^`{{|}}~-]+"
_QUOTED_LOCAL = r'"(?:[^"\\\r\n]|\\.)+"'
_LOCAL = rf"(?:{_LOCAL_ATOM}|{_QUOTED_LOCAL})"
_DOMAIN_UNIT = rf"[\w{_COMBINING_MARKS}]"
_DOMAIN_LABEL = (
    rf"(?:xn--[a-z0-9-]+|{_DOMAIN_UNIT}"
    rf"(?:[\w{_COMBINING_MARKS}-]*{_DOMAIN_UNIT})?)"
)
_EMAIL_TEXT_RE = re.compile(
    rf"(?<![\w.!#$%&'*+/=?^`{{|}}~-])"
    rf"{_LOCAL}@(?:{_DOMAIN_LABEL}\.)+{_DOMAIN_LABEL}"
    rf"(?![\w{_COMBINING_MARKS}@-])",
    re.IGNORECASE | re.UNICODE,
)


def masked_email(email):
    local, separator, domain = str(email or "").rpartition("@")
    if not separator:
        return "***"
    if local.startswith('"') and local.endswith('"'):
        return f'"{local[1:-1][:2]}***"@{domain}'
    return f"{local[:2]}***@{domain}"


def mask_email_text(value):
    return _EMAIL_TEXT_RE.sub(
        lambda match: masked_email(match.group(0)), str(value or "")
    )


def email_log_key(email, length=10):
    normalized = str(email or "").strip().lower().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[: int(length)]
