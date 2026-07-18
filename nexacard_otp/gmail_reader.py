import asyncio
import base64
import binascii
import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from google.auth.exceptions import TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .errors import GmailTemporarilyUnavailable
from .gmail_auth import load_valid_credentials


EXPECTED_SENDER = "jushihui@mail.jushipay.com"
EXPECTED_SUBJECT = "NexaCard Verification Code"
CODE_PATTERN = re.compile(r"(?<!\d)(\d{9})(?!\d)")


def _decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def parse_login_code(raw_message: str, internal_date_ms: int, sent_after: datetime) -> str | None:
    """Return a fresh NexaCard code only from the expected MIME message body."""
    received = datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc)
    if received <= sent_after.astimezone(timezone.utc):
        return None
    try:
        message = BytesParser(policy=policy.default).parsebytes(_decode(raw_message))
    except (binascii.Error, TypeError, ValueError):
        return None

    from_headers = message.get_all("From", [])
    subject_headers = message.get_all("Subject", [])
    if len(from_headers) != 1 or len(subject_headers) != 1:
        return None
    sender = parseaddr(str(from_headers[0]))[1].strip().lower()
    subject = str(subject_headers[0]).strip()
    if sender != EXPECTED_SENDER or subject != EXPECTED_SUBJECT:
        return None

    bodies: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        try:
            bodies.append(part.get_content())
        except (LookupError, UnicodeError, ValueError):
            continue

    match = CODE_PATTERN.search("\n".join(bodies))
    return match.group(1) if match else None


class GmailCodeReader:
    def _fetch_once(self, sent_after: datetime) -> str | None:
        try:
            service = build(
                "gmail",
                "v1",
                credentials=load_valid_credentials(),
                cache_discovery=False,
            )
            query = f'from:({EXPECTED_SENDER}) subject:"{EXPECTED_SUBJECT}" newer_than:1d'
            items = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=10)
                .execute()
                .get("messages", [])
            )
            for item in items:
                data = (
                    service.users()
                    .messages()
                    .get(userId="me", id=item["id"], format="raw")
                    .execute()
                )
                code = parse_login_code(data["raw"], int(data["internalDate"]), sent_after)
                if code:
                    return code
            return None
        except (HttpError, OSError, TransportError, KeyError, TypeError, ValueError) as exc:
            raise GmailTemporarilyUnavailable("Gmail API is temporarily unavailable") from exc

    async def wait_for_login_code(
        self,
        sent_after: datetime,
        interval_seconds: float = 3.0,
        max_attempts: int = 60,
    ) -> str:
        for attempt in range(max_attempts):
            try:
                code = await asyncio.to_thread(self._fetch_once, sent_after)
            except GmailTemporarilyUnavailable:
                code = None
            if code:
                return code
            if attempt + 1 < max_attempts:
                await asyncio.sleep(interval_seconds)
        raise TimeoutError("NexaCard login verification email did not arrive")
