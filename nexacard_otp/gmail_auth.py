import json
import os
import re
import tempfile
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlparse

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import Flow

from .errors import GmailAuthorizationRequired, GmailTemporarilyUnavailable
from .models import AuthStatus
from .settings import (
    PRIVATE_CREDENTIALS_PATH,
    PRIVATE_TOKEN_META_PATH,
    PRIVATE_TOKEN_PATH,
    ensure_private_oauth_files,
)


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_META_KEYS = {
    "authorized_email",
    "authorized_at",
    "estimated_expires_at",
    "estimated",
}
_AUTHORIZATION_403_REASONS = frozenset({"autherror", "insufficientpermissions"})
_FILE_LOCK_TIMEOUT_SECONDS = 5.0
_FILE_LOCK_POLL_SECONDS = 0.05
_PROCESS_FILE_LOCK = threading.RLock()
_FILE_LOCK_STATE = threading.local()


def _lock_file_path() -> Path:
    return PRIVATE_TOKEN_PATH.parent / ".oauth.lock"


def _try_lock_file(handle) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _OAuthFileTransaction:
    """Reentrant in-process guard backed by a bounded cross-process file lock."""

    def __enter__(self):
        if not _PROCESS_FILE_LOCK.acquire(timeout=_FILE_LOCK_TIMEOUT_SECONDS):
            raise GmailTemporarilyUnavailable(
                "Google credential storage is temporarily busy"
            )
        self._process_acquired = True
        depth = getattr(_FILE_LOCK_STATE, "depth", 0)
        if depth:
            _FILE_LOCK_STATE.depth = depth + 1
            return self

        handle = None
        try:
            lock_path = _lock_file_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+b")
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + _FILE_LOCK_TIMEOUT_SECONDS
            while not _try_lock_file(handle):
                if time.monotonic() >= deadline:
                    raise GmailTemporarilyUnavailable(
                        "Google credential storage is temporarily busy"
                    )
                time.sleep(_FILE_LOCK_POLL_SECONDS)
            _FILE_LOCK_STATE.depth = 1
            _FILE_LOCK_STATE.handle = handle
            handle = None
            return self
        except GmailTemporarilyUnavailable:
            raise
        except OSError as exc:
            raise GmailTemporarilyUnavailable(
                "Google credential storage is temporarily unavailable"
            ) from exc
        finally:
            if handle is not None:
                handle.close()
            if not hasattr(_FILE_LOCK_STATE, "depth"):
                _PROCESS_FILE_LOCK.release()
                self._process_acquired = False

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            depth = getattr(_FILE_LOCK_STATE, "depth", 0)
            if depth > 1:
                _FILE_LOCK_STATE.depth = depth - 1
                return
            handle = getattr(_FILE_LOCK_STATE, "handle", None)
            if handle is not None:
                try:
                    _unlock_file(handle)
                finally:
                    handle.close()
            for name in ("depth", "handle"):
                if hasattr(_FILE_LOCK_STATE, name):
                    delattr(_FILE_LOCK_STATE, name)
        finally:
            if getattr(self, "_process_acquired", False):
                _PROCESS_FILE_LOCK.release()


@contextmanager
def oauth_file_transaction() -> Iterator[None]:
    with _OAuthFileTransaction():
        yield


class OAuthCoordinator:
    """Coordinate one-time desktop Google OAuth authorization attempts."""

    pending_ttl = timedelta(minutes=10)
    MAX_PENDING = 100

    def __init__(self) -> None:
        self.pending: dict[str, tuple[object, ...]] = {}
        self._pending_lock = threading.Lock()

    @property
    def max_pending(self) -> int:
        return self.MAX_PENDING

    @max_pending.setter
    def max_pending(self, value: int) -> None:
        self.MAX_PENDING = value

    def start(self, email: str, redirect_uri: str) -> str:
        normalized = email.strip().lower()
        if not self._is_valid_email(normalized):
            raise ValueError("a valid verification email is required")

        with oauth_file_transaction():
            ensure_private_oauth_files()
        flow = Flow.from_client_secrets_file(str(PRIVATE_CREDENTIALS_PATH), scopes=SCOPES)
        flow.redirect_uri = redirect_uri
        requested_state = secrets.token_urlsafe(32)
        authorization_url, returned_state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            login_hint=normalized,
            include_granted_scopes="true",
            state=requested_state,
        )
        if returned_state != requested_state:
            raise ValueError("OAuth state returned by Google does not match the request")
        with self._pending_lock:
            self._cleanup_expired_locked(datetime.now(timezone.utc))
            if len(self.pending) >= self.MAX_PENDING:
                raise ValueError("too many pending OAuth authorizations; try again")
            self.pending[requested_state] = (normalized, flow, datetime.now(timezone.utc))
        return authorization_url

    def complete(self, state: str, authorization_response: str) -> AuthStatus:
        now = datetime.now(timezone.utc)
        with self._pending_lock:
            self._cleanup_expired_locked(now)
            pending = self.pending.pop(state, None)
        if pending is None:
            raise ValueError("OAuth state is missing or expired")

        expected_email, flow, created_at = self._pending_values(pending)
        if now - created_at > self.pending_ttl:
            raise ValueError("OAuth state is missing or expired")
        returned_states = parse_qs(urlparse(authorization_response).query).get("state", [])
        if returned_states != [state]:
            raise ValueError("OAuth state does not match the authorization response")

        flow.fetch_token(authorization_response=authorization_response)
        profile = build("gmail", "v1", credentials=flow.credentials, cache_discovery=False)
        authorized_email = str(profile.users().getProfile(userId="me").execute()["emailAddress"]).strip().lower()
        if authorized_email != expected_email:
            raise ValueError("authorized Gmail address does not match the configured verification email")

        now = datetime.now(timezone.utc)
        expires_at, estimated = self._refresh_expiry(flow, now)
        with oauth_file_transaction():
            atomic_write_text(PRIVATE_TOKEN_PATH, flow.credentials.to_json())
            atomic_write_text(
                PRIVATE_TOKEN_META_PATH,
                json.dumps(
                    {
                        "authorized_email": authorized_email,
                        "authorized_at": now.isoformat(),
                        "estimated_expires_at": expires_at.isoformat(),
                        "estimated": estimated,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return AuthStatus("valid", "Google authorization completed", authorized_email, expires_at, estimated)

    @staticmethod
    def _pending_values(pending: tuple[object, ...]) -> tuple[str, Flow, datetime]:
        """Accept legacy test/setup entries while recording timestamps for new attempts."""
        expected_email, flow = pending[:2]
        created_at = pending[2] if len(pending) == 3 else datetime.now(timezone.utc)
        return str(expected_email), flow, created_at  # type: ignore[return-value]

    def _cleanup_expired_locked(self, now: datetime) -> None:
        expired_states = [
            state
            for state, pending in self.pending.items()
            if now - self._pending_values(pending)[2] > self.pending_ttl
        ]
        for state in expired_states:
            self.pending.pop(state, None)

    @staticmethod
    def _is_valid_email(email: str) -> bool:
        if email.count("@") != 1 or len(email) > 254:
            return False
        local, domain = email.split("@")
        if not local or local.startswith(".") or local.endswith(".") or ".." in local:
            return False
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
            return False
        labels = domain.split(".")
        return len(labels) >= 2 and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels
        )

    @staticmethod
    def _refresh_expiry(flow: Flow, now: datetime) -> tuple[datetime, bool]:
        remaining = getattr(flow.oauth2session, "token", {}).get("refresh_token_expires_in")
        try:
            seconds = int(remaining)
            if seconds < 0:
                raise ValueError
        except (TypeError, ValueError):
            return now + timedelta(days=7), True
        return now + timedelta(seconds=seconds), False


def atomic_write_text(path: Path, text: str) -> None:
    """Replace a private text file without exposing a partial final file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _refresh_or_raise(credentials: Credentials) -> None:
    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        if "invalid_grant" in str(exc).lower():
            raise GmailAuthorizationRequired("Google authorization has expired or was revoked") from exc
        raise GmailTemporarilyUnavailable("Google token refresh failed temporarily") from exc
    except (OSError, TransportError) as exc:
        raise GmailTemporarilyUnavailable("Google token refresh is temporarily unavailable") from exc


def _token_digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _remember_token_source(credentials: Credentials, text: str) -> None:
    setattr(credentials, "_nexacard_token_digest", _token_digest(text))


def _load_valid_credentials_unlocked() -> Credentials:
    ensure_private_oauth_files()
    if not PRIVATE_TOKEN_PATH.is_file():
        raise GmailAuthorizationRequired("Google authorization has not been completed")
    try:
        source_text = PRIVATE_TOKEN_PATH.read_text(encoding="utf-8")
        credentials = Credentials.from_authorized_user_file(str(PRIVATE_TOKEN_PATH), SCOPES)
    except (OSError, ValueError) as exc:
        raise GmailAuthorizationRequired("stored Google credentials are invalid") from exc
    _remember_token_source(credentials, source_text)

    if credentials.valid:
        return credentials
    if not credentials.expired or not credentials.refresh_token:
        raise GmailAuthorizationRequired("Google refresh token is missing")

    _refresh_or_raise(credentials)
    refreshed_text = credentials.to_json()
    atomic_write_text(PRIVATE_TOKEN_PATH, refreshed_text)
    _remember_token_source(credentials, refreshed_text)
    return credentials


def load_valid_credentials() -> Credentials:
    """Load and optionally refresh one token transaction under the shared file lock."""
    with oauth_file_transaction():
        return _load_valid_credentials_unlocked()


def _refresh_credentials_after_unauthorized_unlocked(
    credentials: Credentials,
) -> Credentials:
    try:
        current_text = PRIVATE_TOKEN_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise GmailAuthorizationRequired("stored Google credentials are invalid") from exc
    source_digest = getattr(credentials, "_nexacard_token_digest", None)
    if source_digest and _token_digest(current_text) != source_digest:
        return _load_valid_credentials_unlocked()

    _refresh_or_raise(credentials)
    refreshed_text = credentials.to_json()
    atomic_write_text(PRIVATE_TOKEN_PATH, refreshed_text)
    _remember_token_source(credentials, refreshed_text)
    return credentials


def refresh_credentials_after_unauthorized(credentials: Credentials) -> Credentials:
    """Refresh once unless another process has already replaced these credentials."""
    with oauth_file_transaction():
        return _refresh_credentials_after_unauthorized_unlocked(credentials)


def _gmail_error_reasons(exc: HttpError) -> set[str]:
    try:
        payload = json.loads(bytes(exc.content).decode("utf-8"))
    except (AttributeError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        return set()
    error = payload.get("error") if isinstance(payload, dict) else None
    details = error.get("errors") if isinstance(error, dict) else None
    if not isinstance(details, list):
        return set()
    return {
        str(detail.get("reason", "")).strip().casefold()
        for detail in details
        if isinstance(detail, dict) and detail.get("reason")
    }


def raise_for_gmail_http_error(exc: HttpError, message: str) -> None:
    status = getattr(exc.resp, "status", None)
    if status == 401 or (
        status == 403
        and bool(_gmail_error_reasons(exc) & _AUTHORIZATION_403_REASONS)
    ):
        raise GmailAuthorizationRequired("Gmail authorization is required") from exc
    raise GmailTemporarilyUnavailable(message) from exc


def _profile_email_unlocked(credentials: Credentials) -> tuple[str, Credentials]:
    def request_profile(current_credentials: Credentials) -> str:
        service = build("gmail", "v1", credentials=current_credentials, cache_discovery=False)
        email = service.users().getProfile(userId="me").execute()["emailAddress"]
        return str(email).strip().lower()

    try:
        return request_profile(credentials), credentials
    except HttpError as exc:
        if getattr(exc.resp, "status", None) != 401:
            raise_for_gmail_http_error(
                exc, "Gmail profile is temporarily unavailable"
            )
    except (OSError, TransportError, KeyError, TypeError, ValueError) as exc:
        raise GmailTemporarilyUnavailable("Gmail profile is temporarily unavailable") from exc

    credentials = _refresh_credentials_after_unauthorized_unlocked(credentials)
    try:
        return request_profile(credentials), credentials
    except HttpError as exc:
        raise_for_gmail_http_error(exc, "Gmail profile is temporarily unavailable")
    except (OSError, TransportError, KeyError, TypeError, ValueError) as exc:
        raise GmailTemporarilyUnavailable("Gmail profile is temporarily unavailable") from exc


def _profile_email(credentials: Credentials) -> str:
    with oauth_file_transaction():
        return _profile_email_unlocked(credentials)[0]


def load_authorized_credentials(expected_email: str) -> Credentials:
    """Load credentials and require the live Gmail profile to match the operator setting."""
    with oauth_file_transaction():
        credentials = _load_valid_credentials_unlocked()
        authorized_email, credentials = _profile_email_unlocked(credentials)
        normalized = expected_email.strip().lower()
        if normalized and authorized_email != normalized:
            raise GmailAuthorizationRequired(
                "authorized Gmail address does not match the configured verification email"
            )
        return credentials


def _load_safe_metadata() -> dict[str, object]:
    if not PRIVATE_TOKEN_META_PATH.is_file():
        return {}
    try:
        raw = json.loads(PRIVATE_TOKEN_META_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if key in _META_KEYS}


def _metadata_expiry(metadata: dict[str, object]) -> datetime | None:
    raw = metadata.get("estimated_expires_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _sanitize_metadata() -> dict[str, object]:
    metadata = _load_safe_metadata()
    if PRIVATE_TOKEN_META_PATH.is_file():
        atomic_write_text(
            PRIVATE_TOKEN_META_PATH,
            json.dumps(metadata, ensure_ascii=False, indent=2),
        )
    return metadata


def get_auth_status(expected_email: str = "") -> AuthStatus:
    """Return a safe Gmail authorization status without reading token fields into metadata."""
    try:
        with oauth_file_transaction():
            metadata = _sanitize_metadata()
            credentials = _load_valid_credentials_unlocked()
            authorized_email, _credentials = _profile_email_unlocked(credentials)

            updated_metadata = dict(metadata)
            updated_metadata["authorized_email"] = authorized_email
            atomic_write_text(
                PRIVATE_TOKEN_META_PATH,
                json.dumps(updated_metadata, ensure_ascii=False, indent=2),
            )
    except GmailAuthorizationRequired as exc:
        return AuthStatus("reauthorize", str(exc))
    except GmailTemporarilyUnavailable as exc:
        return AuthStatus("unknown", str(exc))
    except OSError:
        return AuthStatus("unknown", "Gmail authorization metadata is temporarily unavailable")

    expiry = _metadata_expiry(updated_metadata)
    estimated = bool(updated_metadata.get("estimated"))
    if expected_email.strip() and authorized_email != expected_email.strip().lower():
        return AuthStatus(
            "mismatch",
            "authorized Gmail address does not match",
            authorized_email,
            expiry,
            estimated,
        )
    return AuthStatus(
        "valid",
        "Gmail authorization is available; access token refresh is automatic",
        authorized_email,
        expiry,
        estimated,
    )
