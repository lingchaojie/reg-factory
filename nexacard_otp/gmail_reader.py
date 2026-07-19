import asyncio
import base64
import binascii
import re
from calendar import timegm
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from google.auth.exceptions import TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .errors import GmailAuthorizationRequired, GmailTemporarilyUnavailable
from .gmail_auth import (
    load_authorized_credentials,
    load_valid_credentials,
    raise_for_gmail_http_error,
    refresh_credentials_after_unauthorized,
)


EXPECTED_SENDER = "jushihui@mail.jushipay.com"
EXPECTED_SUBJECT = "NexaCard Verification Code"
LOGIN_MESSAGE_QUERY = f'from:({EXPECTED_SENDER}) subject:"{EXPECTED_SUBJECT}" newer_than:1d'
FETCH_PAGE_SIZE = 10
SNAPSHOT_PAGE_SIZE = 500
MAX_SNAPSHOT_PAGES = 100
CODE_PATTERN = re.compile(r"(?<![0-9])([0-9]{9})(?![0-9])")


def _decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def parse_login_code(raw_message: str, internal_date_ms: int, sent_after: datetime) -> str | None:
    """Return a fresh NexaCard code only from the expected MIME message body."""
    if sent_after.tzinfo is None or sent_after.utcoffset() is None:
        raise ValueError("sent_after must be timezone-aware")
    sent_after_utc = sent_after.astimezone(timezone.utc)
    sent_after_ms = timegm(sent_after_utc.utctimetuple()) * 1000 + sent_after_utc.microsecond // 1000
    if internal_date_ms < sent_after_ms:
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
        if (
            part.is_multipart()
            or part.get_content_disposition() == "attachment"
            or part.get_filename() is not None
        ):
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
    @staticmethod
    def _credentials_for(expected_email: str):
        normalized = expected_email.strip().lower()
        if normalized:
            return load_authorized_credentials(normalized)
        return load_valid_credentials()

    @classmethod
    def _run_with_auth_retry(cls, operation, expected_email: str):
        credentials = cls._credentials_for(expected_email)
        try:
            return operation(credentials)
        except HttpError as exc:
            if getattr(exc.resp, "status", None) != 401:
                raise_for_gmail_http_error(
                    exc, "Gmail API is temporarily unavailable"
                )

        credentials = refresh_credentials_after_unauthorized(credentials)
        try:
            return operation(credentials)
        except HttpError as exc:
            raise_for_gmail_http_error(exc, "Gmail API is temporarily unavailable")

    @staticmethod
    def _matching_message_page(
        service, *, max_results: int, page_token: str | None = None
    ) -> dict:
        arguments = {
            "userId": "me",
            "q": LOGIN_MESSAGE_QUERY,
            "maxResults": max_results,
        }
        if page_token is not None:
            arguments["pageToken"] = page_token
        return (
            service.users()
            .messages()
            .list(**arguments)
            .execute()
        )

    @classmethod
    def _matching_message_items(cls, service) -> list[dict]:
        return cls._matching_message_page(service, max_results=FETCH_PAGE_SIZE).get("messages", [])

    def _snapshot_login_message_ids_once(
        self, *, expected_email: str = ""
    ) -> frozenset[str]:
        def snapshot(credentials) -> frozenset[str]:
            service = build(
                "gmail",
                "v1",
                credentials=credentials,
                cache_discovery=False,
            )
            message_ids: set[str] = set()
            page_token: str | None = None
            seen_page_tokens: set[str] = set()
            for _ in range(MAX_SNAPSHOT_PAGES):
                page = self._matching_message_page(
                    service, max_results=SNAPSHOT_PAGE_SIZE, page_token=page_token
                )
                for item in page.get("messages", []):
                    message_id = item["id"]
                    if isinstance(message_id, str):
                        message_ids.add(message_id)
                next_page_token = page.get("nextPageToken")
                if next_page_token is None:
                    return frozenset(message_ids)
                if (
                    not isinstance(next_page_token, str)
                    or not next_page_token
                    or next_page_token in seen_page_tokens
                ):
                    raise ValueError("invalid Gmail nextPageToken")
                seen_page_tokens.add(next_page_token)
                page_token = next_page_token
            raise ValueError("Gmail message snapshot exceeded page limit")

        try:
            return self._run_with_auth_retry(snapshot, expected_email)
        except (OSError, TransportError, KeyError, TypeError, ValueError) as exc:
            raise GmailTemporarilyUnavailable("Gmail API is temporarily unavailable") from exc

    async def snapshot_login_message_ids(
        self, *, expected_email: str = ""
    ) -> frozenset[str]:
        """Return the bounded matching-message ID baseline before requesting a new code."""
        return await asyncio.to_thread(
            self._snapshot_login_message_ids_once, expected_email=expected_email
        )

    def _fetch_once(
        self,
        sent_after: datetime,
        *,
        excluded_message_ids: frozenset[str] = frozenset(),
        expected_email: str = "",
    ) -> str | None:
        def fetch(credentials) -> str | None:
            service = build(
                "gmail",
                "v1",
                credentials=credentials,
                cache_discovery=False,
            )
            for item in self._matching_message_items(service):
                message_id = item["id"]
                if message_id in excluded_message_ids:
                    continue
                data = (
                    service.users()
                    .messages()
                    .get(userId="me", id=message_id, format="raw")
                    .execute()
                )
                code = parse_login_code(data["raw"], int(data["internalDate"]), sent_after)
                if code:
                    return code
            return None

        try:
            return self._run_with_auth_retry(fetch, expected_email)
        except (OSError, TransportError, KeyError, TypeError, ValueError) as exc:
            raise GmailTemporarilyUnavailable("Gmail API is temporarily unavailable") from exc

    async def wait_for_login_code(
        self,
        sent_after: datetime,
        interval_seconds: float = 3.0,
        max_attempts: int = 60,
        *,
        excluded_message_ids: frozenset[str] = frozenset(),
        expected_email: str = "",
    ) -> str:
        """Wait for a fresh code, excluding IDs present before the send request.

        Gmail timestamps are millisecond-granular, so timestamp comparison alone
        cannot distinguish a fast new email from an old email in that millisecond.
        Callers snapshot matching IDs before sending and pass them here.
        """
        last_temporary_error: GmailTemporarilyUnavailable | None = None
        for attempt in range(max_attempts):
            try:
                code = await asyncio.to_thread(
                    self._fetch_once,
                    sent_after,
                    excluded_message_ids=excluded_message_ids,
                    expected_email=expected_email,
                )
                last_temporary_error = None
            except GmailTemporarilyUnavailable as exc:
                last_temporary_error = exc
                code = None
            if code:
                return code
            if attempt + 1 < max_attempts:
                await asyncio.sleep(interval_seconds)
        if last_temporary_error is not None:
            raise last_temporary_error
        raise TimeoutError("NexaCard login verification email did not arrive")
