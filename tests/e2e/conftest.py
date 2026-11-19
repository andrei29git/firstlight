"""End-to-end tests: the demo app in a real browser (Playwright, Chromium), at the widths and in the
colour schemes the app is designed for. They are marked `e2e`, left out of the default run (and the
pre-commit hook), and run in CI:

    uv run playwright install chromium   # once
    uv run pytest -m e2e
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest

playwright = pytest.importorskip("playwright.sync_api")

WIDTHS = (1160, 1440)
SCHEMES = ("light", "dark")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    """A fresh demo (its own temporary data) for each test module, served on a free port."""
    import uvicorn

    from jobhunt import cli, demo
    from jobhunt.web.app import create_app

    paths = cli.demo_paths()
    app = create_app(paths, runner=cli.demo_runner, demo=True)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    demo.remove(paths.root)


@pytest.fixture(scope="session")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium is not installed (uv run playwright install chromium): {e}")
        yield b
        b.close()


class Watched:
    """A page whose console errors, uncaught errors and failed requests are collected."""

    def __init__(self, page, base: str):
        self.page, self.base = page, base
        self.problems: list[str] = []
        self.allowed_statuses: set[int] = set()
        page.on("console", lambda m: self.problems.append(f"console {m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: self.problems.append(f"page error: {e}"))
        page.on("response", self._response)

    def _response(self, response) -> None:
        if response.status >= 400 and response.status not in self.allowed_statuses and response.url.startswith(self.base):
            self.problems.append(f"HTTP {response.status} {response.request.method} {response.url}")

    def go(self, path: str) -> None:
        self.page.goto(self.base + path, wait_until="networkidle")

    def settle(self, ms: int = 250) -> None:
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(ms)

    def assert_clean(self) -> None:
        problems, self.problems = self.problems, []
        assert not problems, "\n".join(problems)


@pytest.fixture
def watched(browser, base_url) -> Iterator[Watched]:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    w = Watched(page, base_url)
    yield w
    w.assert_clean()
    context.close()


LAYOUT_PROBLEMS = """
() => {
  const found = [];
  const doc = document.scrollingElement;
  if (doc.scrollWidth > window.innerWidth + 1) found.push(`page scrolls sideways: ${doc.scrollWidth} > ${window.innerWidth}`);
  const visible = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
  };
  const name = (el) => el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") +
    (el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\\s+/).join(".") : "") +
    ` "${(el.innerText || el.value || "").trim().slice(0, 40)}"`;
  // Text cut off without an ellipsis: a control or label whose content is wider than its box.
  for (const el of document.querySelectorAll("button, a.btn, .btn, select, .tag, .badge, .stage-tab, h1, h2, label.filter span, .tile-figure")) {
    if (!visible(el) || el.closest("dialog:not([open]), [hidden], .visually-hidden")) continue;
    const s = getComputedStyle(el);
    if (s.textOverflow === "ellipsis") continue;
    if (el.scrollWidth > el.clientWidth + 2 && s.overflowX !== "visible") found.push(`clipped: ${name(el)} (${el.scrollWidth} > ${el.clientWidth})`);
  }
  // Controls cut off by a container that hides its overflow (the sidebar's footer, a card).
  for (const el of document.querySelectorAll("button, a, select, input, .badge, .theme-switch")) {
    if (!visible(el) || el.closest("dialog:not([open]), [hidden], .visually-hidden, tr[hidden]")) continue;
    const r = el.getBoundingClientRect();
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.overflowX === "visible") continue;
      if (s.overflowX === "auto" || s.overflowX === "scroll") break;  // it scrolls: reachable
      const pr = p.getBoundingClientRect();
      if (r.right > pr.right + 2 || r.left < pr.left - 2) found.push(`cut off by its container: ${name(el)} in ${name(p)}`);
      break;
    }
  }
  // Controls that overlap their neighbours in the same toolbar, header, footer or row.
  const groups = document.querySelectorAll(".page-toolbar, .page-head, .job-head .tools, .sidebar-footer .footer-row, .row-actions, .sheet-actions, .filters, .stage-tabs");
  for (const g of groups) {
    if (!visible(g)) continue;
    const kids = [...g.querySelectorAll(":scope > *, :scope > * > button, :scope > * > a, :scope > * > select, :scope > * > label")].filter(visible);
    for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
      const a = kids[i], b = kids[j];
      if (a.contains(b) || b.contains(a)) continue;
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      const x = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
      const y = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
      if (x > 2 && y > 2) found.push(`overlap: ${name(a)} and ${name(b)}`);
    }
  }
  return [...new Set(found)];
}
"""


def layout_problems(page) -> list[str]:
    return page.evaluate(LAYOUT_PROBLEMS)
