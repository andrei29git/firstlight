"""The demo is sealed (2026-11-19): `jobhunt serve --demo`, as its own process, with every outbound
connection, DNS lookup, `claude` call and network git command refused and logged. The browser opens
every page and sheet and presses every button that could reach out; the test fails if anything
tried. When the server stops, its temporary folder is gone."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest
from playwright.sync_api import expect

from .conftest import free_port
from .test_pages import PAGES, SHEETS

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).parents[2]
APPLIED = "greenhouse:Polderbyte:pb-1"  # Greenhouse questions, drafts and a chat
LETTER = "Rivoli Ledger"  # the role with a recorded cover letter

GUARD = textwrap.dedent('''
    """Loaded at start-up by every Python process of the sealed demo (PYTHONPATH): refuse and log
    anything that leaves this machine."""
    import os, socket
    LOG = os.environ["JOBHUNT_SEAL_LOG"]
    LOCAL = ("127.", "::1", "localhost")

    def note(line):
        with open(LOG, "a") as f:
            f.write(f"{os.getpid()} {line}\\n")

    def local(host):
        return host is None or str(host).startswith(LOCAL)

    real_connect, real_connect_ex, real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def connect(self, address, _real=real_connect):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not local(address[0]):
            note(f"connect {address}")
            raise OSError("blocked: the sealed demo test refuses outbound connections")
        return _real(self, address)

    def connect_ex(self, address):
        try:
            connect(self, address, real_connect_ex)
        except OSError:
            return 111
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not local(host):
            note(f"dns {host}")
            raise socket.gaierror("blocked: the sealed demo test refuses DNS lookups")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, getaddrinfo
''')


@pytest.fixture
def sealed(tmp_path):
    """The environment of a sealed process: the guard, and `claude` and git that log and refuse."""
    log = tmp_path / "reached-out.log"
    log.touch()
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(GUARD)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "claude").write_text(f'#!/bin/sh\necho "$$ claude $*" >> "{log}"\nexit 1\n')
    git = shutil.which("git") or "/usr/bin/git"
    (bin_ / "git").write_text(textwrap.dedent(f'''\
        #!/bin/sh
        case " $* " in *" fetch "*|*" pull "*|*" push "*|*" clone "*|*" ls-remote "*)
          echo "$$ git $*" >> "{log}"; exit 1;;
        esac
        exec "{git}" "$@"
    '''))
    for script in bin_.iterdir():
        script.chmod(0o755)
    env = {**os.environ, "JOBHUNT_SEAL_LOG": str(log), "PYTHONUNBUFFERED": "1", "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
           "PYTHONPATH": os.pathsep.join(p for p in (str(guard), os.environ.get("PYTHONPATH")) if p)}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    return env, log


def test_the_guard_catches_what_it_is_meant_to(sealed):
    """The seal itself works: a request, a lookup and a claude call from a sealed process are logged."""
    env, log = sealed
    attempt = "import urllib.request\ntry:\n    urllib.request.urlopen('https://example.com', timeout=3)\nexcept Exception:\n    pass"
    subprocess.run([sys.executable, "-c", attempt], env=env, check=True, timeout=30)
    subprocess.run(["claude", "-p", "hello"], env=env, timeout=30)
    subprocess.run(["git", "ls-remote", "https://github.com/example/example"], env=env, timeout=30, capture_output=True)
    text = log.read_text()
    assert "dns example.com" in text and "claude -p hello" in text and "git ls-remote" in text


def test_the_demo_never_reaches_out(browser, sealed):
    env, log = sealed
    port = free_port()
    server = subprocess.Popen([sys.executable, "-c", f"from jobhunt.cli import main; main(['serve', '--demo', '--port', '{port}'])"],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    folder = None
    try:
        first = server.stdout.readline()
        folder = Path(re.search(r"\(database (.+)/jobhunt\.db\)", first).group(1))
        for _ in range(200):
            try:
                urllib.request.urlopen(base + "/today", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        def go(path: str) -> None:
            page.goto(base + path, wait_until="networkidle")

        def settle(ms: int = 300) -> None:
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(ms)

        def post(path: str, data: dict) -> str:
            return page.request.post(base + path, form=data, headers={"Origin": base}).text()

        # Every page, and every sheet.
        for path in PAGES:
            go(path)
        for path, _, open_it in SHEETS:
            go(path)
            open_it(page)
            settle()
            page.keyboard.press("Escape")

        # Set it up: by name, and with a careers page.
        for name, fill_url in (("nordwind robotics", False), ("havel instruments", True)):  # a set-up is remembered
            go("/companies?q=" + quote(name))
            page.locator(".setup-empty button", has_text="Set it up").click()
            sheet = page.locator("dialog#sheet")
            expect(sheet).to_contain_text("Switched off in the demo")
            if fill_url:
                sheet.locator("input[name='url']").fill("https://careers.example.com/jobs")
            sheet.get_by_role("button", name="Look for it").click()
            expect(sheet).to_contain_text("could not be set up", timeout=15000)
            page.keyboard.press("Escape")
        assert "Switched off in the demo" in post("/wishlist/find", {"name": "Nordwind Robotics", "url": "https://example.com"})

        # Find more companies: said in its sheet, and refused if asked directly.
        go("/companies")
        page.get_by_role("button", name="Find more companies").click()
        sheet = page.locator("dialog#sheet")
        expect(sheet).to_contain_text("this would search the web with your Claude account")
        expect(sheet.get_by_role("button", name="Search")).to_be_disabled()
        page.keyboard.press("Escape")
        assert "search the web with your Claude account" in post("/companies/find/start", {"country": "NL", "domain": "software"})

        # Fetch and score now: offered nowhere, refused if asked.
        post("/wishlist/set", {"name": "Polderbyte", "on": "1", "fetch_now": "1"})
        assert "Switched off in the demo" in post("/wishlist/score", {"company": "polderbyte"})

        # Application help on the role with recorded drafts: the Greenhouse questions, every draft
        # button, the letter, the chat, Try again and Rescore.
        go(f"/job?key={quote(APPLIED)}")
        help_ = page.locator("#help")
        expect(help_).to_contain_text("Reload from Greenhouse is switched off in the demo")
        assert "load the questions from Greenhouse" in post("/job/help/questions/load", {"key": APPLIED})
        help_.get_by_role("button", name="Draft all").click()
        settle()
        expect(help_.locator(".banner[role=note]")).to_contain_text("Drafting is switched off in the demo")
        expect(help_.locator(".banner[role=note]")).to_contain_text("drafts shown were recorded")
        help_.get_by_role("button", name="Redraft").first.click()
        settle()
        help_.get_by_role("tab", name="Chat").click()
        settle()
        bubbles = help_.locator(".bubble").count()
        help_.locator(".chat-form textarea").fill("Make it shorter.")
        help_.get_by_role("button", name="Send").click()
        settle()
        expect(help_.locator(".banner[role=note]")).to_contain_text("Chat is switched off in the demo")
        assert help_.locator(".bubble").count() == bubbles  # nothing sent, nothing kept
        post("/job/help/retry", {"key": APPLIED, "kind": "questions"})
        page.locator(".scored-by .link-btn", has_text="Rescore").click()
        settle()
        expect(page.locator("#score-demo-note")).to_contain_text("Rescoring is switched off in the demo")
        assert "Rescoring is switched off" in post("/job/rescore", {"key": APPLIED, "back": "/today"})  # asked directly
        # A role without drafts points to the recorded examples.
        go("/jobs?view=apply")
        go(page.locator("tr.row.job", has_text="Vercors Data").first.get_attribute("data-href"))
        page.locator("#help .paste-form textarea").fill("1. Why us?\n2. Are you eligible to work in France?")
        page.locator("#help .paste-form button").click()
        settle()
        page.get_by_role("button", name="Looks right").click()
        settle()
        page.locator("#help").get_by_role("button", name="Draft all").click()
        settle()
        expect(page.locator("#help .banner[role=note] a")).to_contain_text("Polderbyte")
        page.locator("#help [role=tab]", has_text="Cover letter").click()
        settle()
        page.locator("#help .letter-form button[type=submit]").click()
        settle()
        expect(page.locator("#help .banner[role=note] a")).to_contain_text(LETTER)
        post("/job/help/facts", {"key": APPLIED, "kind": "question", "qid": "q1", "fact_0_name": "x", "fact_0": "y"})

        # The rest of what a click can do: settings, sync, the row actions.
        go("/settings")
        page.select_option("select[name='apply_min_fit']", "8")
        settle()
        post("/sync", {})
        go("/jobs?view=scored")
        page.locator("tr.row.job").first.hover()
        page.locator("tr.row.job").first.locator("button[aria-label='Hide']").click()
        settle(1500)  # time for anything started in the background
        context.close()
    finally:
        server.terminate()
        output = server.communicate(timeout=20)[0]
    reached = log.read_text().strip()
    assert not reached, f"the demo tried to reach out:\n{reached}"
    assert "Traceback" not in output, output
    assert folder is not None and not folder.exists(), "the demo's temporary folder outlived the server"
