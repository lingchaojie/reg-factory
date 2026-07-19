"""Local, secret-safe HTTP interface for NexaCard OTP lookups."""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from .browser import NativeChromeManager
from .errors import (
    GmailAuthorizationRequired,
    GmailTemporarilyUnavailable,
    InvalidLookupInput,
    NexaCardLoginFailed,
    NexaCardPageError,
    NexaCardTransientError,
    OtpLookupTimedOut,
)
from .gmail_reader import GmailCodeReader
from .login import NexaCardLogin
from .lookup import OtpLookupService
from .matching import parse_lookup_input
from .settings import load_settings


class OtpRequest(BaseModel):
    card_number: str
    card_type: str
    order_created_at: str


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """Return the complete, deliberately small public error representation."""
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message},
    )


_MISSING = object()


def _state_value(app: FastAPI, name: str):
    return getattr(app.state, name, _MISSING)


def _restore_state_if_unchanged(app: FastAPI, name: str, written, previous) -> None:
    """Restore one lifecycle-owned assignment without erasing a newer owner."""
    if written is _MISSING or _state_value(app, name) is not written:
        return
    if previous is _MISSING:
        delattr(app.state, name)
    else:
        setattr(app.state, name, previous)


@asynccontextmanager
async def _service_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire services while respecting state owned by an enclosing lifespan."""
    lock: asyncio.Lock = app._nexacard_lifespan_lock
    previous_browser = _MISSING
    previous_lookup_service = _MISSING
    owned_browser = _MISSING
    written_browser = _MISSING
    written_lookup_service = _MISSING
    try:
        async with lock:
            previous_browser = _state_value(app, "browser")
            previous_lookup_service = _state_value(app, "lookup_service")
            if previous_lookup_service is _MISSING:
                browser = previous_browser
                if browser is _MISSING:
                    browser = NativeChromeManager()
                    owned_browser = browser
                reader = GmailCodeReader()
                login = NexaCardLogin(browser.login_lock, reader)
                lookup_service = OtpLookupService(browser, login)
                if previous_browser is _MISSING:
                    app.state.browser = browser
                    written_browser = browser
                app.state.lookup_service = lookup_service
                written_lookup_service = lookup_service
        yield
    finally:
        # Detach before close: a later lifespan must create a fresh generation,
        # never borrow a browser that is in the process of shutting down.
        async with lock:
            _restore_state_if_unchanged(
                app, "lookup_service", written_lookup_service, previous_lookup_service
            )
            _restore_state_if_unchanged(app, "browser", written_browser, previous_browser)
        if owned_browser is not _MISSING:
            await owned_browser.close()


def create_app() -> FastAPI:
    app = FastAPI(title="NexaCard OTP Service", lifespan=_service_lifespan)
    app._nexacard_lifespan_lock = asyncio.Lock()

    @app.exception_handler(Exception)
    async def unexpected_failure(_request: Request, _exception: Exception) -> JSONResponse:
        # Exceptions can carry request values and upstream credentials. Never
        # include either in an API response.
        return _error(500, "internal_error", "internal server error")

    @app.get("/health")
    async def health() -> dict[str, bool]:
        # This is deliberately a process liveness check, not a browser/login probe.
        return {"ok": True}

    @app.post("/v1/otp")
    async def get_otp(request: Request):
        # Keep a single immutable configuration snapshot for this request. It is
        # intentionally loaded before payload validation so every POST observes a
        # contemporaneous WebUI configuration without any subsequent reload.
        settings = load_settings()
        try:
            payload = await request.json()
            body = OtpRequest.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
            return _error(400, "invalid_request", "invalid request")

        try:
            lookup = parse_lookup_input(
                body.card_number,
                body.card_type,
                body.order_created_at,
                settings.page_timezone,
            )
            otp = await request.app.state.lookup_service.lookup(lookup, settings)
            return {"otp": otp}
        except InvalidLookupInput as exc:
            return _error(400, exc.code, "invalid lookup input")
        except (NexaCardLoginFailed, NexaCardPageError) as exc:
            return _error(502, exc.code, "NexaCard request failed")
        except (
            GmailAuthorizationRequired,
            GmailTemporarilyUnavailable,
            NexaCardTransientError,
        ) as exc:
            return _error(503, exc.code, "service temporarily unavailable")
        except OtpLookupTimedOut as exc:
            return _error(504, exc.code, "OTP lookup timed out")

    return app


app = create_app()
