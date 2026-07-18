from dataclasses import dataclass
from pathlib import Path
import re
import threading

import config


_POOL_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_ROOT = Path(__file__).resolve().parent.parent
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


class AccountFormatError(ValueError):
    pass


def normalize_email_provider(value):
    provider = str(value or "NINEMALL").strip().upper() or "NINEMALL"
    if provider not in {"NINEMALL", "OUTLOOK"}:
        raise ValueError(f"unsupported email provider: {provider}")
    return provider


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
    def __init__(self, provider=None, source_file=None, root_dir=None):
        self.provider = normalize_email_provider(provider or config.EMAIL_PROVIDER)
        self.root_dir = Path(root_dir or _ROOT).resolve()
        default_name = config.NINEMALL_EMAIL_FILE if self.provider == "NINEMALL" else "emails.txt"
        raw_source = Path(source_file or default_name)
        self.source_file = raw_source if raw_source.is_absolute() else self.root_dir / raw_source
        if self.provider == "NINEMALL":
            self.used_file = self.root_dir / "mail_used_claude.txt"
            self.error_file = self.root_dir / "mail_error_claude.txt"
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
                released = (
                    self.provider == "NINEMALL"
                    and len(parts) == 2
                    and parts[1].lower() == "released"
                ) or (
                    self.provider == "OUTLOOK"
                    and len(parts) >= 3
                    and parts[-1].lower() == "released"
                )
                terminal_success = (
                    self.provider == "NINEMALL"
                    and len(parts) == 2
                    and parts[1].lower() == "ok"
                ) or (
                    self.provider == "OUTLOOK"
                    and len(parts) >= 3
                    and parts[-1].lower() == "ok"
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
        with path.open("a", encoding="utf-8") as handle:
            if self.provider == "OUTLOOK":
                handle.write(f"{account.email}----{account.password}----{status}\n")
            else:
                handle.write(f"{account.email}----{status}\n")

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
                if parts[0].lower() == email and parts[-1].lower() == "ok":
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
        with _POOL_LOCK:
            return self._load_accounts(limit=limit)

    def reserve_many(self, limit=None):
        if limit is not None and limit <= 0:
            return []
        with _POOL_LOCK:
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
        with _POOL_LOCK:
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
        with _POOL_LOCK:
            self._append_state(self.error_file, account, safe_reason)
            self._active_reservations.discard(account.email.lower())

    def release(self, account):
        email = account.email.lower()
        with _POOL_LOCK:
            if email not in self._active_reservations:
                return False
            if self._has_terminal_state(email):
                self._active_reservations.discard(email)
                return False
            self._append_state(self.used_file, account, "released")
            self._active_reservations.discard(email)
            return True
