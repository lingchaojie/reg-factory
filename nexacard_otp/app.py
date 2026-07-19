"""Local, secret-safe HTTP interface for NexaCard OTP lookups."""

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


@asynccontextmanager
async def _service_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire request services without starting Chrome until the first lookup."""
    browser = NativeChromeManager()
    lookup_service = None
    try:
        reader = GmailCodeReader()
        login = NexaCardLogin(browser.login_lock, reader)
        lookup_service = OtpLookupService(browser, login)
        app.state.browser = browser
        app.state.lookup_service = lookup_service
        yield
    finally:
        try:
            await browser.close()
        finally:
            # A process can run multiple test/server lifespans. Do not leave a
            # closed service reachable, and never clear a later lifecycle's state.
            if lookup_service is not None and getattr(app.state, "lookup_service", None) is lookup_service:
                delattr(app.state, "lookup_service")
            if getattr(app.state, "browser", None) is browser:
                delattr(app.state, "browser")


def create_app() -> FastAPI:
    app = FastAPI(title="NexaCard OTP Service", lifespan=_service_lifespan)

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
