"""The cover letter as a one-page PDF (2026-10-29): a header with the candidate's name, email,
phone and LinkedIn (from the untracked personal.toml, read at export and never stored), the date,
the company, then the letter. Printed by Chromium (Playwright, already used to render
JavaScript-only career sites), A4, and checked to fit on one page.
"""

from __future__ import annotations

import html
import re
from datetime import date

PAGE = re.compile(rb"/Type\s*/Page(?!s)")


class LetterError(ValueError):
    pass


def file_name(name: str, company: str) -> str:
    """"{name} Cover Letter {company}.pdf", without characters a file name cannot hold."""
    clean = lambda text: re.sub(r'[\\/:*?"<>|]+', " ", text).strip()  # noqa: E731
    return " ".join(p for p in (clean(name), "Cover Letter", clean(company)) if p) + ".pdf"


def letter_html(contact: dict[str, str], company: str, body: str, day: date) -> str:
    """The page: header (name, then email, phone and LinkedIn on one line), date, company, letter."""
    def esc(text: str) -> str:
        return html.escape(text or "")
    links = [esc(contact.get(k, "")) for k in ("email", "phone", "linkedin") if contact.get(k)]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    letter = "".join(f"<p>{esc(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 22mm 22mm 20mm; }}
body {{ font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 10.5pt; line-height: 1.5; color: #1d1d1f; margin: 0; }}
header {{ border-bottom: 0.6pt solid #c7c7cc; padding-bottom: 9pt; margin-bottom: 18pt; }}
h1 {{ font-size: 19pt; font-weight: 600; letter-spacing: -0.01em; margin: 0 0 4pt; }}
.contact {{ font-size: 9.5pt; color: #3c3c43; }}
.contact span + span::before {{ content: "·"; margin: 0 6pt; color: #8e8e93; }}
.meta {{ margin-bottom: 16pt; }} .meta div {{ margin: 0; }}
p {{ margin: 0 0 9pt; text-align: left; hyphens: manual; }}
</style></head><body>
<header><h1>{esc(contact.get("name", ""))}</h1><div class="contact">{"".join(f"<span>{x}</span>" for x in links)}</div></header>
<div class="meta"><div>{day.day} {day:%B %Y}</div><div>{esc(company)}</div></div>
{letter}
</body></html>"""


def render(markup: str) -> bytes:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(markup, wait_until="load")
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            browser.close()


def pages(pdf: bytes) -> int:
    return len(PAGE.findall(pdf))


def build(contact: dict[str, str], company: str, body: str, day: date, renderer=None) -> bytes:
    """The PDF, or LetterError when it is empty, has [placeholders] left, or runs past one page."""
    if not body.strip():
        raise LetterError("The letter is empty: draft or write it first.")
    left = re.findall(r"\[[^\]\n]{1,60}\]", body)
    if left:
        raise LetterError(f"Fill in {', '.join(dict.fromkeys(left))} before exporting.")
    pdf = (renderer or render)(letter_html(contact, company, body, day))
    count = pages(pdf)
    if count > 1:
        raise LetterError(f"The letter runs to {count} pages: shorten it to fit on one (about 400 words at most).")
    return pdf
