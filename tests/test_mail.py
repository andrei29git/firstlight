"""Job alert emails (2026-11-18): read-only, one label, links never followed, provisional roles."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from jobhunt import mail, pipeline
from jobhunt.companies import Company
from jobhunt.state import State

ALERT_HTML = """<html><body>
<p>New jobs matching your alert at Acme Robotics</p>
<table>
 <tr><td><a href="https://acme.wd3.myworkdayjobs.com/en-US/External/job/Delft/Graduate-Software-Engineer_R123">Graduate Software Engineer</a></td></tr>
 <tr><td>Delft, Netherlands</td></tr>
 <tr><td>Build the software that runs our warehouse robots.</td></tr>
 <tr><td><a href="https://acme.wd3.myworkdayjobs.com/en-US/External/job/Bucharest/Junior-Detection-Engineer_R124">Junior Detection Engineer</a></td></tr>
 <tr><td>Bucharest, Romania</td></tr>
</table>
<p><a href="https://acme.wd3.myworkdayjobs.com/en-US/External">View all jobs</a> ·
   <a href="https://acme.wd3.myworkdayjobs.com/en-US/External/userHome">Manage your job alerts</a> ·
   <a href="https://acme.example/unsubscribe?job=1">Unsubscribe</a></p>
</body></html>"""


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(message_id: str, labels=("Label_7",), sender="Acme Robotics Careers <careers@acme.example>") -> dict:
    return {"id": message_id, "labelIds": list(labels), "payload": {
        "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": "New jobs for you"},
                    {"name": "Date", "value": "Tue, 17 Nov 2026 08:00:00 +0000"}],
        "mimeType": "multipart/alternative",
        "parts": [{"mimeType": "text/plain", "body": {"data": b64("New jobs")}},
                  {"mimeType": "text/html", "body": {"data": b64(ALERT_HTML)}}]}}


class FakeGmail:
    """Answers the Gmail API calls and records them; anything else (a job link) fails the test."""

    def __init__(self, messages: dict[str, dict]):
        self.messages, self.calls = messages, []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        assert url.startswith(mail.API), f"only the Gmail API may be called, not {url}"
        assert headers["Authorization"] == "Bearer t"
        path = url[len(mail.API):]
        if path == "/labels":
            body = {"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_7", "name": "Job alerts"}]}
        elif path == "/messages":
            assert params["labelIds"] == "Label_7"  # every listing names the label
            body = {"messages": [{"id": i} for i in self.messages]}
        else:
            body = self.messages[path.rsplit("/", 1)[1]]
        return type("R", (), {"status_code": 200, "json": lambda self, b=body: b, "text": ""})()


def test_an_alert_email_becomes_roles_without_following_its_links():
    email = mail.email_from(message("m1"))
    assert (email.sender, email.address, email.date) == ("Acme Robotics Careers", "careers@acme.example", "2026-11-17")
    jobs = mail.roles_in(email, "Acme Robotics")
    assert [j.title for j in jobs] == ["Graduate Software Engineer", "Junior Detection Engineer"]  # not the footer links
    first = jobs[0]
    assert first.locations == ["Delft, Netherlands"] and first.source == "email" and first.posted == "2026-11-17"
    assert "posting itself was not read" in first.description and "warehouse robots" in first.description
    assert len(first.description) <= mail.SNIPPET_CHARS


def test_the_company_comes_from_your_list_or_the_senders_name():
    email = mail.email_from(message("m1"))
    assert mail.company_of(email, {"acme.example": "Acme"}) == "Acme"
    assert mail.company_of(email, {}) == "Acme Robotics"


def test_reading_stays_inside_the_label_and_skips_messages_already_read(tmp_path):
    settings = mail.Settings(folder=tmp_path)
    fake = FakeGmail({"m1": message("m1"), "m2": message("m2")})
    gmail = mail.Gmail("t", session=fake)
    mail.remember(settings, {"m1"})
    found, read = mail.read_alerts(gmail, settings, {})
    assert (found.messages, found.new_messages, read) == (2, 1, {"m1", "m2"})
    assert [u.rsplit("/", 1)[1] for u, _ in fake.calls if "/messages/" in u] == ["m2"]
    assert fake.calls[1][1]["q"] == "newer_than:30d"
    with pytest.raises(mail.MailError):  # a message without the label is never read
        mail.Gmail("t", session=FakeGmail({"m3": message("m3", labels=("INBOX",))})).message("m3", "Label_7")


def test_sign_in_asks_for_read_only_access_and_the_token_refreshes(tmp_path):
    url = mail.auth_url("id", "http://127.0.0.1:5000/", "challenge", "s")
    assert "gmail.readonly" in url and "code_challenge_method=S256" in url and "access_type=offline" in url
    settings = mail.Settings(folder=tmp_path)
    settings.client_file.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "secret"}}))
    now = datetime(2026, 11, 18, tzinfo=UTC)
    mail.save_token(settings, {"refresh_token": "r", "access_token": "old", "expires_in": 10, "scope": mail.SCOPE},
                    now - timedelta(hours=2))
    posted = []

    def post(url, data, timeout):
        posted.append(data)
        return type("R", (), {"json": lambda self: {"access_token": "new", "expires_in": 3600, "scope": mail.SCOPE}})()
    assert mail.access_token(settings, post=post, now=now) == "new"
    assert posted[0]["grant_type"] == "refresh_token" and posted[0]["refresh_token"] == "r"
    assert mail.access_token(settings, post=post, now=now) == "new" and len(posted) == 1  # still valid: no call
    assert oct(settings.token_file.stat().st_mode)[-3:] == "600"


def test_alert_roles_are_stored_as_your_companys_with_a_short_snippet(tmp_path):
    state = State(tmp_path / "state")
    jobs = mail.roles_in(mail.email_from(message("m1")), "Acme Robotics")
    company = Company(name="Acme Robotics", board="email", partial=True)
    counts = pipeline.update_jobs(state, pipeline.Fetched(company, jobs), {"NL", "RO"}, "2026-11-18T09:00:00+00:00")
    assert counts["new"] == 2 and counts["passed"] == 2
    record = next(iter(state.jobs.values()))
    assert record["source"] == "email" and record["origin"] == "config" and pipeline.scorable(record)
    assert len(state.read_posting(record["description_hash"])) <= mail.SNIPPET_CHARS
    # A partial source: a role missing from later alerts is not closed at once.
    pipeline.update_jobs(state, pipeline.Fetched(company, []), {"NL", "RO"}, "2026-11-19T09:00:00+00:00")
    assert not any(r["closed"] for r in state.jobs.values())
