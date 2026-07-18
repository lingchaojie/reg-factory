from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import threading
import uuid


_INDEX_LOCK = threading.Lock()


def _email_key(email):
    normalized = email.strip().lower().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:16]


def _is_claude_domain(value):
    domain = str(value or "").lstrip(".").lower()
    return domain == "claude.com" or domain.endswith(".claude.com")


def _append_index_record(index, record):
    encoded = (json.dumps(record) + "\n").encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    with _INDEX_LOCK:
        descriptor = os.open(index, flags, 0o600)
        try:
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise OSError("incomplete index write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


async def save_claude_platform_session(
    context,
    email,
    output_dir="cookies/claude_api",
):
    cookies = await context.cookies()
    platform_cookies = [
        cookie for cookie in cookies if _is_claude_domain(cookie.get("domain"))
    ]
    if not platform_cookies:
        raise RuntimeError("console_not_reached")

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    email_key = _email_key(email)
    nonce = uuid.uuid4().hex
    path = target / f"full_{email_key}_{stamp}_{nonce}.json"
    temporary = target / f".{path.name}.{uuid.uuid4().hex}.tmp"
    index = target / "accounts.jsonl"
    final_created = False
    try:
        temporary.write_text(
            json.dumps(platform_cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        final_created = True
        _append_index_record(index, {
            "email_key": email_key,
            "cookie_file": path.name,
        })
    except Exception:
        temporary.unlink(missing_ok=True)
        if final_created:
            path.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return path
