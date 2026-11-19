"""Every control of the demo does what it says: sidebar, filters, row actions and menus, sheets, tabs,
keyboard shortcuts, the company set-up and the Settings page. Each test starts from a fresh demo
(tests in a module share one), so they run in file order and later ones see earlier changes."""

from __future__ import annotations

import re
from urllib.parse import quote

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

APPLIED = "greenhouse:Polderbyte:pb-1"


def rows(page):
    return page.locator("tr.row.job")


def test_the_sidebar_navigates_and_remembers_the_wishlist(watched):
    page = watched.page
    watched.go("/today")
    for label, path in (("Apply now", "/jobs?view=apply"), ("Scored", "/jobs?view=scored"),
                        ("Waiting", "/jobs?view=waiting"), ("Hidden", "/jobs?view=hidden"),
                        ("All applications", "/applications"), ("Needs follow-up", "/applications?view=stale"),
                        ("Companies", "/companies"), ("Runs", "/runs"), ("Today", "/today")):
        page.locator(".source-list a.item", has_text=label).first.click()
        watched.settle(100)
        assert path in page.url, label
        expect(page.locator(".source-list .item.selected", has_text=label)).to_have_count(1)
    group = page.locator(".wish-group")
    assert not group.evaluate("g => g.open")
    group.locator("summary").click()
    expect(page.locator(".wish-items .item.wish")).to_have_count(3)
    page.reload()
    assert group.evaluate("g => g.open")  # remembered
    page.locator(".wish-items .item.wish a").first.click()
    watched.settle()
    assert "company=" in page.url and rows(page).count() >= 1
    group.locator("summary").click()


def test_the_footer_settings_shortcuts_and_appearance(watched):
    page = watched.page
    watched.go("/today")
    page.locator(".footer-links a", has_text="Settings").click()
    watched.settle(100)
    assert page.url.endswith("/settings")
    page.locator(".sidebar-footer button[aria-label='Keyboard shortcuts']").click()
    expect(page.locator("dialog#shortcuts")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("dialog#shortcuts")).to_be_hidden()
    page.locator(".sidebar-footer [data-theme-choice='dark']").click()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"  # remembered
    page.keyboard.press("t")
    assert page.evaluate("document.documentElement.dataset.theme") != "dark"
    page.locator(".sidebar-footer [data-theme-choice='system']").click()
    page.keyboard.press("?")
    expect(page.locator("dialog#shortcuts")).to_be_visible()
    page.keyboard.press("Escape")


def test_the_apply_now_filters(watched):
    page = watched.page
    watched.go("/jobs?view=apply")
    assert rows(page).count() == 8
    assert page.locator("select[name='min_fit']").count() == 0  # the threshold is on Settings
    expect(page.locator(".page-head .subtitle")).to_contain_text("Fit 7+")
    page.select_option("select[name='odds']", "high")
    watched.settle()
    assert "odds=high" in page.url and 0 < rows(page).count() < 8
    expect(page.locator(".chip", has_text="Odds: High")).to_be_visible()
    page.locator(".chip", has_text="Odds: High").click()
    watched.settle()
    assert "odds=" not in page.url and rows(page).count() == 8
    page.select_option("select[name='group']", "")
    watched.settle()
    assert "group=" in page.url
    page.locator("details.more-filters summary").click()
    company = page.locator("input[name='company']")
    company.fill("Danubit")
    company.dispatch_event("change")
    watched.settle()
    expect(page.locator(".chip", has_text="Company: Danubit")).to_be_visible()
    assert rows(page).count() == 2  # a company's own view lists its roles by title
    expect(page.locator("h1")).to_have_text("Danubit")
    page.locator(".chip", has_text="Company").click()
    watched.settle()
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "search"
    page.keyboard.type("payments")  # filters as you type, across every page of results
    expect(page).to_have_url(re.compile("q=payments"))
    watched.settle()
    assert 1 <= rows(page).count() < 8 and page.evaluate("document.activeElement.id") == "search"
    expect(page.locator("#jobs-chips")).to_contain_text("Search: payments")
    page.keyboard.press("Enter")
    watched.settle()
    assert "q=payments" in page.url


def test_the_keyboard_on_jobs(watched):
    page = watched.page
    watched.go("/jobs?view=scored")
    page.keyboard.press("j")
    expect(page.locator("tr.row.job.kb-focus")).to_have_count(1)
    first = page.locator("tr.row.job.kb-focus").get_attribute("data-key")
    page.keyboard.press("j")
    page.keyboard.press("k")
    assert page.locator("tr.row.job.kb-focus").get_attribute("data-key") == first
    page.keyboard.press("x")
    assert page.locator("tr.row.job.kb-focus .row-select").is_checked()
    page.keyboard.press("x")
    page.keyboard.press("Enter")
    watched.settle()
    from urllib.parse import parse_qs, urlparse
    assert parse_qs(urlparse(page.url).query)["key"] == [first]


def test_row_actions_and_their_menu(watched):
    page = watched.page
    watched.go("/jobs?view=apply")
    row = rows(page).filter(has_text="Tulpwerk Systems").first
    row.hover()
    row.locator("details.row-menu summary").click()
    menu = row.locator(".menu")
    expect(menu.get_by_role("menuitem")).to_have_count(2)
    menu.get_by_role("menuitem", name=re.compile("Add Tulpwerk Systems to the wishlist")).click()
    watched.settle()
    row = rows(page).filter(has_text="Tulpwerk Systems").first
    expect(row.locator(".title-line .star-btn.on")).to_be_visible()
    expect(page.locator("#toast")).to_contain_text("Tulpwerk")
    # Hide, then undo with the toast.
    before = rows(page).count()
    row.hover()
    row.locator("button[aria-label='Hide']").click()
    watched.settle()
    expect(page.locator("#toast")).to_contain_text("Hidden")
    page.locator("#toast .toast-action").click()
    watched.settle()
    assert rows(page).count() == before
    # Hide, then undo with the keyboard.
    row = rows(page).filter(has_text="Tulpwerk Systems").first
    row.hover()
    row.locator("button[aria-label='Hide']").click()
    watched.settle()
    page.keyboard.press("Meta+z")
    watched.settle()
    assert rows(page).count() == before
    # Mark as applied: the role moves to Applications.
    badge = int(page.locator(".source-list a.item", has_text="All applications").locator(".badge").inner_text())
    row = rows(page).filter(has_text="Tulpwerk Systems").first
    row.hover()
    row.locator("button[aria-label='Mark as applied']").click()
    watched.settle()
    page.reload()
    assert rows(page).filter(has_text="Tulpwerk Systems").count() == 0
    assert int(page.locator(".source-list a.item", has_text="All applications").locator(".badge").inner_text()) == badge + 1


def test_job_detail(watched):
    page = watched.page
    watched.allowed_statuses = {400}  # Rescore in the demo: the scoring model is switched off
    watched.go(f"/job?key={quote(APPLIED)}")
    expect(page.locator("h1")).to_have_text("Graduate Backend Engineer (Go)")
    expect(page.locator(".gaps-line")).to_contain_text("Gaps:")
    page.locator(".head-menu summary").click()
    expect(page.locator(".head-menu .menu")).to_contain_text("Block Polderbyte")
    page.keyboard.press("Escape")
    notes = page.locator(".notes-form textarea")
    notes.fill("Ask about the payments team's on-call.")
    expect(page.locator(".autosave-status")).to_have_text(re.compile(r"Saved \d"), timeout=5000)
    page.reload()
    expect(notes).to_have_value("Ask about the payments team's on-call.")
    help_ = page.locator("#help")
    expect(help_).to_contain_text("Why do you want to join Polderbyte")
    help_.get_by_role("tab", name="Cover letter").click()
    watched.settle()
    expect(help_.get_by_role("tab", name="Cover letter")).to_have_attribute("aria-selected", "true")
    help_.get_by_role("tab", name="Chat").click()
    watched.settle()
    expect(help_.get_by_role("tab", name="Chat")).to_have_attribute("aria-selected", "true")
    page.locator("details.help-note summary").click()
    expect(page.locator("details.help-note p")).to_be_visible()
    # Rescore, in the demo, says why it cannot: in place, neutral, without a reload (2026-11-19).
    help_.get_by_role("tab", name="Chat").click()
    watched.settle()
    url = page.url
    page.locator(".scored-by .link-btn", has_text="Rescore").click()
    note = page.locator("#score-demo-note")
    expect(note).to_be_visible()
    expect(note).to_have_text("Rescoring is switched off in the demo: the scores shown were recorded.")
    assert note.get_attribute("role") == "note" and page.locator(".banner[role=alert]").count() == 0
    assert page.url == url and "error=" not in page.url
    expect(help_.get_by_role("tab", name="Chat")).to_have_attribute("aria-selected", "true")  # the tab is kept
    # The star on the header adds the company to the wishlist.
    watched.go("/jobs?view=apply")
    watched.go(rows(page).filter(has_text="Spreewerk").first.get_attribute("data-href"))
    page.locator(".job-head .star-toggle").click()
    watched.settle()
    expect(page.locator(".job-head .star-toggle.on")).to_be_visible()


def test_applications(watched):
    page = watched.page
    watched.go("/applications")
    page.locator(".stage-tab", has_text="Interviewing").click()
    watched.settle()
    assert page.locator("table.apps tbody tr").count() == 1
    page.locator(".stage-tab", has_text="All").click()
    watched.settle()
    expect(page.locator(".next.soon, .next.past").first).to_have_attribute("data-tip", re.compile("Due within|Overdue"))
    expect(page.locator(".row-hint[data-tip^='Has notes']").first).to_be_visible()
    page.locator("button.row-title", has_text="Carpatica Software").click()
    expect(page.locator("dialog#sheet")).to_be_visible()
    page.keyboard.press("Escape")
    page.locator("[data-open='add-sheet']").click()
    sheet = page.locator("dialog#add-sheet")
    expect(sheet).to_be_visible()
    sheet.locator("input[name='company']").fill("Nordlicht Labs")
    sheet.locator("input[name='role']").fill("Junior Backend Engineer")
    sheet.get_by_role("button", name=re.compile("^Add")).click()
    watched.settle()
    expect(page.locator("button.row-title", has_text="Nordlicht Labs")).to_be_visible()


def test_companies_search_tabs_and_set_up(watched):
    page = watched.page
    watched.go("/companies")
    page.locator(".stage-tab", has_text="Wishlist").click()
    watched.settle()
    assert page.locator("table.companies tbody tr").count() >= 3
    page.locator("#search").fill("polder")
    watched.settle(600)
    expect(page.locator("table.companies tbody tr", has_text="Polderbyte")).to_have_count(1)
    page.locator("#search").fill("grachtwise")
    watched.settle(600)
    expect(page.locator("table.companies", has_text="Generated list")).to_be_visible()
    page.locator("#search").fill("nordwind robotics")
    watched.settle(600)
    page.locator(".setup-empty button", has_text="Set it up").click()
    sheet = page.locator("dialog#sheet")
    expect(sheet).to_contain_text("Switched off in the demo: this would search the web")
    expect(sheet.locator("input[name='name']")).to_have_value("Nordwind Robotics")  # capitalised
    sheet.get_by_role("button", name="Look for it").click()
    expect(sheet).to_contain_text("could not be set up", timeout=15000)
    sheet.locator("details.setup-notes summary").click()
    expect(sheet.locator("details.setup-notes li").first).to_be_visible()
    assert "claude exited" not in sheet.inner_text() and ".." not in sheet.inner_text()
    sheet.get_by_role("button", name=re.compile("Check by hand weekly")).click()
    expect(sheet).to_contain_text("weekly list to check by hand")
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Find more companies").click()
    expect(page.locator("dialog#sheet")).to_contain_text("Find more companies")
    page.keyboard.press("Escape")
    watched.go("/today")
    expect(page.locator("section.group", has_text="Check by hand")).to_contain_text("Nordwind Robotics")


def test_runs(watched):
    page = watched.page
    watched.go("/runs")
    page.select_option("select[name='where']", "laptop")
    watched.settle()
    assert "where=laptop" in page.url and page.locator("table.runs tbody tr").count() == 1
    page.locator("table.runs tbody tr").first.click()
    expect(page.locator("dialog#sheet")).to_contain_text("Claude account")
    page.keyboard.press("Escape")


def test_settings_change_apply_now_and_hidden_days(watched):
    page = watched.page
    watched.go("/settings")
    status = page.locator("#settings-status")
    page.select_option("select[name='apply_min_fit']", "9")  # saved as it changes (2026-11-19)
    expect(status).to_contain_text("fit 9+")
    page.locator("input[name='hidden_days']").fill("10")
    page.keyboard.press("Tab")
    expect(status).to_contain_text("after 10 days")
    watched.go("/jobs?view=apply")
    expect(page.locator(".page-head .subtitle")).to_contain_text("Fit 9+")
    assert rows(page).count() < 8
    badge = page.locator(".source-list a.item", has_text="Apply now").locator(".badge").inner_text()
    assert int(badge) == rows(page).count()
    watched.go("/settings")
    assert page.locator("input[name='hidden_days']").input_value() == "10"
    page.select_option("select[name='apply_min_fit']", "7")
    expect(page.locator("#settings-status")).to_contain_text("fit 7+")
    page.locator(".theme-switch.large [data-theme-choice='light']").click()
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    page.locator(".theme-switch.large [data-theme-choice='system']").click()


def test_today_application_rows_open_their_application(watched):
    """2026-11-19: the whole row is the link, and it opens that application's role (or, for one the
    tool did not find, its sheet), not the Applications list."""
    page = watched.page
    watched.go("/today")
    section = page.locator("section.group", has=page.locator("h2", has_text="Applications"))
    row = section.locator("li.row-link", has_text="Polderbyte")
    detail = row.locator(".today-detail")
    detail.click()  # not the title: anywhere on the row
    watched.settle()
    from urllib.parse import parse_qs, urlparse
    assert urlparse(page.url).path == "/job" and parse_qs(urlparse(page.url).query)["key"] == [APPLIED]
    expect(page.locator("h1")).to_have_text("Graduate Backend Engineer (Go)")
    watched.go("/today")
    section.locator("li.row-link", has_text="to follow up").locator(".today-detail").click()
    watched.settle()
    assert "/applications?open=" in page.url
    expect(page.locator("dialog#sheet")).to_be_visible()
    expect(page.locator("dialog#sheet")).to_contain_text("Aalto Freight")
    page.keyboard.press("Escape")
    watched.go("/today")
    expect(page.locator("h2", has_text="The tool").locator(".group-count")).to_have_count(0)  # no "0"


def test_today_links(watched):
    page = watched.page
    watched.go("/today")
    links = page.locator("a.today-role")
    assert links.count() >= 3
    links.first.click()
    watched.settle()
    assert "/job?" in page.url
    page.locator("p.back a").click()
    watched.settle()
    assert page.url.endswith("/today")
