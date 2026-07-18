from datetime import datetime
from pathlib import Path
import asyncio
import hashlib
import json
import os
import uuid

from common.interprocess_lock import InterprocessFileLock


def _email_key(email):
    normalized = email.strip().lower().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:16]


def _is_claude_domain(value):
    domain = str(value or "").lstrip(".").lower()
    return domain == "claude.com" or domain.endswith(".claude.com")


def _write_fsynced(path, payload):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("incomplete index write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path):
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_index(index, record):
    encoded_record = (json.dumps(record) + "\n").encode("utf-8")
    temporary = index.parent / f".{index.name}.{uuid.uuid4().hex}.tmp"
    lock_path = index.parent / f".{index.name}.lock"
    with InterprocessFileLock(lock_path):
        existing = index.read_bytes() if index.exists() else b""
        separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
        payload = existing + separator + encoded_record
        committed = False
        try:
            _write_fsynced(temporary, payload)
            os.replace(temporary, index)
            committed = True
        finally:
            if not committed:
                temporary.unlink(missing_ok=True)


def _persist_claude_platform_session(platform_cookies, email, output_dir):
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
        cookie_payload = json.dumps(
            platform_cookies, ensure_ascii=False, indent=2
        ).encode("utf-8")
        _write_fsynced(temporary, cookie_payload)
        os.replace(temporary, path)
        final_created = True
        _fsync_directory(target)
        _replace_index(index, {
            "email_key": email_key,
            "cookie_file": path.name,
        })
    except Exception:
        temporary.unlink(missing_ok=True)
        if final_created:
            path.unlink(missing_ok=True)
            _fsync_directory(target)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return path


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

    return await asyncio.to_thread(
        _persist_claude_platform_session,
        platform_cookies,
        email,
        output_dir,
    )
