# -*- coding: utf-8 -*-
"""
common/emails.py — 邮箱供给（平台独立的占用记录）

读取 emails.txt（email----password----refresh_token----client_id），
每个平台用独立的 emails_used_<platform>.txt 记录已占用，互不干扰。
线程安全。
"""

import os
import sys
import threading

from common.log_redaction import masked_email

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

EMAILS_FILE = "emails.txt"
_lock = threading.Lock()


def _used_file(platform):
    return f"emails_used_{platform}.txt"


def _error_file(platform):
    return f"emails_error_{platform}.txt"


def _load_used(platform):
    used = set()
    for fp in [_used_file(platform), _error_file(platform)]:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        used.add(line.split("----")[0].strip().lower())
    return used


def next_email(platform, *, display="full"):
    """取下一个未被该平台占用的邮箱，返回 (email, password, refresh_token, client_id) 或 None。
    取出即标记 reserved，防止并发重复。"""
    if display not in {"full", "masked", "silent"}:
        raise ValueError("display must be full, masked, or silent")
    with _lock:
        if not os.path.exists(EMAILS_FILE):
            print(f"  [email] {EMAILS_FILE} not found")
            return None
        used = _load_used(platform)
        with open(EMAILS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----")
                email = parts[0].strip()
                if email.lower() in used:
                    continue
                password = parts[1].strip() if len(parts) >= 2 else ""
                token = parts[2].strip() if len(parts) >= 3 else ""
                client_id = parts[3].strip() if len(parts) >= 4 else ""
                with open(_used_file(platform), "a", encoding="utf-8") as uf:
                    uf.write(f"{email}----{password}----reserved\n")
                if display == "masked":
                    shown = masked_email(email)
                elif display == "silent":
                    shown = None
                elif display == "full":
                    shown = email
                if shown is not None:
                    print(f"  [email] picked for {platform}: {shown}")
                return email, password, token, client_id
        print(f"  [email] no unused emails left for {platform}")
        return None


def mark_used(platform, email, password=""):
    with open(_used_file(platform), "a", encoding="utf-8") as f:
        f.write(f"{email}----{password}----ok\n")


def mark_error(platform, email, password="", reason=""):
    with open(_error_file(platform), "a", encoding="utf-8") as f:
        f.write(f"{email}----{password}----{reason}\n")
