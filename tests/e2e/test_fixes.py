"""Checks for the fixes of 2026-11-19: More filters closes like a menu, deadlines say "Due today"
and turn red once passed, answer boxes grow instead of scrolling, the wheel over Job detail's
sidebar scrolls the page, top-right buttons end at the table's edge, and the demo's footer line
is not cut off."""

from __future__ import annotations

import re
from urllib.parse import quote

import pytest
from playwright.sync_api import expect

from .conftest import Watched

pytestmark = pytest.mark.e2e

APPLIED = "greenhouse:Polderbyte:pb-1"  # drafted answers, a stage history and a chat


def rgb(text: str) -> tuple[int, ...]:
    return tuple(int(float(n)) for n in re.findall(r"[\d.]+", text)[:3])


def test_more_filters_closes_on_escape_and_outside_and_returns_focus(watched):
    page = watched.page
    watched.go("/jobs?view=scored")
    summary = page.locator("details.more-filters summary")
    summary.click()
    expect(page.locator("details.more-filters")).to_have_attribute("open", "")
    page.locator(".more-panel select[name=status]").focus()
    page.keyboard.press("Escape")
    expect(page.locator("details.more-filters[open]")).to_have_count(0)
    assert summary.evaluate("s => document.activeElement === s"), "focus goes back to More filters"
    summary.click()
    expect(page.locator("details.more-filters[open]")).to_have_count(1)
    page.locator("h1").click()  # a click outside
    expect(page.locator("details.more-filters[open]")).to_have_count(0)


def test_deadlines_due_today_and_passed(watched):
    page = watched.page
    watched.go("/today")
    deadlines = page.locator("section", has_text="Deadlines in the next 7 days")
    expect(deadlines.locator(".today-when", has_text="Due today")).to_have_count(1)
    watched.go("/jobs?view=scored")
    today = page.locator("tr.row.job .tag.due", has_text="Due today")
    expect(today).to_have_count(1)
    passed = page.locator("tr.row.job .tag.due.past")
    expect(passed).to_have_text("Deadline passed")
    red, neutral = rgb(passed.evaluate("e => getComputedStyle(e).color")), rgb(today.evaluate("e => getComputedStyle(e).color"))
    assert red[0] > red[1] + 60 and red[0] > red[2] + 60, f"a passed deadline is red, not {red}"
    assert red != neutral
    # On Job detail too.
    watched.go(page.locator("tr.row.job", has=page.locator(".tag.due", has_text="Due today")).first.get_attribute("data-href"))
    expect(page.locator(".facts dd.deadline")).to_have_text("Today")


def test_answer_and_letter_boxes_grow_with_their_text(watched):
    page = watched.page
    watched.go(f"/job?key={quote(APPLIED)}")
    answers = page.locator("#help textarea.answer")
    answers.first.wait_for()
    assert answers.count() >= 2
    for i in range(answers.count()):
        fits = answers.nth(i).evaluate("a => a.scrollHeight <= a.clientHeight + 2")
        assert fits, f"answer {i + 1} scrolls inside its box"
    # Typing more makes the box taller, not scrollable.
    first = answers.first
    before = first.bounding_box()["height"]
    first.fill(first.input_value() + "\n\nOne more paragraph.\nAnd another line.")
    page.wait_for_timeout(100)
    assert first.bounding_box()["height"] > before + 20
    assert first.evaluate("a => a.scrollHeight <= a.clientHeight + 2")
    # The cover letter, on the role that has one.
    watched.go("/jobs?view=apply")
    watched.go(page.locator("tr.row.job", has_text="Rivoli Ledger").first.get_attribute("data-href"))
    page.locator("#help [role=tab]", has_text="Cover letter").click()
    letter = page.locator("#help textarea.letter")
    letter.wait_for()
    assert letter.evaluate("a => a.scrollHeight <= a.clientHeight + 2"), "the letter scrolls inside its box"


def test_the_wheel_over_the_job_sidebar_scrolls_the_page(browser, base_url):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    w = Watched(context.new_page(), base_url)
    w.go(f"/job?key={quote(APPLIED)}")
    side = w.page.locator(".job-side")
    assert "tall" not in (side.get_attribute("class") or ""), "it fits the window, so it is no scroll box"
    box = side.bounding_box()
    w.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 60)
    w.page.mouse.wheel(0, 700)
    w.page.wait_for_timeout(300)
    assert w.page.evaluate("window.scrollY") > 300, "the page scrolled under the sidebar"
    # In a window too short for it, the sidebar scrolls on its own.
    w.page.set_viewport_size({"width": 1440, "height": 420})
    w.page.wait_for_timeout(200)
    assert "tall" in (side.get_attribute("class") or "")
    assert side.evaluate("s => s.scrollHeight > s.clientHeight")
    context.close()
    w.assert_clean()


@pytest.mark.parametrize("width", (1160, 1440))
def test_top_right_buttons_end_at_the_tables_edge(browser, base_url, width):
    context = browser.new_context(viewport={"width": width, "height": 900})
    w = Watched(context.new_page(), base_url)
    for path, button, table in (("/applications", "[data-open='add-sheet']", ".card"),
                                ("/companies", "button:has-text('Find more companies')", "table.companies"),
                                ("/runs", ".page-head .head-action", "table.runs")):
        w.go(path)
        if not w.page.locator(button).count():  # the demo has no GitHub repository, so Runs has no button
            continue
        right = lambda b: b["x"] + b["width"]  # noqa: E731
        b, t = w.page.locator(button).first.bounding_box(), w.page.locator(table).first.bounding_box()
        assert abs(right(b) - right(t)) <= 1, f"{path}: the button ends at {right(b)}, the table at {right(t)}"
    # Runs: the button sits in the header row, beside the title, not on a line of its own.
    w.go("/runs")
    assert w.page.locator(".page-head + .page-toolbar").count() == 0
    context.close()
    w.assert_clean()


def test_the_demo_footer_line_is_not_cut_off(watched):
    watched.go("/today")
    text = watched.page.locator(".sidebar-footer .sync-text")
    assert text.evaluate("t => t.scrollWidth <= t.clientWidth"), text.inner_text()


def test_chat_replies_keep_their_paragraphs_lists_and_bold(watched):
    page = watched.page
    watched.go(f"/job?key={quote(APPLIED)}")
    page.locator("#help [role=tab]", has_text="Chat").click()
    reply = page.locator("#help .bubble.theirs .bubble-text").first
    reply.wait_for()
    expect(reply.locator("strong")).to_have_count(1)
    expect(reply.locator("ul li")).to_have_count(3)
    assert reply.locator("p").count() == 2


def test_copy_all_waits_for_a_draft_and_the_posting_has_headings_and_lists(watched):
    page = watched.page
    watched.go("/jobs?view=scored")
    watched.go(page.locator("tr.row.job", has_text="Spreewerk").first.get_attribute("data-href"))
    page.locator("#help .paste-form textarea").fill("1. Why us? (max 120 words)\n2. Are you eligible to work in Germany?")
    page.locator("#help .paste-form button").click()
    watched.settle()
    items = page.locator("#help .review-item")
    expect(items).to_have_count(2)
    expect(items.first.locator("textarea")).to_have_value("Why us?")  # the limit leaves the text
    expect(items.first.locator(".tag")).to_have_text("120 words")  # and stays as its tag
    one_line = items.nth(1).locator("textarea").bounding_box()["height"]
    assert one_line < 48, f"a one-line question gets a one-line box, not {one_line}px"
    delete = rgb(items.first.locator("button", has_text="Delete").evaluate("e => getComputedStyle(e).color"))
    assert abs(delete[0] - delete[1]) < 40, f"Delete is quiet grey until hovered, not {delete}"
    page.get_by_role("button", name="Looks right").click()
    watched.settle()
    expect(page.locator("#help button", has_text="Copy all")).to_be_disabled()
    posting = page.locator(".posting-text")
    assert posting.locator("h3.text-heading").count() >= 1 and posting.locator("ul li").count() >= 3
