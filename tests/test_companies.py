from __future__ import annotations

import shutil
from datetime import date

from jobhunt import companies as co

from .conftest import EXAMPLE_CONFIG


def copy_config(tmp_path):
    shutil.copy(EXAMPLE_CONFIG / co.FILE, tmp_path / co.FILE)
    return tmp_path


def test_load_example_companies():
    loaded = {c.name: c for c in co.load_companies(EXAMPLE_CONFIG)}
    assert loaded["Mistral"].slug == "mistral.ai" and loaded["Mistral"].fetchable
    assert not loaded["Stripe"].fetchable  # todo = "detect"
    assert loaded["Stripe"].wishlist


def test_set_verified_inserts_then_updates(tmp_path):
    config = copy_config(tmp_path)
    original = (config / co.FILE).read_text()
    assert co.set_verified(config, "Winton", date(2027, 1, 2))
    assert co.set_verified(config, "Winton", date(2027, 1, 3))
    text = (config / co.FILE).read_text()
    assert text.count("verified = 2027-01-03") == 1 and "2027-01-02" not in text
    assert {c.name: c.verified for c in co.load_companies(config)}["Winton"] == date(2027, 1, 3)
    # Only one line changed: comments and other blocks are untouched.
    assert len(text.splitlines()) == len(original.splitlines()) + 1


def test_set_verified_on_last_block_without_trailing_newline(tmp_path):
    (tmp_path / co.FILE).write_text('[[company]]\nname = "A"\nboard = "ashby"\nslug = "a"')
    assert co.set_verified(tmp_path, "A", date(2027, 1, 2))
    assert co.load_companies(tmp_path)[0].verified == date(2027, 1, 2)


def test_set_verified_unknown_company(tmp_path):
    assert not co.set_verified(copy_config(tmp_path), "Nobody", date(2027, 1, 2))
