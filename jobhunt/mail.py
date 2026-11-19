"""Job alert emails as a source (2026-11-18): for employers whose sites the tool cannot read
(JavaScript-only pages, robots.txt, bot protection), their own job alert emails, filed under one
Gmail label, become roles.

- Access: the Gmail API with the read-only scope (`gmail.readonly`), and every request names the
  label: nothing outside it is listed or read. The OAuth client and the token stay on the laptop in
  `.gmail/` (git-ignored), so this runs on the laptop only.
- Links in the emails are never followed. A role is scored from its title and the email's text
  around it, and shown as provisional.
- Stored on the data branch: title, link, place, date and a snippet of at most 1,000 characters (as
  the role's posting text). The ids of the messages already read stay on the laptop
  (`.gmail/seen.json`).

    uv run jobhunt mail auth     # once: sign in to Google in the browser, read-only
    uv run jobhunt mail fetch    # read new alert emails, add and score their roles, push
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import webbrowser
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from . import geo
from .sources.base import Job, html_to_text
from .sources.custom import parse_page

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SNIPPET_CHARS = 1000
SOURCE = "email"

# Links in alert emails that are not roles.
NOT_A_ROLE = re.compile(r"unsubscribe|manage|preferences|privacy|terms|view all|see all|all jobs|search jobs|"
                        r"log ?in|sign ?in|account|profile|help|contact|facebook|twitter|linkedin|instagram|youtube|"
                        r"cookie|settings|edit (your )?alert|apply now$|view job$|learn more|read more", re.IGNORECASE)
LOOKS_LIKE_A_ROLE = re.compile(r"job|vacanc|career|requisition|posting|position|opening|/details/|/apply|req|jobid|"
                               r"vacature|stelle|stelle|cariere|offre", re.IGNORECASE)
SENDER_NOISE = re.compile(r"\b(?:careers?|jobs?|job alerts?|recruit(?:ing|ment)?|talent(?: acquisition)?|"
                          r"hiring|team|no-?reply|notifications?|hr)\b", re.IGNORECASE)


@dataclass
class Settings:
    label: str = "Job alerts"
    folder: Path = Path(".gmail")
    lookback_days: int = 30

    @classmethod
    def from_settings(cls, settings: dict[str, Any], root: Path) -> "Settings":
        mail = settings.get("mail", {})
        return cls(label=str(mail.get("label", "Job alerts")), folder=root / str(mail.get("folder", ".gmail")),
                   lookback_days=int(mail.get("lookback_days", 30)))

    @property
    def client_file(self) -> Path:
        return self.folder / "client_secret.json"

    @property
    def token_file(self) -> Path:
        return self.folder / "token.json"

    @property
    def seen_file(self) -> Path:
        return self.folder / "seen.json"


class MailError(Exception):
    pass


# --- OAuth (installed app, loopback redirect, PKCE) --------------------------------------------

def client(settings: Settings) -> dict[str, str]:
    """The OAuth client downloaded from Google Cloud (a "Desktop app" client)."""
    if not settings.client_file.exists():
        raise MailError(f"No OAuth client at {settings.client_file}: see the setup steps (`jobhunt mail auth --help`).")
    data = json.loads(settings.client_file.read_text())
    installed = data.get("installed") or data.get("web") or {}
    if not installed.get("client_id") or not installed.get("client_secret"):
        raise MailError(f"{settings.client_file} is not a Desktop app OAuth client")
    return {"client_id": installed["client_id"], "client_secret": installed["client_secret"]}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def auth_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline",
        "prompt": "consent", "state": state})


def authorise(settings: Settings, post: Callable[..., Any] = requests.post, open_browser: Callable[[str], Any] = webbrowser.open,
              say: Callable[[str], None] = print) -> None:
    """Sign in once in the browser; the refresh token is saved to `.gmail/token.json`."""
    c = client(settings)
    verifier, challenge = pkce()
    expected = secrets.token_urlsafe(16)
    got: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server's name)
            query = parse_qs(urlsplit(self.path).query)
            got.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Signed in. You can close this tab and go back to the terminal.".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = auth_url(c["client_id"], redirect, challenge, expected)
    say(f"Opening the browser to sign in (read-only Gmail access). If it does not open:\n{url}")
    open_browser(url)
    while "code" not in got and "error" not in got:
        server.handle_request()
    server.server_close()
    if got.get("error") or got.get("state") != expected:
        raise MailError(f"Sign-in did not complete: {got.get('error') or 'state mismatch'}")
    response = post(TOKEN_URL, data={**c, "code": got["code"], "code_verifier": verifier, "redirect_uri": redirect,
                                     "grant_type": "authorization_code"}, timeout=30)
    data = response.json()
    if "refresh_token" not in data:
        raise MailError(f"Google gave no refresh token: {data.get('error_description') or data}")
    save_token(settings, data)
    say(f"Saved the token to {settings.token_file} (read-only access; stays on this laptop).")


def save_token(settings: Settings, data: dict[str, Any], now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    token = {"refresh_token": data["refresh_token"], "access_token": data.get("access_token"),
             "expires_at": (now + timedelta(seconds=int(data.get("expires_in", 0)) - 60)).isoformat(),
             "scope": data.get("scope", SCOPE)}
    settings.folder.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text(json.dumps(token, indent=1))
    settings.token_file.chmod(0o600)


def access_token(settings: Settings, post: Callable[..., Any] = requests.post, now: datetime | None = None) -> str:
    """A valid access token, refreshed with the saved refresh token when it has expired."""
    if not settings.token_file.exists():
        raise MailError("Not signed in to Gmail yet: run `uv run jobhunt mail auth` first.")
    token = json.loads(settings.token_file.read_text())
    if SCOPE not in str(token.get("scope", SCOPE)).split():
        raise MailError("The saved token is not read-only Gmail access: run `uv run jobhunt mail auth` again.")
    now = now or datetime.now(UTC)
    if token.get("access_token") and datetime.fromisoformat(token["expires_at"]) > now:
        return token["access_token"]
    data = post(TOKEN_URL, data={**client(settings), "refresh_token": token["refresh_token"],
                                 "grant_type": "refresh_token"}, timeout=30).json()
    if "access_token" not in data:
        raise MailError(f"Could not refresh the Gmail token ({data.get('error')}): run `uv run jobhunt mail auth` again.")
    save_token(settings, {**data, "refresh_token": token["refresh_token"]}, now)
    return data["access_token"]


# --- Reading the label --------------------------------------------------------------------------

class Gmail:
    """Read-only calls, each one limited to the alert label."""

    def __init__(self, token: str, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}"}

    def get(self, path: str, **params) -> dict[str, Any]:
        response = self.session.get(API + path, headers=self.headers, params=params, timeout=30)
        if response.status_code >= 400:
            raise MailError(f"Gmail API {response.status_code} on {path}: {response.text[:200]}")
        return response.json()

    def label_id(self, name: str) -> str:
        for label in self.get("/labels").get("labels", []):
            if label.get("name") == name:
                return label["id"]
        raise MailError(f'No Gmail label "{name}": create it and file the job alert emails under it.')

    def message_ids(self, label_id: str, days: int) -> list[str]:
        ids, token = [], None
        while True:
            params = {"labelIds": label_id, "q": f"newer_than:{days}d", "maxResults": 100}
            if token:
                params["pageToken"] = token
            data = self.get("/messages", **params)
            ids += [m["id"] for m in data.get("messages", [])]
            token = data.get("nextPageToken")
            if not token:
                return ids

    def message(self, message_id: str, label_id: str) -> dict[str, Any]:
        data = self.get(f"/messages/{message_id}", format="full")
        if label_id not in data.get("labelIds", []):  # never read a message outside the label
            raise MailError(f"message {message_id} is not under the alert label")
        return data


# --- Turning an email into roles ------------------------------------------------------------------

@dataclass
class Email:
    sender: str  # display name
    address: str
    subject: str
    date: str | None  # ISO date
    html: str = ""
    text: str = ""


def decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def email_from(message: dict[str, Any]) -> Email:
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    name, address = parseaddr(headers.get("from", ""))
    try:
        sent = parsedate_to_datetime(headers["date"]).date().isoformat() if headers.get("date") else None
    except (TypeError, ValueError):
        sent = None
    out = Email(sender=name or address, address=address, subject=headers.get("subject", ""), date=sent)

    def walk(part: dict[str, Any]) -> None:
        body = (part.get("body") or {}).get("data")
        kind = part.get("mimeType", "")
        if body and kind == "text/html" and not out.html:
            out.html = decode(body)
        elif body and kind == "text/plain" and not out.text:
            out.text = decode(body)
        for child in part.get("parts") or []:
            walk(child)
    walk(payload)
    return out


def company_of(email: Email, known: dict[str, str]) -> str:
    """The employer: a company of yours whose careers site shares the sender's domain, else the
    sender's name without words like "Careers" or "Talent"."""
    domain = email.address.rpartition("@")[2].lower()
    for site, name in known.items():
        if domain and (domain == site or domain.endswith("." + site) or site.endswith("." + domain)):
            return name
    cleaned = " ".join(SENDER_NOISE.sub(" ", re.sub(r"[|@·•\-–—:]", " ", email.sender)).split())
    return cleaned or email.sender or domain


def roles_in(email: Email, company: str) -> list[Job]:
    """The roles an alert email lists: links whose text reads like a role title. The links are kept
    as they are and never requested."""
    text = html_to_text(email.html) if email.html else email.text
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    links = parse_page(email.html).links if email.html else \
        [(u, "") for u in re.findall(r"https?://\S+", email.text)]
    jobs: dict[str, Job] = {}
    for href, label in links:
        title = " ".join(label.split())
        if not href.startswith(("http://", "https://")) or not title or len(title) < 4 or len(title) > 140:
            continue
        if NOT_A_ROLE.search(title) or not LOOKS_LIKE_A_ROLE.search(href):
            continue
        snippet, place = context(lines, title)
        key = hashlib.sha1(href.encode()).hexdigest()[:16]
        jobs.setdefault(key, Job(
            company=company, title=title, url=href, source=SOURCE, source_id=key,
            locations=[place] if place else [], posted=email.date,
            description=(f"From a job alert email ({email.sender}, {email.date or 'date unknown'}): the posting itself "
                         f"was not read, so this score is provisional.\n\n{snippet}")[:SNIPPET_CHARS]))
    return list(jobs.values())


def context(lines: list[str], title: str) -> tuple[str, str | None]:
    """The text around a role's title in the email, and a place named near it."""
    at = next((i for i, line in enumerate(lines) if title in line), None)
    if at is None:
        return title, None
    near = lines[at:at + 6]
    place = next((line for line in near[1:4] if len(line) < 80 and geo.parse(line).codes), None)
    return "\n".join(near)[:SNIPPET_CHARS - 200], place


# --- What has been read (local only) ----------------------------------------------------------------

def seen(settings: Settings) -> set[str]:
    return set(json.loads(settings.seen_file.read_text())) if settings.seen_file.exists() else set()


def remember(settings: Settings, ids: set[str]) -> None:
    settings.folder.mkdir(parents=True, exist_ok=True)
    settings.seen_file.write_text(json.dumps(sorted(ids)))


@dataclass
class Found:
    messages: int = 0
    new_messages: int = 0
    by_company: dict[str, list[Job]] = field(default_factory=dict)


def read_alerts(gmail: Gmail, settings: Settings, known: dict[str, str]) -> tuple[Found, set[str]]:
    """New messages under the label and the roles in them; returns the ids read (to remember)."""
    label = gmail.label_id(settings.label)
    ids = gmail.message_ids(label, settings.lookback_days)
    already = seen(settings)
    found = Found(messages=len(ids))
    read: set[str] = set()
    for message_id in ids:
        if message_id in already:
            continue
        email = email_from(gmail.message(message_id, label))
        company = company_of(email, known)
        found.by_company.setdefault(company, []).extend(roles_in(email, company))
        read.add(message_id)
    found.new_messages = len(read)
    return found, already | read


def known_sites(companies: list) -> dict[str, str]:
    """Careers-site domains of your companies (careers_url, listing_url or url) -> name."""
    out = {}
    for c in companies:
        for url in (c.extra.get("careers_url"), c.listing_url, c.url):
            host = urlsplit(str(url or "")).netloc.lower()
            if host:
                parts = host.split(".")
                out[".".join(parts[-2:]) if len(parts) > 2 else host] = c.name
    return out
