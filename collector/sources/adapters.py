"""Per-ATS adapters. Each hits a FREE public JSON endpoint and normalizes postings to a
common shape. Read-only, no auth, no keys. Polite: short timeout, identifying User-Agent,
and conditional GET (ETag / Last-Modified) so we don't re-download unchanged boards.

Common job shape (a dict):
    url, title, company, location, department, posted_at (ISO or ''), ats, token,
    description (plain text, '' if the board needs a second call), salary_hint, emp_hint,
    remote_hint, detail (url for a lazy per-job fetch, or '')

Greenhouse / Lever / Ashby hand us the full description in the SAME bulk call, so it's free.
SmartRecruiters and Workable need one call per posting — those set `detail`, and poll.py
fetches them ONLY for jobs that already passed the score gate (a handful, not thousands).
"""
from __future__ import annotations

import html as _html
import json
import random
import re
import time

from collector.sources import enrich, net

# Identity, the conditional-GET cache and the fetch itself now live in net.py, which every
# harvester shares. These two names stay because they read better at the call sites below, and
# because they pin down what each kind of endpoint expects back: JSON adapters degrade on None,
# XML adapters parse whatever bytes arrived.
UA = net.UA


def _get_json(url: str, timeout: int = 20, retries: int = 3):
    """GET JSON politely. None on give-up — one flaky board must not break a poll of the others."""
    return net.get_json(url, timeout=timeout, retries=retries)


def _get_bytes(url: str, timeout: int = 20, retries: int = 3) -> bytes:
    """Same, for the XML feeds (Personio). Empty bytes on give-up."""
    return net.get_bytes(url, timeout=timeout, retries=retries)


# Greenhouse's public board API. Named because `discover` asks the same host what a board calls
# itself, and a base URL written out in two files is a base URL that can disagree with itself.
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/"


def _clean(s) -> str:
    return " ".join(str(s or "").split())


def _blank(**kw) -> dict:
    j = {"url": "", "title": "", "company": "", "location": "", "department": "", "posted_at": "",
         "ats": "", "token": "", "description": "", "salary_hint": "", "emp_hint": "",
         "remote_hint": "", "detail": ""}
    j.update(kw)
    return j


class BoardSnapshot(list):
    """A validated full public board, including an authoritative empty board.

    Ordinary lists retain their old meaning: completeness is not established. Only adapters
    whose documented endpoint returns all published postings may opt into reconciliation.
    """
    def __init__(self, jobs, ats, token, complete=True):
        super().__init__(jobs)
        self.ats, self.token = ats, token
        urls = [j.get("url") for j in self]
        valid = all(isinstance(u, str) and u.startswith(("https://", "http://")) for u in urls)
        self.complete = complete and valid and len(set(urls)) == len(urls)


def read_ok(jobs):
    return jobs.complete if isinstance(jobs, BoardSnapshot) else bool(jobs)


# ---- Greenhouse -------------------------------------------------------------
def greenhouse(token: str, company: str = "") -> list[dict]:
    # content=true returns the full posting HTML inline — no per-job call needed.
    data = _get_json(f"{GREENHOUSE_API}{token}/jobs?content=true")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return []
    out = []
    for j in data.get("jobs", []):
        out.append(_blank(
            url=j.get("absolute_url", ""),
            title=_clean(j.get("title")),
            company=company or token,
            location=_clean((j.get("location") or {}).get("name")),
            department=_clean(", ".join(d.get("name", "") for d in (j.get("departments") or []))),
            posted_at=j.get("first_published", ""), posted_field="greenhouse.first_published",
            description=enrich.to_text(j.get("content") or ""),
            ats="greenhouse", token=token))
    total = (data.get("meta") or {}).get("total", len(out))
    return BoardSnapshot(out, "greenhouse", token, complete=total == len(out))


# ---- Lever ------------------------------------------------------------------
def lever(token: str, company: str = "") -> list[dict]:
    data = _get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    if not isinstance(data, list):
        # EU customers live on a separate API; their boards answer 404 on the US one.
        data = _get_json(f"https://api.eu.lever.co/v0/postings/{token}?mode=json")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or []
        locations = list(dict.fromkeys(_clean(v) for v in [cats.get("location"), *locs]
                                       if isinstance(v, str) and v.strip()))
        # descriptionPlain is only the INTRO. The requirements (and therefore the years of
        # experience, the stack, the sponsorship line) live in `lists` — drop them and every
        # Lever posting looks like it has no requirements at all.
        parts = [j.get("descriptionPlain") or ""]
        for lst in (j.get("lists") or []):
            parts.append(_clean(lst.get("text")) + "\n" + enrich.to_text(lst.get("content") or ""))
        parts.append(j.get("additionalPlain") or "")
        out.append(_blank(
            url=j.get("hostedUrl", "") or j.get("applyUrl", ""),
            title=_clean(j.get("text")),
            company=company or token,
            location="; ".join(locations),
            department=_clean(cats.get("team") or cats.get("department")),
            # createdAt is a record creation time, not evidence of public publication.
            posted_at="",
            description="\n\n".join(p for p in parts if p.strip()),
            emp_hint=_clean(cats.get("commitment")),
            remote_hint=_clean(j.get("workplaceType")),
            ats="lever", token=token))
    return out


# ---- Ashby ------------------------------------------------------------------
def ashby(token: str, company: str = "") -> list[dict]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return []
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = j.get("compensation") or {}
        out.append(_blank(
            url=j.get("jobUrl", "") or j.get("applyUrl", ""),
            title=_clean(j.get("title")),
            company=company or token,
            location=_clean(j.get("location") or (j.get("address") or {})
                            .get("postalAddress", {}).get("addressLocality")),
            department=_clean(j.get("department") or j.get("team")),
            posted_at=j.get("publishedAt", "") or j.get("publishedDate", ""),
            posted_field="ashby.publishedAt" if j.get("publishedAt") else "ashby.publishedDate",
            description=j.get("descriptionPlain") or enrich.to_text(j.get("descriptionHtml") or ""),
            # the company's OWN comp field — trusted over anything we'd scrape from prose
            salary_hint=_clean(comp.get("scrapeableCompensationSalarySummary")
                               or comp.get("compensationTierSummary")),
            emp_hint=_clean(j.get("employmentType")),
            remote_hint="Remote" if j.get("isRemote") else _clean(j.get("workplaceType")),
            ats="ashby", token=token))
    return BoardSnapshot(out, "ashby", token)


# ---- SmartRecruiters --------------------------------------------------------
def smartrecruiters(token: str, company: str = "") -> list[dict]:
    """The list call gives structured experienceLevel / remote / typeOfEmployment but NO
    description — that needs one call per posting, so we defer it via `detail`."""
    out = []
    for offset in (0, 100, 200):                       # up to 300 postings/company; plenty
        data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
                         f"?limit=100&offset={offset}")
        items = (data or {}).get("content") or []
        if not items:
            break
        for j in items:
            loc = j.get("location") or {}
            city = _clean(loc.get("city"))
            region = _clean(loc.get("region"))
            where = ", ".join(x for x in (city, region) if x) or _clean(loc.get("country")).upper()
            if _clean(loc.get("country")).lower() == "in" and not re.search(r"\bindia\b", where, re.I):
                where = (where + ", India") if where != "IN" else "India"
            pid = j.get("id", "")
            out.append(_blank(
                # The public list API omits postingUrl; the numeric posting id also resolves
                # on the public job-ad host. Without this fallback, the poller drops every row.
                url=j.get("postingUrl", "") or j.get("applyUrl", "") or
                    (f"https://jobs.smartrecruiters.com/{token}/{pid}" if re.fullmatch(r"\d+", str(pid)) else ""),
                title=_clean(j.get("name")),
                company=company or token,
                location=where,
                department=_clean((j.get("department") or {}).get("label")),
                posted_at=j.get("releasedDate", ""),
                posted_field="smartrecruiters.releasedDate",
                emp_hint=_clean((j.get("typeOfEmployment") or {}).get("label")),
                remote_hint="Remote" if loc.get("remote") else ("Hybrid" if loc.get("hybrid") else ""),
                detail=f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{pid}",
                ats="smartrecruiters", token=token))
        if len(items) < 100:
            break
    return out


def _detail_smartrecruiters(url: str) -> str:
    d = _get_json(url)
    if not d:
        return ""
    secs = ((d.get("jobAd") or {}).get("sections") or {})
    parts = [(secs.get(k) or {}).get("text", "") for k in
             ("companyDescription", "jobDescription", "qualifications", "additionalInformation")]
    return enrich.to_text("\n\n".join(p for p in parts if p))


# ---- Workable ---------------------------------------------------------------
def workable(token: str, company: str = "") -> list[dict]:
    data = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true")
    if not data:
        return []
    out = []
    for j in (data.get("jobs") or []):
        out.append(_blank(
            url=j.get("url", "") or j.get("application_url", ""),
            title=_clean(j.get("title")),
            company=company or _clean(data.get("name")) or token,
            location=_clean(", ".join(x for x in (j.get("city"), j.get("country")) if x)),
            department=_clean(j.get("department")),
            posted_at=j.get("published_on", ""), posted_field="workable.published_on",
            description=enrich.to_text(j.get("description") or "") + "\n\n"
            + enrich.to_text(j.get("requirements") or ""),
            emp_hint=_clean(j.get("employment_type")),
            remote_hint="Remote" if j.get("telecommuting") else "",
            ats="workable", token=token))
    return out


# ---- Workday -----------------------------------------------------------------
# Workday is where most of the Fortune 500 posts, and it's the biggest single source of
# engineering jobs at NON-TECH employers — 3M, banks, insurers, retailers, hospitals. Without
# this adapter, 3,456 boards sat in the company catalog contributing zero jobs to the feed.
#
# Its API needs BOTH a tenant and a site name ({tenant}.wd5.myworkdayjobs.com/en-US/{SITE}),
# and one tenant can run several sites (AllianceBernstein has separate campus and main boards).
# So the token here is a compound "tenant|wdN|site" — both halves come out of the crawled URL.
def _post_json(url: str, body: dict, timeout: int = 20, retries: int = 2):
    return net.post_json(url, body, timeout=timeout, retries=retries)


def workday(token: str, company: str = "") -> list[dict]:
    parts = (token or "").split("|")
    if len(parts) != 3:
        return []
    tenant, wd, site = parts
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    out, offset = [], 0
    for _ in range(10):                        # up to 200 postings per board
        d = _post_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset,
                             "searchText": ""})
        posts = (d or {}).get("jobPostings") or []
        if not posts:
            break
        for j in posts:
            path = j.get("externalPath") or ""
            out.append(_blank(
                url=f"{host}/en-US/{site}{path}" if path else host,
                title=_clean(j.get("title")),
                company=company or tenant,
                location=_clean(j.get("locationsText")),
                # Workday says "Posted 30+ Days Ago", not a date — so we leave posted_at empty
                # rather than invent a timestamp the freshness logic would then trust.
                posted_at="",
                detail=f"{host}/wday/cxs/{tenant}/{site}{path}" if path else "",
                ats="workday", token=token))
        if len(posts) < 20:
            break
        offset += 20
        time.sleep(random.uniform(0.05, 0.2))
    return out


def _detail_workday(url: str) -> str:
    d = _get_json(url)
    info = ((d or {}).get("jobPostingInfo") or {})
    return enrich.to_text(info.get("jobDescription") or "")


# ---- Recruitee --------------------------------------------------------------
# Clean JSON, descriptions inline — the easiest of the batch.
def recruitee(token: str, company: str = "") -> list[dict]:
    data = _get_json(f"https://{token}.recruitee.com/api/offers/")
    out = []
    for j in (data or {}).get("offers", []):
        out.append(_blank(
            url=j.get("careers_url", "") or j.get("careers_apply_url", ""),
            title=_clean(j.get("title")),
            company=company or _clean(j.get("company_name")) or token,
            location=_clean(j.get("location") or ", ".join(
                x for x in (j.get("city"), j.get("country")) if x)),
            department=_clean(j.get("department")),
            posted_at=j.get("published_at", ""), posted_field="recruitee.published_at",
            description=enrich.to_text(j.get("description") or ""),
            remote_hint="Remote" if j.get("remote") else "",
            emp_hint=_clean(j.get("employment_type_code")),
            ats="recruitee", token=token))
    return out


# ---- Breezy -----------------------------------------------------------------
# The list has NO descriptions, so we fetch them lazily (like SmartRecruiters/Workday) — but only
# for jobs that pass the score gate. Location is structured, with an is_remote flag.
def breezy(token: str, company: str = "") -> list[dict]:
    data = _get_json(f"https://{token}.breezy.hr/json")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        loc = j.get("location") or {}
        city = _clean(loc.get("name") or (loc.get("city") if isinstance(loc, dict) else ""))
        out.append(_blank(
            url=j.get("url", "") or f"https://{token}.breezy.hr/p/{j.get('id','')}",
            title=_clean(j.get("name")),
            company=company or _clean(j.get("company")) or token,
            location=city,
            department=_clean((j.get("department") or {}).get("name")
                              if isinstance(j.get("department"), dict) else j.get("department")),
            posted_at=j.get("published_date", ""), posted_field="breezy.published_date",
            remote_hint="Remote" if (loc.get("is_remote") if isinstance(loc, dict) else False) else "",
            emp_hint=_clean(j.get("type", {}).get("name") if isinstance(j.get("type"), dict) else ""),
            # NB: no `detail`. Breezy exposes descriptions ONLY in rendered HTML — no per-job JSON,
            # no JSON-LD. We don't scrape HTML (brittle, and against the spirit of the free-API
            # rule), so Breezy roles are judged on their TITLE, which is-software-title handles
            # fine. The only thing lost is the description-based rescue for a vague title.
            ats="breezy", token=token))
    return out


# ---- Teamtailor -------------------------------------------------------------
# JSON Feed (jsonfeed.org): items[] with content_html inline. No key, no auth.
def teamtailor(token: str, company: str = "") -> list[dict]:
    data = _get_json(f"https://{token}.teamtailor.com/jobs.json")
    if not isinstance(data, dict):
        return []
    name = _clean(data.get("title"))
    out = []
    for j in (data.get("items") or []):
        out.append(_blank(
            url=j.get("url", ""),
            title=_clean(j.get("title")),
            company=company or name or token,
            location="",                               # not in the feed; parsed from desc by enrich
            posted_at=j.get("date_published", ""), posted_field="teamtailor.date_published",
            description=enrich.to_text(j.get("content_html") or j.get("content_text") or ""),
            ats="teamtailor", token=token))
    return out


# ---- Personio ---------------------------------------------------------------
# An XML feed, with descriptions inline. Parse just the fields we need — no XML library, since
# these feeds are simple and predictable, and a regex won't choke on a malformed one.
_P_POS = re.compile(r"<position>(.*?)</position>", re.S)
_P_FIELD = {k: re.compile(rf"<{k}>(.*?)</{k}>", re.S) for k in
            ("id", "name", "office", "department", "employmentType", "recruitingCategory")}
_P_DESC = re.compile(r"<jobDescriptions>(.*?)</jobDescriptions>", re.S)


def personio(token: str, company: str = "") -> list[dict]:
    raw = _get_bytes(f"https://{token}.jobs.personio.de/xml")
    if not raw:
        return []
    xml = raw.decode("utf-8", "replace")
    out = []
    for m in _P_POS.finditer(xml):
        block = m.group(1)

        def f(k):
            mm = _P_FIELD[k].search(block)
            return _html.unescape(_clean(mm.group(1))) if mm else ""
        pid = f("id")
        desc = _P_DESC.search(block)
        out.append(_blank(
            url=f"https://{token}.jobs.personio.de/job/{pid}" if pid else "",
            title=f("name"),
            company=company or token,
            location=f("office"),
            department=f("department"),
            emp_hint=f("employmentType"),
            description=enrich.to_text(desc.group(1)) if desc else "",
            ats="personio", token=token))
    return out


# ---- BambooHR ---------------------------------------------------------------
# The list has no descriptions; each job has a /detail endpoint. Fetch lazily behind the gate.
def bamboohr(token: str, company: str = "") -> list[dict]:
    data = _get_json(f"https://{token}.bamboohr.com/careers/list")
    if not isinstance(data, dict):
        return []
    out = []
    for j in (data.get("result") or []):
        loc = j.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x) if isinstance(loc, dict) else ""
        jid = j.get("id")
        out.append(_blank(
            url=f"https://{token}.bamboohr.com/careers/{jid}" if jid else "",
            title=_clean(j.get("jobOpeningName")),
            company=company or token,
            location=where,
            department=_clean(j.get("departmentLabel")),
            remote_hint="Remote" if (str(j.get("locationType") or "").lower() == "remote"
                                     or j.get("isRemote")) else "",
            emp_hint=_clean(j.get("employmentStatusLabel")),
            detail=f"https://{token}.bamboohr.com/careers/{jid}/detail" if jid else "",
            ats="bamboohr", token=token))
    return out


def _detail_bamboohr(url: str) -> str:
    d = _get_json(url)
    jo = ((d or {}).get("result") or {}).get("jobOpening") or {}
    return enrich.to_text(jo.get("description") or "")


def _pay(cur, lo, hi, per: str = "") -> str:
    """A comp summary in the shape enrich.salary() trusts as an ATS field ('USD 90,000 – 120,000')."""
    try:
        lo = int(float(lo)) if lo not in (None, "", "None") else 0
        hi = int(float(hi)) if hi not in (None, "", "None") else 0
    except (TypeError, ValueError):
        return ""
    if not (lo or hi):
        return ""
    s = f"{_clean(cur)} {lo or hi:,}" + (f" – {hi:,}" if lo and hi and hi != lo else "")
    return s.strip() + (f" per {_clean(per).lower()}" if _clean(per) else "")


# ---- Keka -------------------------------------------------------------------
# The careers page is a shell; its script reads this JSON (robots.txt allows /careers only, and
# this lives under it). Descriptions are inline for most postings; the rest are judged on title.
def keka(token: str, company: str = "") -> list[dict]:
    data = _get_json(f"https://{token}.keka.com/careers/api/jobs/default/active")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        locs = [x for x in (j.get("jobLocations") or []) if isinstance(x, dict)]
        where = "; ".join(dict.fromkeys(
            ", ".join(v for v in (x.get("name"), x.get("countryName")) if v) for x in locs))
        sal = j.get("salaryRange") or {}
        desc = enrich.to_text(j.get("description") or j.get("excerpt") or "")
        if desc and j.get("experience"):
            desc = f"Experience: {_clean(j['experience'])}\n\n{desc}"     # Keka's own field
        out.append(_blank(
            url=f"https://{token}.keka.com/careers/jobdetails/{j.get('id')}" if j.get("id") else "",
            title=_clean(j.get("title")),
            company=company or token,
            location=where,
            department=_clean(j.get("departmentName")),
            posted_at=j.get("publishedOn") or "",
            posted_field="keka.publishedOn",
            description=desc,
            salary_hint=_pay(sal.get("currency"), sal.get("minimum"), sal.get("maximum")),
            remote_hint="Remote" if any("remote" in (x.get("name") or "").lower() for x in locs) else "",
            ats="keka", token=token))
    return out


# ---- Zoho Recruit -----------------------------------------------------------
# No public JSON (its API path is disallowed in robots.txt), but the careers page itself — which
# robots.txt allows — carries every published opening as JSON in a hidden input. The token is the
# careers HOST, since Zoho runs regional domains (.com, .in, .eu …).
# The value is HTML-escaped JSON, so it never holds a '"' — but it can hold a raw '>'.
_ZR_JOBS = re.compile(r'\bvalue="([^"]*)"\s*id="jobs"|\bid="jobs"\s*value="([^"]*)"')


def zoho(token: str, company: str = "") -> list[dict]:
    raw = net.get_bytes(f"https://{token}/jobs/Careers", accept="text/html, */*")
    m = _ZR_JOBS.search(raw.decode("utf-8", "replace")) if raw else None
    try:
        data = json.loads(_html.unescape(m.group(1) or m.group(2))) if m else []
    except ValueError:
        return []
    out = []
    for j in data if isinstance(data, list) else []:
        # A filled role can remain visible with Keep_on_Career_Site=true. Visibility is
        # not an open vacancy: the publisher's explicit Publish=false takes precedence.
        if not isinstance(j, dict) or not j.get("id") or j.get("Publish") is False:
            continue
        title = _clean(j.get("Posting_Title") or j.get("Job_Opening_Name"))
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-") or "job"
        desc = enrich.to_text(j.get("Job_Description") or "")
        if desc and j.get("Work_Experience"):
            desc = f"Experience: {_clean(j['Work_Experience'])}\n\n{desc}"
        out.append(_blank(
            url=f"https://{token}/jobs/Careers/{j['id']}/{slug}",
            title=title,
            company=company or token.split(".")[0],
            location=", ".join(v for v in (_clean(j.get("City")), _clean(j.get("State")),
                                           _clean(j.get("Country"))) if v),
            # An internally opened requisition need not have been published that day.
            posted_at="",
            description=desc,
            emp_hint=_clean(j.get("Job_Type")),
            remote_hint="Remote" if j.get("Remote_Job") is True else "",
            ats="zoho", token=token))
    return out


# ---- Rippling ---------------------------------------------------------------
# A public JSON board API. The list has no descriptions; each job has its own JSON — lazily.
_RIPPLING_API = "https://api.rippling.com/platform/api/ats/v1/board/"


def rippling(token: str, company: str = "") -> list[dict]:
    data = _get_json(f"{_RIPPLING_API}{token}/jobs")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        uid = j.get("uuid")
        where = _clean((j.get("workLocation") or {}).get("label"))
        out.append(_blank(
            url=j.get("url") or (f"https://ats.rippling.com/{token}/jobs/{uid}" if uid else ""),
            title=_clean(j.get("name")),
            company=company or token,
            location=where,
            department=_clean((j.get("department") or {}).get("label")),
            remote_hint="Remote" if "remote" in where.lower() else "",
            detail=f"{_RIPPLING_API}{token}/jobs/{uid}" if uid else "",
            ats="rippling", token=token))
    return out


def _detail_rippling(url: str) -> str:
    d = (_get_json(url) or {}).get("description") or {}
    return enrich.to_text((d.get("role") or "") + " " + (d.get("company") or "")) if isinstance(d, dict) else ""


# ---- Pinpoint ---------------------------------------------------------------
# postings.json: every open posting, description and pay band inline.
def pinpoint(token: str, company: str = "") -> list[dict]:
    data = _get_json(f"https://{token}.pinpointhq.com/postings.json")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("data") or []:
        parts = [j.get("description") or ""]
        for k in ("key_responsibilities", "skills_knowledge_expertise", "benefits"):
            if j.get(k):
                parts.append(f"<h3>{j.get(k + '_header') or ''}</h3>{j[k]}")
        loc = j.get("location") or {}
        visible = j.get("compensation_visible") in (True, "true", "True")
        out.append(_blank(
            url=j.get("url", ""),
            title=_clean(j.get("title")),
            company=company or token,
            location=", ".join(v for v in (_clean(loc.get("city")), _clean(loc.get("province"))) if v)
                     or _clean(loc.get("name")),
            department=_clean(((j.get("job") or {}).get("department") or {}).get("name")),
            description=enrich.to_text(" ".join(parts)),
            salary_hint=_pay(j.get("compensation_currency"), j.get("compensation_minimum"),
                             j.get("compensation_maximum"),
                             j.get("compensation_frequency")) if visible else "",
            emp_hint=_clean(j.get("employment_type_text")),
            remote_hint="Remote" if j.get("workplace_type") == "remote" else "",
            ats="pinpoint", token=token))
    return out


# ---- Manatal ----------------------------------------------------------------
# Public career-page API, paginated, descriptions inline.
_MANATAL_POLITE = None


def _manatal_page(url: str) -> str:
    global _MANATAL_POLITE
    if _MANATAL_POLITE is None:
        from collector.sources import webscout
        _MANATAL_POLITE = webscout.Polite()
    if not _MANATAL_POLITE.allowed(url):
        return ""
    from urllib.parse import urlsplit
    host = urlsplit(url).netloc
    _MANATAL_POLITE._wait(host, _MANATAL_POLITE.crawl_delay(host))
    # Keep the transport's body cache/conditional GET while obeying the site's pacing.
    return _page(url)


def manatal(token: str, company: str = "") -> list[dict]:
    if token.endswith(".careers-page.com"):
        from collector.sources.manatal_list import collect
        return [_blank(url=j["url"], title=_clean(j["title"]), company=company or token,
                       location=_clean(j["location"]), description=_clean(j["description"]),
                       detail=j["url"], ats="manatal", token=token) for j in collect(token, _manatal_page)]
    url, out = f"https://api.manatal.com/open/v3/career-page/{token}/jobs/", []
    for _ in range(20):                                # 20 pages is far past any real board
        data = _get_json(url)
        if not isinstance(data, dict):
            break
        for j in data.get("results") or []:
            out.append(_blank(
                url=f"https://www.careers-page.com/{token}/job/{j.get('hash')}" if j.get("hash") else "",
                title=_clean(j.get("position_name")),
                company=company or token,
                location=_clean(j.get("location_display")) or ", ".join(
                    v for v in (_clean(j.get("city")), _clean(j.get("country"))) if v),
                description=enrich.to_text(j.get("description") or ""),
                emp_hint=_clean(j.get("contract_details")).replace("_", " "),
                remote_hint="Remote" if j.get("is_remote") is True else "",
                ats="manatal", token=token))
        url = data.get("next")
        if not url:
            break
    return out


# ---- Jobvite / Freshteam / JazzHR ---------------------------------------------
# No public JSON list. The list PAGE names each job and links it, and every job page publishes a
# schema.org JobPosting for search engines — the same front door webscout reads. So: title and
# link from the list, description from the JobPosting, lazily and only for software titles.
def _page(url: str) -> str:
    raw = net.get_bytes(url, accept="text/html, */*")
    return raw.decode("utf-8", "replace") if raw else ""


def _detail_jsonld(url: str) -> str:
    from collector.sources import webscout              # local: webscout is the heavier module
    jp = webscout.jsonld_jobs(_page(url))
    d = jp[0].get("description") if jp else ""
    return enrich.to_text(d if isinstance(d, str) else "")


def _text(s: str) -> str:
    return _html.unescape(_clean(re.sub(r"<[^>]+>", " ", s or "")))


def jobvite(token: str, company: str = "") -> list[dict]:
    from collector.sources.jobvite_list import collect
    return [_blank(url=r["url"], title=_clean(r["title"]), company=company or token,
                   location=_clean(r["location"]), detail=r["url"], ats="jobvite", token=token)
            for r in collect(token, _page)]


# Two portal layouts: job cards (the link wraps a <div class="job-title">) and an accordion list
# (the link IS the title, and the location sits in a later link to the same job).
_FT_CARD = re.compile(r'<a href="(/jobs/[^"]+)" class="heading"([^>]*)>.*?'
                      r'<div class="[\w-]*job-title">(.*?)</div>', re.S)     # custom templates prefix it
_FT_LIST = re.compile(r'<a href="(/jobs/[^"]+)" class="job-title">(.*?)</a>(.*?)</li>', re.S)


def freshteam(token: str, company: str = "") -> list[dict]:
    html = _page(f"https://{token}.freshteam.com/jobs")
    found = []
    for path, attrs, title in _FT_CARD.findall(html):
        where = re.search(r'data-portal-location="([^"]*)"', attrs)
        remote = re.search(r'data-portal-remote-location=["\']?true', attrs)
        found.append((path, title, where.group(1) if where else "", bool(remote)))
    for path, title, rest in _FT_LIST.findall(html):
        where = re.search(r'class="location-info">(.*?)(?:<br\s*/?>|</a>)', rest, re.S)
        found.append((path, title, where.group(1) if where else "", False))
    out, seen = [], set()
    for path, title, where, remote in found:
        url = f"https://{token}.freshteam.com{path}"
        if url in seen:
            continue
        seen.add(url)
        out.append(_blank(url=url, title=_text(title), company=company or token,
                          location=_text(where), remote_hint="Remote" if remote else "",
                          detail=url, ats="freshteam", token=token))
    return out


_JZ_ITEM = re.compile(r'<a href="(https://[A-Za-z0-9_-]+\.applytojob\.com/apply/[^"]+)">(.*?)</a>'
                      r'\s*</h3>\s*(?:<ul[^>]*>\s*<li><i class=.fa fa-map-marker.></i>(.*?)</li>)?', re.S)


def jazzhr(token: str, company: str = "") -> list[dict]:
    out, seen = [], set()
    for url, title, where in _JZ_ITEM.findall(_page(f"https://{token}.applytojob.com/apply")):
        if url not in seen:
            seen.add(url)
            out.append(_blank(url=url, title=_text(title), company=company or token,
                              location=_text(where), detail=url, ats="jazzhr", token=token))
    return out


from collector.sources import indiafeeds

ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
            "smartrecruiters": smartrecruiters, "workable": workable, "workday": workday,
            "recruitee": recruitee, "breezy": breezy, "teamtailor": teamtailor,
            "personio": personio, "bamboohr": bamboohr, "keka": keka, "zoho": zoho,
            "rippling": rippling, "pinpoint": pinpoint, "manatal": manatal,
            "jobvite": jobvite, "freshteam": freshteam, "jazzhr": jazzhr,
            "indiafeed": indiafeeds.fetch}
DETAIL = {"smartrecruiters": _detail_smartrecruiters, "workday": _detail_workday,
          "bamboohr": _detail_bamboohr, "rippling": _detail_rippling,
          "jobvite": _detail_jsonld, "freshteam": _detail_jsonld, "jazzhr": _detail_jsonld,
          "indiafeed": indiafeeds.detail, "manatal": _detail_jsonld}


def fetch(ats: str, token: str, company: str = "") -> list[dict]:
    fn = ADAPTERS.get(ats)
    try:
        return fn(token, company) if fn else []
    except Exception:
        return []                                      # one broken board never breaks the poll


def fetch_detail(job: dict) -> str:
    """Lazily pull the description for boards that don't inline it. Called ONLY for postings
    that already passed the score gate, so it costs a few calls, not thousands."""
    fn = DETAIL.get(job.get("ats", ""))
    if not fn or not job.get("detail"):
        return ""
    try:
        return fn(job["detail"])
    except Exception:
        return ""
