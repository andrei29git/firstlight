"""JavaScript-only career pages, rendered with Playwright (headless Chromium), for custom sites
marked `render = true` (built 2026-10-29; optional).

Rendering follows the same rules as every other request: robots.txt is checked for each page,
the host's delay (and any Crawl-delay) is kept, and the User-Agent is the tool's own. It is never
a way past bot protection: a page answering 403, 429 or another error is an error like any
other, and nothing is retried in a different guise. Rendered sites are slower, so they are
fetched from the laptop (`fetch_from = "local"`), where Chromium is installed.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from .base import SourceError
from .http import Http, rate_key

NAVIGATION_TIMEOUT_MS = 45_000
SETTLE_MS = 1_500  # after the load event, for the page's own scripts to fill in the jobs


class Renderer:
    """One headless browser for one company's fetch; close() when done (or use as a context)."""

    def __init__(self, http: Http, wait_for: str | None = None):
        self.http = http
        self.wait_for = wait_for  # a CSS selector that shows the jobs have loaded, if the site needs one
        self._playwright = None
        self._browser = None
        self._context = None
        self.seconds = 0.0  # time spent rendering, for the run summary

    def __enter__(self) -> "Renderer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _start(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise SourceError("rendering needs Playwright: run `uv sync` and `uv run playwright install chromium`") from e
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch()
        except Exception as e:  # no browser installed (as on GitHub's runners)
            self.close()
            raise SourceError(f"no Chromium for rendering ({str(e).splitlines()[0][:120]}): "
                              "run `uv run playwright install chromium`, or fetch this company from the laptop") from e
        self._context = self._browser.new_context(user_agent=self.http.user_agent, locale="en-US")

    def html(self, url: str) -> tuple[str, str]:
        """(final URL, rendered HTML) of a page robots.txt allows."""
        if not self.http.allowed_by_robots(url):
            raise SourceError(f"robots.txt disallows {url}")
        self.http._wait_for_host(rate_key(urlsplit(url).netloc))
        self._start()
        started = time.monotonic()
        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            if response is not None and response.status >= 400:
                raise SourceError(f"HTTP {response.status} from {url}")
            if self.wait_for:
                try:
                    page.wait_for_selector(self.wait_for, timeout=15_000)
                except Exception:
                    pass  # an empty listing has nothing to wait for
            else:
                page.wait_for_load_state("load", timeout=NAVIGATION_TIMEOUT_MS)
            page.wait_for_timeout(SETTLE_MS)
            return page.url, page.content()
        except SourceError:
            raise
        except Exception as e:
            raise SourceError(f"could not render {url}: {str(e).splitlines()[0][:160]}") from e
        finally:
            page.close()
            self.seconds += time.monotonic() - started

    def close(self) -> None:
        for thing in (self._context, self._browser):
            try:
                if thing is not None:
                    thing.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = self._browser = self._context = None
