from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import threading
import uuid

import config
from common.interprocess_lock import InterprocessFileLock


_POOL_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_ROOT = Path(__file__).resolve().parent.parent
_PURPOSES = {"claude", "claude_api"}
_POOL_LOCK_NAME = ".claude_email_pool.lock"
_POOL_JOURNAL_NAME = ".claude_email_pool.journal"
_SAFE_REASONS = {
    "http_400",
    "http_401",
    "http_403",
    "invalid_json",
    "invalid_response",
    "magic_link_timeout",
    "network_error",
    "no_session_key",
    "onboarding_stuck",
    "phone_verify_failed",
    "registration_error",
    "timeout",
    "transient_http",
    "unexpected_http",
}
_SAFE_REASONS.update({
    "mail_timeout",
    "verification_artifact_not_found",
    "magic_link_invalid",
    "verification_rejected",
    "personal_account_not_available",
    "console_not_reached",
})


class AccountFormatError(ValueError):
    pass


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
                raise OSError("incomplete ledger write")
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


def _atomic_rewrite(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    committed = False
    try:
        _write_fsynced(temporary, payload)
        os.replace(temporary, path)
        committed = True
        _fsync_directory(path.parent)
    finally:
        if not committed:
            temporary.unlink(missing_ok=True)


def _journal_path(root_dir):
    return Path(root_dir) / _POOL_JOURNAL_NAME


def _recover_pool_transaction(root_dir):
    root = Path(root_dir).resolve()
    journal = _journal_path(root)
    if not journal.exists():
        return
    transaction = json.loads(journal.read_text(encoding="utf-8"))
    if transaction.get("version") != 1:
        raise RuntimeError("unsupported Claude email pool journal")
    for entry in transaction.get("files", ()):
        name = entry.get("name")
        if not name or Path(name).name != name:
            raise RuntimeError("invalid Claude email pool journal path")
        target = root / name
        original_size = int(entry["size"])
        if entry.get("existed"):
            if not target.exists():
                raise RuntimeError("Claude email ledger missing during recovery")
            with target.open("r+b") as handle:
                handle.truncate(original_size)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            target.unlink(missing_ok=True)
    _fsync_directory(root)
    journal.unlink()
    _fsync_directory(root)


def _prepare_pool_transaction(root_dir, paths):
    root = Path(root_dir).resolve()
    journal = _journal_path(root)
    entries = []
    for path in paths:
        resolved = Path(path).resolve()
        if resolved.parent != root:
            raise RuntimeError("Claude email ledger is outside its pool root")
        entries.append({
            "name": resolved.name,
            "existed": resolved.exists(),
            "size": resolved.stat().st_size if resolved.exists() else 0,
        })
    payload = json.dumps({"version": 1, "files": entries}).encode("utf-8")
    _atomic_rewrite(journal, payload)


def _finish_pool_transaction(root_dir):
    journal = _journal_path(root_dir)
    journal.unlink(missing_ok=True)
    _fsync_directory(Path(root_dir))


@contextmanager
def _locked_pool(root_dir):
    root = Path(root_dir).resolve()
    with _POOL_LOCK:
        with InterprocessFileLock(root / _POOL_LOCK_NAME):
            _recover_pool_transaction(root)
            yield


def normalize_email_provider(value):
    provider = str(value or "NINEMALL").strip().upper() or "NINEMALL"
    if provider not in {"NINEMALL", "OUTLOOK"}:
        raise ValueError(f"unsupported email provider: {provider}")
    return provider


def normalize_purpose(value):
    purpose = str(value or "claude").strip().lower() or "claude"
    if purpose not in _PURPOSES:
        raise ValueError(f"unsupported Claude email purpose: {purpose}")
    return purpose


@dataclass(frozen=True)
class ClaudeEmailAccount:
    provider: str
    email: str
    password: str
    client_id: str
    refresh_token: str
    source_file: str = ""
    source_line: int = 0


class ClaudeEmailAccountStore:
    def __init__(self, provider=None, source_file=None, root_dir=None, purpose="claude"):
        self.provider = normalize_email_provider(provider or config.EMAIL_PROVIDER)
        self.purpose = normalize_purpose(purpose)
        self.root_dir = Path(root_dir or _ROOT).resolve()
        default_name = (
            config.NINEMALL_EMAIL_FILE
            if self.provider == "NINEMALL"
            else "emails.txt"
        )
        raw_source = Path(source_file or default_name)
        self.source_file = (
            raw_source if raw_source.is_absolute() else self.root_dir / raw_source
        )
        if self.provider == "NINEMALL":
            suffix = "claude" if self.purpose == "claude" else "claude_api"
            self.used_file = self.root_dir / f"mail_used_{suffix}.txt"
            self.error_file = self.root_dir / f"mail_error_{suffix}.txt"
        elif self.purpose == "claude_api":
            self.used_file = self.root_dir / "emails_used_claude_api.txt"
            self.error_file = self.root_dir / "emails_error_claude_api.txt"
        else:
            self.used_file = self.root_dir / "emails_used.txt"
            self.error_file = self.root_dir / "emails_error.txt"
        self._active_reservations = set()

    @staticmethod
    def parse_line(line, provider, line_number=0, source_file=""):
        provider = normalize_email_provider(provider)
        parts = [part.strip() for part in line.strip().split("----")]
        if provider == "NINEMALL":
            valid = len(parts) == 4 and all(parts)
            if not valid:
                raise AccountFormatError(f"invalid NINEMALL account at line {line_number}")
            email, password, client_id, refresh_token = parts
        else:
            if len(parts) < 2 or not parts[0] or not parts[1]:
                raise AccountFormatError(f"invalid OUTLOOK account at line {line_number}")
            email, password = parts[:2]
            refresh_token = parts[2] if len(parts) >= 3 else ""
            client_id = parts[3] if len(parts) >= 4 else ""
        if not _EMAIL_RE.match(email):
            raise AccountFormatError(f"invalid email address at line {line_number}")
        return ClaudeEmailAccount(
            provider, email, password, client_id, refresh_token,
            str(source_file), line_number,
        )

    def _blocked(self):
        terminal = set()
        two_field_events = (
            self.provider == "NINEMALL" or self.purpose == "claude_api"
        )
        if self.error_file.exists():
            for raw in self.error_file.read_text(encoding="utf-8").splitlines():
                value = raw.strip()
                if value and not value.startswith("#"):
                    terminal.add(
                        value.split("----", 1)[0].strip().lower()
                    )
        used_events = []
        if self.used_file.exists():
            for raw in self.used_file.read_text(encoding="utf-8").splitlines():
                value = raw.strip()
                if not value or value.startswith("#"):
                    continue
                parts = [part.strip() for part in value.split("----")]
                email = parts[0].lower()
                legacy_outlook_success = (
                    self.provider == "OUTLOOK"
                    and self.purpose == "claude"
                    and len(parts) == 2
                )
                event_status = parts[-1].lower()
                released = (
                    two_field_events
                    and len(parts) == 2
                    and event_status == "released"
                ) or (
                    self.provider == "OUTLOOK"
                    and self.purpose == "claude"
                    and len(parts) >= 3
                    and event_status == "released"
                )
                terminal_success = legacy_outlook_success or (
                    two_field_events
                    and len(parts) == 2
                    and event_status == "ok"
                ) or (
                    self.provider == "OUTLOOK"
                    and self.purpose == "claude"
                    and len(parts) >= 3
                    and event_status == "ok"
                )
                used_events.append((email, released))
                if terminal_success:
                    terminal.add(email)

        blocked = set(terminal)
        for email, released in used_events:
            if email in terminal:
                blocked.add(email)
            elif released:
                blocked.discard(email)
            else:
                blocked.add(email)
        return blocked

    def _append_state(self, path, account, status):
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_bytes() if path.exists() else b""
        separator = b"" if not existing or existing.endswith((b"\n", b"\r")) else os.linesep.encode()
        if self.provider == "OUTLOOK" and self.purpose == "claude":
            line = f"{account.email}----{account.password}----{status}"
        else:
            line = f"{account.email}----{status}"
        payload = existing + separator + line.encode("utf-8") + os.linesep.encode()
        _atomic_rewrite(path, payload)

    def _has_terminal_state(self, email):
        if self.error_file.exists():
            for raw in self.error_file.read_text(encoding="utf-8").splitlines():
                value = raw.strip()
                if value and not value.startswith("#"):
                    if value.split("----", 1)[0].strip().lower() == email:
                        return True
        if self.used_file.exists():
            for raw in self.used_file.read_text(encoding="utf-8").splitlines():
                value = raw.strip()
                if not value or value.startswith("#"):
                    continue
                parts = [part.strip() for part in value.split("----")]
                if parts[0].lower() != email:
                    continue
                if (
                    self.provider == "OUTLOOK"
                    and self.purpose == "claude"
                    and len(parts) == 2
                ):
                    return True
                if parts[-1].lower() == "ok":
                    return True
        return False

    def _load_accounts(self, limit=None):
        if not self.source_file.exists():
            return []
        selected = []
        for line_number, raw in enumerate(
            self.source_file.read_text(encoding="utf-8").splitlines(), 1
        ):
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            try:
                account = self.parse_line(
                    value, self.provider, line_number, self.source_file
                )
            except AccountFormatError as exc:
                print(f"  [email-file] {exc}")
                continue
            selected.append(account)
            if limit is not None and len(selected) >= limit:
                break
        return selected

    def load_many(self, limit=None):
        with _locked_pool(self.root_dir):
            return self._load_accounts(limit=limit)

    def reserve_many(self, limit=None):
        if limit is not None and limit <= 0:
            return []
        with _locked_pool(self.root_dir):
            blocked = self._blocked()
            selected = []
            for account in self._load_accounts():
                if account.email.lower() in blocked:
                    continue
                self._append_state(self.used_file, account, "reserved")
                email = account.email.lower()
                blocked.add(email)
                self._active_reservations.add(email)
                selected.append(account)
                if limit is not None and len(selected) >= limit:
                    break
            return selected

    def reserve_one(self):
        selected = self.reserve_many(limit=1)
        return selected[0] if selected else None

    def mark_used(self, account):
        with _locked_pool(self.root_dir):
            self._append_state(self.used_file, account, "ok")
            self._active_reservations.discard(account.email.lower())

    def mark_error(self, account, reason):
        raw = str(reason or "unknown").lower()
        if "401" in raw:
            safe_reason = "http_401"
        elif "400" in raw:
            safe_reason = "http_400"
        elif raw in _SAFE_REASONS:
            safe_reason = raw
        else:
            safe_reason = "registration_error"
        with _locked_pool(self.root_dir):
            self._append_state(self.error_file, account, safe_reason)
            self._active_reservations.discard(account.email.lower())

    def release(self, account):
        email = account.email.lower()
        with _locked_pool(self.root_dir):
            if email not in self._active_reservations:
                return False
            if self._has_terminal_state(email):
                self._active_reservations.discard(email)
                return False
            self._append_state(self.used_file, account, "released")
            self._active_reservations.discard(email)
            return True


def reserve_shared_claude_account(
    provider,
    purposes,
    source_file=None,
    root_dir=None,
):
    requested = tuple(dict.fromkeys(normalize_purpose(purpose) for purpose in purposes))
    if not requested:
        raise ValueError("at least one Claude email purpose is required")
    stores = {
        purpose: ClaudeEmailAccountStore(
            provider=provider,
            source_file=source_file,
            root_dir=root_dir,
            purpose=purpose,
        )
        for purpose in requested
    }
    base = stores[requested[0]]
    with _locked_pool(base.root_dir):
        blocked = {purpose: store._blocked() for purpose, store in stores.items()}
        for account in base._load_accounts():
            email = account.email.lower()
            if any(email in blocked[purpose] for purpose in requested):
                continue
            paths = [store.used_file for store in stores.values()]
            _prepare_pool_transaction(base.root_dir, paths)
            try:
                for store in stores.values():
                    store._append_state(store.used_file, account, "reserved")
            except BaseException:
                _recover_pool_transaction(base.root_dir)
                raise
            _finish_pool_transaction(base.root_dir)
            for store in stores.values():
                store._active_reservations.add(email)
            return account, stores
    return None
