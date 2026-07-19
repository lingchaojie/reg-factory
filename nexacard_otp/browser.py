import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

from playwright.async_api import BrowserContext, Page, async_playwright

from .settings import CHROME_PROFILE_DIR, Settings

PROXY_ENV_KEYS = frozenset(
    {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy"}
)


def direct_browser_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Return a child environment without standard proxy-routing variables."""
    environment = source if source is not None else dict(os.environ)
    return {
        key: value
        for key, value in environment.items()
        if key.casefold() not in PROXY_ENV_KEYS
    }


def chrome_args() -> list[str]:
    """Chrome switches that force all browser traffic to connect directly."""
    return ["--no-proxy-server", "--proxy-server=direct://", "--proxy-bypass-list=*"]


async def _default_playwright_factory():
    return await async_playwright().start()


PlaywrightFactory = Callable[[], Awaitable[Any]]


class NativeChromeManager:
    """Own the persistent, direct-network native Google Chrome context."""

    def __init__(self, playwright_factory: PlaywrightFactory = _default_playwright_factory) -> None:
        self._playwright_factory = playwright_factory
        self._playwright: Any | None = None
        self._context: BrowserContext | None = None
        self._fingerprint: tuple[str, bool, str, str, str] | None = None
        self._active_pages = 0
        self._transitioning = False
        self._condition = asyncio.Condition()
        self.login_lock = asyncio.Lock()

    async def _context_for(self, settings: Settings) -> BrowserContext:
        fingerprint = settings.browser_fingerprint
        previous_context: BrowserContext | None = None
        detached_context = False

        async with self._condition:
            while self._transitioning:
                await self._condition.wait()
            if self._context is not None and self._fingerprint == fingerprint:
                self._active_pages += 1
                return self._context

            self._transitioning = True

        try:
            async with self._condition:
                while self._active_pages:
                    await self._condition.wait()
                previous_context = self._context
                self._context = None
                self._fingerprint = None
                detached_context = True

            if previous_context is not None:
                await previous_context.close()
            if self._playwright is None:
                self._playwright = await self._playwright_factory()
            CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE_DIR),
                executable_path=str(settings.chrome_path),
                headless=settings.headless,
                args=chrome_args(),
                env=direct_browser_env(),
            )
        except BaseException:
            if detached_context:
                await self._stop_playwright_after_failed_launch()
            raise
        else:
            async with self._condition:
                self._context = context
                self._fingerprint = fingerprint
                self._active_pages += 1
                self._transitioning = False
                self._condition.notify_all()
            return context
        finally:
            async with self._condition:
                if self._transitioning:
                    self._transitioning = False
                    self._condition.notify_all()

    async def _stop_playwright_after_failed_launch(self) -> None:
        playwright = self._playwright
        self._playwright = None
        if playwright is not None:
            try:
                await playwright.stop()
            except BaseException:
                pass

    async def _release_page(self) -> None:
        async with self._condition:
            self._active_pages -= 1
            self._condition.notify_all()

    @asynccontextmanager
    async def page(self, settings: Settings) -> AsyncIterator[Page]:
        context = await self._context_for(settings)
        try:
            browser_page = await context.new_page()
        except BaseException:
            await self._release_page()
            raise

        try:
            yield browser_page
        finally:
            try:
                await browser_page.close()
            finally:
                await self._release_page()

    async def close(self) -> None:
        context: BrowserContext | None = None
        playwright: Any | None = None
        detached_context = False
        async with self._condition:
            while self._transitioning:
                await self._condition.wait()
            self._transitioning = True

        try:
            async with self._condition:
                while self._active_pages:
                    await self._condition.wait()
                context = self._context
                playwright = self._playwright
                self._context = None
                self._playwright = None
                self._fingerprint = None
                detached_context = True

            if context is not None:
                await context.close()
        finally:
            try:
                if detached_context and playwright is not None:
                    await playwright.stop()
            finally:
                async with self._condition:
                    self._transitioning = False
                    self._condition.notify_all()
