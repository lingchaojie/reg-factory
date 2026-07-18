from datetime import datetime
from pathlib import Path
import hashlib
import json


def _email_key(email):
    normalized = email.strip().lower().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:16]


def _is_claude_domain(value):
    domain = str(value or "").lstrip(".").lower()
    return domain == "claude.com" or domain.endswith(".claude.com")


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
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    email_key = _email_key(email)
    path = target / f"full_{email_key}_{stamp}.json"
    path.write_text(
        json.dumps(platform_cookies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    index = target / "accounts.jsonl"
    with index.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "email_key": email_key,
            "cookie_file": path.name,
        }) + "\n")
    return path
