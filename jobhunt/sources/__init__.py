"""Job board adapters: one module per board type, all returning the common Job object."""

from __future__ import annotations

from typing import Callable

from ..companies import Company
from . import (amazon, apple, ashby, avature, bendingspoons, custom, eightfold, google, greenhouse, homerun, jibe, join, lever, oracle,
               personio, recruitee, smartrecruiters, teamtailor, workable, workday)
from .base import Job, SourceError, WantDetails, all_details, no_details
from .http import Http

ADAPTERS: dict[str, Callable[..., list[Job]]] = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "workday": workday.fetch,
    "recruitee": recruitee.fetch,
    "custom": custom.fetch,
    "jibe": jibe.fetch,
    "personio": personio.fetch,
    "teamtailor": teamtailor.fetch,
    "join": join.fetch,
    "workable": workable.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "homerun": homerun.fetch,
    "amazon": amazon.fetch,
    "apple": apple.fetch,
    "oracle": oracle.fetch,
    "bendingspoons": bendingspoons.fetch,
    "avature": avature.fetch,
    "eightfold": eightfold.fetch,
    "google": google.fetch,
}

__all__ = ["ADAPTERS", "Http", "Job", "SourceError", "WantDetails", "all_details", "fetch", "no_details"]


def fetch(company: Company, http: Http, details: WantDetails = all_details,
          custom_max_links: int = custom.DEFAULT_MAX_LINKS, light: bool = False) -> list[Job]:
    """All open jobs on the company's board. Raises SourceError on any failure, including an
    unexpected response shape, so a bad fetch can never be mistaken for an empty board.
    `light` asks boards that can for a listing without descriptions (Greenhouse), fetching the
    description only for jobs the `details` hook wants."""
    adapter = ADAPTERS.get(company.board or "")
    if adapter is None:
        raise SourceError(f"{company.name}: no adapter for board {company.board!r}")
    kwargs: dict = {"max_links": custom_max_links} if company.board in ("custom", "apple") else {}
    if light and company.board == "greenhouse":
        kwargs["light"] = True
    try:
        return adapter(company, http, details, **kwargs)
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        raise SourceError(f"{company.name}: unexpected {company.board} response ({type(e).__name__}: {e})") from e
