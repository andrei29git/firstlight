"""Layout rules the page checks alone would not catch (2026-11-19): Job detail's two columns leave no
gap under a short card, More filters lines its controls up on one edge and stays in the window, and
the sidebar's list shows that it scrolls."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from playwright.sync_api import expect

from .conftest import SCHEMES, Watched

pytestmark = pytest.mark.e2e

APPLIED = "greenhouse:Polderbyte:pb-1"  # the role with drafted answers and a stage history
ROW_GAP = 16  # the main column's gap between cards


def boxes(page, *selectors):
    return [page.locator(s).first.bounding_box() for s in selectors]


def first_apply_role(w: Watched) -> str:
    w.go("/jobs?view=apply")
    return w.page.locator("tr.row.job").first.get_attribute("data-href")


@pytest.mark.parametrize("width", (1160, 1440))
@pytest.mark.parametrize("scheme", SCHEMES)
def test_job_detail_columns_leave_no_gap(browser, base_url, width, scheme):
    context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    w = Watched(context.new_page(), base_url)
    for path in (first_apply_role(w), f"/job?key={quote(APPLIED)}"):
        w.go(path)
        w.page.locator("#help .card-title").first.wait_for()
        w.settle(300)
        score, help_, posting, side = boxes(w.page, ".job-score", ".job-help #help", ".job-posting", ".job-side")
        # The main column's cards follow each other at the row gap, whatever the sidebar's height.
        assert abs(help_["y"] - (score["y"] + score["height"]) - ROW_GAP) <= 1, (path, score, help_)
        assert abs(posting["y"] - (help_["y"] + help_["height"]) - ROW_GAP) <= 1, (path, help_, posting)
        # Two columns, tops aligned; the main column at a reading width, the sidebar fixed.
        assert side["x"] > score["x"] + score["width"], path
        assert abs(side["y"] - score["y"]) <= 1, path
        assert score["width"] <= 740 and 260 <= side["width"] <= 300, (path, score["width"], side["width"])
        assert abs(help_["width"] - score["width"]) <= 1 and abs(posting["width"] - score["width"]) <= 1, path
        # The posting text uses its card's width, not 70% of it.
        text = w.page.locator(".job-posting .posting-text").bounding_box()
        assert text["width"] >= posting["width"] - 2 * 24 - 2, (path, text["width"], posting["width"])
        # The sidebar stays in view while the page scrolls.
        w.page.mouse.wheel(0, 5000)
        w.settle(300)
        top = w.page.locator(".job-side").bounding_box()["y"]
        assert 0 <= top <= 24, (path, top)
    context.close()
    w.assert_clean()


def test_job_detail_stacks_in_a_narrow_window_and_notes_grow(browser, base_url):
    context = browser.new_context(viewport={"width": 960, "height": 900})
    w = Watched(context.new_page(), base_url)
    w.go(first_apply_role(w))
    w.page.locator("#help .card-title").first.wait_for()
    score, side, help_ = boxes(w.page, ".job-score", ".job-side", ".job-help #help")
    assert side["y"] >= score["y"] + score["height"] and side["y"] + side["height"] <= help_["y"], "the sidebar sits under the score"
    assert abs(side["x"] - score["x"]) <= 1
    notes = w.page.locator(".notes-form textarea")
    notes.fill("")
    empty = notes.bounding_box()["height"]
    assert 50 <= empty <= 90, empty  # about three lines, not a tall empty box
    notes.fill("\n".join(f"Line {n}" for n in range(1, 9)))
    w.settle(100)
    assert notes.bounding_box()["height"] > empty + 60  # it grew with the text
    context.close()
    w.assert_clean()


@pytest.mark.parametrize("width", (1160, 1440))
@pytest.mark.parametrize("view", ("apply", "scored", "all"))
def test_more_filters_controls_share_a_left_edge(browser, base_url, width, view):
    context = browser.new_context(viewport={"width": width, "height": 900})
    w = Watched(context.new_page(), base_url)
    w.go(f"/jobs?view={view}")
    w.page.locator("details.more-filters summary").click()
    w.settle(200)
    panel = w.page.locator(".more-panel")
    controls = panel.locator(".filter select, .filter input")
    edges = {(round(b["x"]), round(b["width"]), round(b["height"])) for b in
             (controls.nth(i).bounding_box() for i in range(controls.count()))}
    assert len(edges) == 1, f"controls differ in left edge, width or height: {edges}"
    labels = panel.locator(".filter > span")
    assert len({round(labels.nth(i).bounding_box()["x"]) for i in range(labels.count())}) == 1
    # The checks sit below a divider, on the controls' edge; the footer comes last.
    divider, first_check, foot = boxes(w.page, ".more-divider", ".more-checks .check", ".more-foot")
    assert divider["y"] < first_check["y"] < foot["y"]
    assert abs(round(first_check["x"]) - next(iter(edges))[0]) <= 1
    # Under its button, inside the window.
    button, box = w.page.locator("details.more-filters summary").bounding_box(), panel.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width - 8, (box, width)
    assert abs(box["x"] - button["x"]) <= 1 or abs(box["x"] + box["width"] - button["x"] - button["width"]) <= 1
    context.close()
    w.assert_clean()


def test_more_filters_resets_and_counts(watched):
    page = watched.page
    watched.go("/jobs?view=scored")
    page.locator("details.more-filters summary").click()
    expect(page.locator(".more-foot button[disabled]", has_text="Reset filters")).to_have_count(1)
    page.locator(".more-panel select[name=status]").select_option("clear")
    watched.settle()
    assert "status=clear" in page.url
    total = page.locator("#jobs-subtitle").inner_text().split(" role")[0].split()[-1]
    expect(page.locator(".more-count")).to_have_text(f"{total} matching role{'' if total == '1' else 's'}")
    page.locator(".more-foot a", has_text="Reset filters").click()
    watched.settle()
    assert "status=" not in page.url and "view=scored" in page.url
    expect(page.locator(".more-foot button[disabled]")).to_have_count(1)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_more_filters_sits_above_the_page(browser, base_url, scheme):
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
    w = Watched(context.new_page(), base_url)
    w.go("/jobs?view=apply")
    w.page.locator("details.more-filters summary").click()
    style = w.page.locator(".more-panel").evaluate(
        "e => { const s = getComputedStyle(e); return [s.backgroundColor, s.borderTopWidth, s.boxShadow]; }")
    card = w.page.locator(".jobs-card").evaluate("e => getComputedStyle(e).backgroundColor")
    assert style[0] != card or scheme == "light", "the panel needs its own surface in dark mode"
    assert style[1] == "1px" and style[2] != "none"
    context.close()
    w.assert_clean()


def test_the_sidebar_list_fades_where_it_scrolls(browser, base_url):
    context = browser.new_context(viewport={"width": 1160, "height": 560})
    w = Watched(context.new_page(), base_url)
    w.go("/today")
    w.page.locator(".wish-group summary").click()
    w.settle(200)
    nav = w.page.locator(".source-list")
    assert nav.evaluate("n => n.scrollHeight > n.clientHeight"), "the list should scroll at this height"
    nav.evaluate("n => n.scrollTo(0, 0)")
    w.settle(150)
    assert "fade-bottom" in nav.get_attribute("class") and "fade-top" not in nav.get_attribute("class")
    nav.evaluate("n => n.scrollTo(0, n.scrollHeight)")
    w.settle(150)
    classes = nav.get_attribute("class")
    assert "fade-top" in classes and "fade-bottom" not in classes
    context.close()
    w.assert_clean()
