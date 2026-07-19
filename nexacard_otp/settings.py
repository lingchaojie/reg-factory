import os
import shutil
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIR = ROOT / "nexacard_otp" / "private"
PRIVATE_CREDENTIALS_PATH = PRIVATE_DIR / "credentials.json"
PRIVATE_TOKEN_PATH = PRIVATE_DIR / "token.json"
PRIVATE_TOKEN_META_PATH = PRIVATE_DIR / "token.meta.json"
CHROME_PROFILE_DIR = PRIVATE_DIR / "chrome-profile"
LEGACY_CREDENTIALS_PATH = Path(r"D:\Gmail API李\credentials.json")
LEGACY_TOKEN_PATH = Path(r"D:\Gmail API李\token.json")


@dataclass(frozen=True)
class Settings:
    account: str
    password: str
    verification_email: str
    headless: bool
    chrome_path: Path
    page_timezone: ZoneInfo
    poll_interval_seconds: float
    max_attempts: int
    service_host: str
    service_port: int

    @property
    def browser_fingerprint(self) -> tuple[str, bool, str, str, str]:
        password_digest = sha256(self.password.encode("utf-8")).hexdigest()
        return (
            str(self.chrome_path),
            self.headless,
            self.account,
            self.verification_email,
            password_digest,
        )


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _positive_float(value: str, name: str) -> float:
    parsed = float(value)
    if not isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _positive_int(value: str, name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _trusted_chrome_path(candidate: Path) -> Path | None:
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved.name.casefold() != "chrome.exe" or not resolved.is_file():
        return None
    installed_hierarchy = tuple(
        part.casefold() for part in resolved.parts[-4:-1]
    )
    if installed_hierarchy != ("google", "chrome", "application"):
        return None
    return resolved


def discover_chrome(explicit: str = "") -> Path:
    if explicit:
        configured = Path(explicit)
        trusted = _trusted_chrome_path(configured)
        if trusted is None:
            raise FileNotFoundError(
                "NEXACARD_CHROME_PATH must name an installed Google Chrome chrome.exe"
            )
        return trusted

    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Google/Chrome/Application/chrome.exe"
        )
    for candidate in candidates:
        trusted = _trusted_chrome_path(candidate)
        if trusted is not None:
            return trusted
    raise FileNotFoundError("Google Chrome executable was not found")


def load_settings(env_path: Path = ROOT / ".env") -> Settings:
    file_values = _read_env(env_path)
    values = {key: value for key, value in os.environ.items() if key.startswith("NEXACARD_")}
    values.update(file_values)
    return Settings(
        account=values.get("NEXACARD_ACCOUNT", "").strip(),
        password=values.get("NEXACARD_PASSWORD", ""),
        verification_email=values.get("NEXACARD_VERIFICATION_EMAIL", "").strip().lower(),
        headless=_bool(values.get("NEXACARD_HEADLESS", "true")),
        chrome_path=discover_chrome(values.get("NEXACARD_CHROME_PATH", "")),
        page_timezone=ZoneInfo(values.get("NEXACARD_PAGE_TIMEZONE", "Asia/Shanghai")),
        poll_interval_seconds=_positive_float(
            values.get("NEXACARD_OTP_POLL_INTERVAL_SECONDS", "3"),
            "NEXACARD_OTP_POLL_INTERVAL_SECONDS",
        ),
        max_attempts=_positive_int(
            values.get("NEXACARD_OTP_MAX_ATTEMPTS", "100"),
            "NEXACARD_OTP_MAX_ATTEMPTS",
        ),
        service_host=values.get("NEXACARD_SERVICE_HOST", "127.0.0.1").strip(),
        service_port=_positive_int(
            values.get("NEXACARD_SERVICE_PORT", "8811"), "NEXACARD_SERVICE_PORT"
        ),
    )


def ensure_private_oauth_files() -> None:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    for source, destination in (
        (LEGACY_CREDENTIALS_PATH, PRIVATE_CREDENTIALS_PATH),
        (LEGACY_TOKEN_PATH, PRIVATE_TOKEN_PATH),
    ):
        if not destination.exists() and source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
