import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .errors import GmailAuthorizationRequired, GmailTemporarilyUnavailable
from .models import AuthStatus
from .settings import PRIVATE_TOKEN_META_PATH, PRIVATE_TOKEN_PATH, ensure_private_oauth_files


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_META_KEYS = {
    "authorized_email",
    "authorized_at",
    "estimated_expires_at",
    "estimated",
    "refresh_token_expires_in",
}


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


def load_valid_credentials() -> Credentials:
    """Load credentials and lazily renew an expired access token when possible."""
    ensure_private_oauth_files()
    if not PRIVATE_TOKEN_PATH.is_file():
        raise GmailAuthorizationRequired("Google authorization has not been completed")
    try:
        credentials = Credentials.from_authorized_user_file(str(PRIVATE_TOKEN_PATH), SCOPES)
    except (OSError, ValueError) as exc:
        raise GmailAuthorizationRequired("stored Google credentials are invalid") from exc

    if credentials.valid:
        return credentials
    if not credentials.expired or not credentials.refresh_token:
        raise GmailAuthorizationRequired("Google refresh token is missing")

    _refresh_or_raise(credentials)
    atomic_write_text(PRIVATE_TOKEN_PATH, credentials.to_json())
    return credentials


def _profile_email(credentials: Credentials) -> str:
    def request_profile() -> str:
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        email = service.users().getProfile(userId="me").execute()["emailAddress"]
        return str(email).strip().lower()

    try:
        return request_profile()
    except HttpError as exc:
        if getattr(exc.resp, "status", None) != 401:
            raise GmailTemporarilyUnavailable("Gmail profile is temporarily unavailable") from exc
    except (OSError, TransportError, KeyError, TypeError, ValueError) as exc:
        raise GmailTemporarilyUnavailable("Gmail profile is temporarily unavailable") from exc

    _refresh_or_raise(credentials)
    atomic_write_text(PRIVATE_TOKEN_PATH, credentials.to_json())
    try:
        return request_profile()
    except (HttpError, OSError, TransportError, KeyError, TypeError, ValueError) as exc:
        raise GmailTemporarilyUnavailable("Gmail profile is temporarily unavailable") from exc


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
        metadata = _sanitize_metadata()
    except OSError:
        return AuthStatus("unknown", "Gmail authorization metadata is temporarily unavailable")
    try:
        credentials = load_valid_credentials()
        authorized_email = _profile_email(credentials)
    except GmailAuthorizationRequired as exc:
        return AuthStatus("reauthorize", str(exc))
    except GmailTemporarilyUnavailable as exc:
        return AuthStatus("unknown", str(exc))

    updated_metadata = dict(metadata)
    updated_metadata["authorized_email"] = authorized_email
    atomic_write_text(
        PRIVATE_TOKEN_META_PATH,
        json.dumps(updated_metadata, ensure_ascii=False, indent=2),
    )

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
