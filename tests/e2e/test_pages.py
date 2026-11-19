"""Every page and sheet of the demo opens without errors, and nothing scrolls sideways, overlaps or
is cut off, at both widths in light and dark mode."""

from __future__ import annotations

from urllib.parse import quote

import pytest

from .conftest import SCHEMES, WIDTHS, Watched, layout_problems

pytestmark = pytest.mark.e2e

APPLIED = "greenhouse:Polderbyte:pb-1"  # the demo's role at the Interviewing stage, with helper drafts
PAGES = [
    "/today", "/jobs?view=apply", "/jobs?view=scored", "/jobs?view=waiting", "/jobs?view=hidden", "/jobs?view=all",
    "/jobs?view=all&company=polderbyte", "/jobs?view=scored&min_fit=8&odds=high", "/jobs?view=all&q=engineer&sort=posted",
    f"/job?key={quote(APPLIED)}", "/applications", "/applications?view=stale", "/applications?stage=interviewing",
    "/companies", "/companies?group=wishlist", "/companies?q=polder", "/companies?q=zzqq", "/runs",
    "/runs?where=laptop", "/settings",
]


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("scheme", SCHEMES)
def test_every_page_lays_out_cleanly(browser, base_url, width, scheme):
    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    w = Watched(context.new_page(), base_url)
    found = {}
    for path in PAGES:
        w.go(path)
        assert w.page.locator("h1").first.is_visible(), path
        problems = layout_problems(w.page)
        if problems:
            found[path] = problems
    # The first role on Apply now, opened from the list.
    w.go("/jobs?view=apply")
    w.go(w.page.locator("tr.row.job").first.get_attribute("data-href"))
    found.update({"job detail": p} for p in [layout_problems(w.page)] if p)
    context.close()
    assert not found, "\n".join(f"{path}: {p}" for path, items in found.items() for p in items)
    w.assert_clean()


SHEETS = [
    ("/today", "keyboard ?", lambda page: page.keyboard.press("?")),
    ("/today", "Add a company", lambda page: (page.locator(".wish-group summary").click(),
                                              page.locator(".wish-add").click())),
    ("/companies?q=zzqq", "Set it up", lambda page: page.locator(".setup-empty button").click()),
    ("/companies", "Find more companies", lambda page: page.get_by_role("button", name="Find more companies").click()),
    ("/runs", "a run", lambda page: page.locator("tr.row[hx-get^='/runs/']").first.click()),
    ("/applications", "an application", lambda page: page.locator("button.row-title").first.click()),
    ("/applications", "Add", lambda page: page.locator("[data-open='add-sheet']").click()),
]


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("scheme", SCHEMES)
def test_every_sheet_opens_and_lays_out_cleanly(browser, base_url, width, scheme):
    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    w = Watched(context.new_page(), base_url)
    found = []
    for path, name, open_it in SHEETS:
        w.go(path)
        open_it(w.page)
        w.settle()
        dialog = w.page.locator("dialog[open]")
        assert dialog.count() == 1, f"{name}: no sheet opened"
        box = dialog.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width, f"{name}: the sheet is off screen"
        found += [f"{name}: {p}" for p in layout_problems(w.page)]
        w.page.keyboard.press("Escape")
        w.settle(150)
        assert w.page.locator("dialog[open]").count() == 0, f"{name}: Escape did not close the sheet"
    context.close()
    assert not found, "\n".join(found)
    w.assert_clean()


def test_the_layout_check_finds_what_it_looks_for(watched):
    """The checks above would catch a real problem: an overlap, a clipped label, a sideways scroll."""
    watched.go("/jobs?view=scored")
    watched.page.evaluate("""() => {
      const bar = document.querySelector('.page-toolbar');
      const a = document.createElement('button'); a.className = 'btn'; a.textContent = 'First';
      const b = document.createElement('button'); b.className = 'btn'; b.textContent = 'Second';
      b.style.marginLeft = '-40px';
      const c = document.createElement('button'); c.className = 'btn'; c.textContent = 'A label far too long for its box';
      c.style.width = '60px'; c.style.overflow = 'hidden'; c.style.whiteSpace = 'nowrap';
      bar.append(a, b, c);
      const wide = document.createElement('div'); wide.style.width = '3000px'; wide.style.height = '1px';
      document.querySelector('main').append(wide);
      const footer = document.querySelector('.sidebar-footer .footer-row');
      const cut = document.createElement('button'); cut.textContent = 'Wide'; cut.style.width = '400px'; cut.style.flex = 'none';
      footer.append(cut);
    }""")
    problems = " ".join(layout_problems(watched.page))
    assert "overlap" in problems and "clipped" in problems and "scrolls sideways" in problems
    assert "cut off by its container" in problems  # the sidebar footer's appearance switch did this (2026-11-19)
