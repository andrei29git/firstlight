"""Oracle Recruiting Cloud: the public candidate-experience API (2026-10-29), used by Nokia,
Oracle and J.P. Morgan.

Listing: `GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions` with
`finder=findReqs;siteNumber={site},limit=200,offset=N,sortBy=POSTING_DATES_DESC` and
`expand=requisitionList.secondaryLocations`: title, id, primary location and its country (ISO
alpha-2), secondary locations, posting date. Detail (for roles the pipeline wants):
`recruitingCEJobRequisitionDetails?finder=ById;Id="{id}",siteNumber={site}&expand=all`, with the
description, responsibilities and qualifications as HTML.

Config: `url` is the API host ("https://jpmc.fa.oraclecloud.com"), `slug` the site number
("CX_1001"), `job_url` the public job page base (a job is `{job_url}/job/{id}`), and `countries`
(ISO alpha-2) keeps only roles with a location there: these listings run to thousands of roles
worldwide, and the API cannot filter by country without tenant-specific location ids.
"""

from __future__ import annotations

from typing import Any

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text, unique
from .http import Http

PAGE = 200
MAX_PAGES = 60
API = "/hcmRestApi/resources/latest"


def list_url(host: str, site: str, offset: int) -> str:
    return (f"{host}{API}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber={site},limit={PAGE},offset={offset},sortBy=POSTING_DATES_DESC")


def detail_url(host: str, site: str, job_id: str) -> str:
    return (f"{host}{API}/recruitingCEJobRequisitionDetails?onlyData=true&expand=all"
            f'&finder=ById;Id="{job_id}",siteNumber={site}')


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    host, site = (company.url or "").rstrip("/"), company.slug or ""
    if not host or not site:
        raise SourceError(f"{company.name}: an Oracle Recruiting Cloud board needs url (the API host) and slug (the site number)")
    countries = {str(c).upper() for c in company.extra.get("countries", [])}
    job_base = str(company.extra.get("job_url") or f"{host}/hcmUI/CandidateExperience/en/sites/{site}").rstrip("/")
    jobs: list[Job] = []
    for page in range(MAX_PAGES):
        data = http.get_json(list_url(host, site, page * PAGE))
        items = data.get("items") or []
        if not items:
            raise SourceError(f"{company.name}: unexpected Oracle Recruiting Cloud response (no items)")
        listing = items[0].get("requisitionList") or []
        for raw in listing:
            if countries and not countries & codes_of(raw):
                continue
            jobs.append(parse_listing(company.name, raw, job_base))
        total = int(items[0].get("TotalJobsCount") or 0)
        if not listing or (page + 1) * PAGE >= total:
            break
    for job in jobs:
        if details(job):
            try:
                add_details(job, http.get_json(detail_url(host, site, job.source_id)))
            except SourceError:
                pass  # the listing's data stays; the text is filled in on a later run
    return jobs


def codes_of(raw: dict[str, Any]) -> set[str]:
    codes = {str(raw.get("PrimaryLocationCountry") or "").upper()}
    codes |= {str(s.get("CountryCode") or "").upper() for s in raw.get("secondaryLocations") or []}
    return codes - {""}


def parse_listing(company: str, raw: dict[str, Any], job_base: str) -> Job:
    places = unique([raw.get("PrimaryLocation") or ""] + [s.get("Name") or "" for s in raw.get("secondaryLocations") or []])
    country = raw.get("PrimaryLocationCountry")
    if places and country and len(places[0].split(",")) == 1 and places[0].upper() != country:
        places[0] = f"{places[0]}, {country}"  # "Bucharest" + "RO": the pre-filter needs the country
    return Job(company=company, title=(raw.get("Title") or "").strip(), url=f"{job_base}/job/{raw.get('Id')}",
               source="oracle", source_id=str(raw.get("Id")), locations=[p for p in places if p],
               description=html_to_text(raw.get("ShortDescriptionStr")), posted=(raw.get("PostedDate") or "")[:10] or None,
               department=raw.get("Department") or raw.get("JobFamily") or None, employment_type=raw.get("JobType") or None)


def add_details(job: Job, data: dict[str, Any]) -> None:
    items = data.get("items") or []
    if not items:
        return
    item = items[0]
    parts = [html_to_text(item.get(k)) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                                                 "ExternalQualificationsStr", "CorporateDescriptionStr")]
    text = "\n\n".join(p for p in parts if p)
    if text:
        job.description = text
    posted = (item.get("ExternalPostedStartDate") or "")[:10]
    if posted:
        job.posted = posted
