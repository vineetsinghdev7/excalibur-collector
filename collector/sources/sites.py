"""Add a company by its website, and read the jobs on its own careers site every round.

    python -m excalibur.finder add-site https://acme.com [https://other.com …]
    python -m excalibur.finder add-site companies.txt          # one website per line

Or the "Add a company's website" box on the Companies page.

WHAT HAPPENS TO A WEBSITE
  1. Find its careers page: links on the homepage ("careers", "jobs", "join us"…), or the usual
     paths. A careers URL you give is tried first.
  2. If the careers page is a hiring system the finder reads (Greenhouse, Lever, Keka … all of
     adapters.ADAPTERS), the board joins the watchlist and is read like any other.
  3. Otherwise, find the job data the site publishes for search engines (schema.org JobPosting):
     on the careers page itself, on the job pages it links to, or on the job pages its sitemap
     lists. That becomes a RECIPE, saved in `site_recipes`, and the board joins the watchlist as
     ats "site", so every round reads it again, scores it and closes what it stops listing.
  4. The jobs are read and saved at once; you don't wait for the next round.
  5. If none of that works it says why, in plain words, and saves nothing.

Recipes written by hand or by the OpenCode site scout (~/excalibur-sitescout) use the same format
and are imported with `add-site --recipes recipes.jsonl`. Their methods are those above plus
"json" (a public JSON list the careers page loads) and "rss". "links" is "jsonld" for job pages
that carry no job data: their heading becomes the title and their text the description.

Politeness is webscout.Polite's: robots.txt first, one request per host every 2 s, honest name,
stop on 403/429, no logins and nothing that gets past a bot check.
"""
from __future__ import annotations



import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime


from collector.sources import adapters, enrich, regions, scorer, webscout

MAX_DISCOVER_PAGES = 14     # pages one discovery may fetch (it walks two links deep)
MAX_NEW_JOB_PAGES = 25      # job pages one READ may fetch; the rest wait for the next read
MAX_LIST_LINKS = 400        # job links taken from one list page or sitemap
MAX_NEW_RENDERS = 10        # job pages one read may open in the browser (slower, so fewer)

_A = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_SITEMAP_INDEX = re.compile(r"<sitemapindex\b", re.I)
_JOB_LIST_PAGE = re.compile(r"(?i)/(?:jobs?|careers?|openings?|positions?|vacanc(?:y|ies)|"
                            r"open-(?:positions|roles|jobs)|all-jobs|job-openings|current-openings)/?$")

_POLITE: dict = {"p": None, "at": 0.0}
_POLITE_LOCK = threading.Lock()


def _polite() -> webscout.Polite:
    """One Polite per process (robots.txt and pacing are per host), renewed daily."""
    with _POLITE_LOCK:
        if _POLITE["p"] is None or time.time() - _POLITE["at"] > 86400:
            _POLITE.update(p=webscout.Polite(), at=time.time())
        return _POLITE["p"]


# ── reading a recipe ──────────────────────────────────────────────────────────────────────────────
def _text(v) -> str:
    if isinstance(v, dict):
        v = v.get("name") or v.get("value") or ""
    if isinstance(v, list):
        return ", ".join(filter(None, (_text(item) for item in v)))
    return html.unescape(str(v or "")).strip()          # "Lead &#8211; Data" → "Lead – Data"


def _location(p: dict) -> str:
    places = []
    locs = p.get("jobLocation") or []
    for loc in (locs if isinstance(locs, list) else [locs]):
        a = (loc or {}).get("address") if isinstance(loc, dict) else None
        if isinstance(a, dict):
            parts = [_text(a.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry")]
            places.append(", ".join(x for x in parts if x))
        elif isinstance(a, str):
            places.append(a)
    if str(p.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        req = p.get("applicantLocationRequirements") or []
        where = ", ".join(_text(r) for r in (req if isinstance(req, list) else [req]) if _text(r))
        places.insert(0, f"Remote ({where})" if where else "Remote")
    return "; ".join(x for x in places if x)[:200]


def _job_from_posting(p: dict, url: str, company: str, token: str) -> dict:
    from collector.sources.posting_dates import instant
    org = _text(p.get("hiringOrganization"))
    emp = p.get("employmentType") or ""
    expiry = instant(p.get("validThrough"))
    posted = str(p.get("datePosted") or "")
    # Polymer labels its timestamp explicitly as UTC. Canonical ISO keeps that exact instant
    # usable by both Python and the existing mobile parser; never infer a missing timezone.
    utc = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2}(?:\.\d+)?) UTC", posted)
    if utc:
        posted = f"{utc[1]}T{utc[2]}Z"
    return {"url": url, "title": _text(p.get("title"))[:200], "company": company or org,
            "location": _location(p), "department": "",
            "posted_at": posted, "posted_field": "JobPosting.datePosted",
            "description": enrich.to_text(str(p.get("description") or "")),
            "emp_hint": ", ".join(emp) if isinstance(emp, list) else str(emp),
            # An unspecified timezone cannot establish an exact closing instant.
            "expires_at": expiry.strftime("%Y-%m-%d %H:%M:%S") if expiry else "",
            "expiry_checked": not p.get("validThrough") or expiry is not None,
            "ats": "site", "token": token}




def _links(html: str, base: str, rx: re.Pattern | None, texts: dict | None = None) -> list[str]:
    """Links on a page (matching rx); their visible text goes into `texts` when given."""
    out, seen = [], set()
    for href, inner in _A.findall(html or ""):
        u = urllib.parse.urljoin(base, href.strip()).split("#")[0]
        if u in seen or (rx and not rx.search(u)):
            continue
        seen.add(u)
        out.append(u)
        if texts is not None:
            texts[u] = re.sub(r"\s+", " ", _TAG.sub(" ", inner)).strip()[:200]
    return out[:MAX_LIST_LINKS]


_DROP = re.compile(r"(?is)<(script|style|noscript|nav|header|footer|svg|form)\b.*?</\1>")
_MAIN = re.compile(r"(?is)<(main|article)\b[^>]*>(.*)</\1>")
_H1 = re.compile(r"(?is)<h1\b[^>]*>(.*?)</h1>")
_TITLE = re.compile(r"(?is)<title\b[^>]*>(.*?)</title>")


_GENERIC_H1 = re.compile(r"(?i)^(careers?|jobs?|join us|open (?:roles|positions)|work with us|apply)$")
_PARTS = re.compile(r"\s*[·•|–—]\s*")


def _clean_title(raw: str, company: str) -> str:
    """A page's <title> or a link's text, without the company name and the trimmings around it:
    "Cursor · Account Executive - Commercial" → "Account Executive - Commercial";
    "Software Engineer·Full-time·RemoteApply →" → "Software Engineer"."""
    raw = re.sub(r"(?i)\s*apply\s*(?:now)?\s*[→>»]*\s*$", "", raw or "").strip()
    parts = [x for x in _PARTS.split(raw) if x.strip()]
    co = (company or "").strip().lower().removeprefix("www.")
    names = {co, co.split(".")[0], re.sub(r"\W", "", co.split(".")[0]), "careers", "jobs", "careers at"}
    keep = [x for x in parts if x.strip().lower() not in names
            and re.sub(r"\W", "", x.lower()) not in names]
    return (keep[0] if keep else "").strip()


def _location_hint(link_text: str, text: str) -> str:
    """Where a plain job page's job is: a place named in its link's text ("…·San Francisco"),
    else the Indian cities its opening lines name. Empty when it doesn't say."""
    for part in _PARTS.split(link_text or "")[1:]:
        part = re.sub(r"(?i)\s*(?:apply\s*(?:now)?|view\s+(?:role|job|details))\s*[→>»]*\s*$", "", part).strip()
        if part and (re.search(r"(?i)\bremote\b", part) or regions.first(part) != "Other"):
            return part[:120]
    cities = regions.india_cities(text[:4000])
    return f"{', '.join(cities)}, India" if cities else ""


def _job_from_html(html: str, url: str, company: str, token: str, link_text: str = "") -> dict | None:
    """A job page with no job data for search engines: its heading (or its <title>, or its link's
    text) is the title, its main text the description, and the place from the link or from the
    Indian cities its opening lines name."""
    clean = _DROP.sub(" ", html)          # scripts out FIRST: their text may contain "<main>"/"<h1>"
    h = _H1.search(clean)
    title = enrich.to_text(h.group(1)) if h else ""
    if not title or _GENERIC_H1.match(title) or title.lower() == (company or "").lower():
        title = (_clean_title(enrich.to_text((_TITLE.search(html) or [None, ""])[1]), company)
                 or _clean_title(link_text, company))
    m = _MAIN.search(clean)
    text = enrich.to_text(m.group(2) if m else clean)
    if not title or len(text) < 200:
        return None                       # a stub or an error page, not a job
    return {"url": url, "title": title[:200], "company": company,
            "location": _location_hint(link_text, text), "department": "",
            "posted_at": "", "description": text[:20000], "ats": "site", "token": token}


def _sitemap_urls(xml: str) -> list[str]:
    return list(_sitemap_entries(xml))


def _sitemap_entries(xml: str) -> dict[str, str]:
    """URL/lastmod pairs, including namespaced sitemaps and XML-escaped query strings."""
    if not xml or len(xml) > 4_000_000 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", xml, re.I):
        return {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    out = {}
    for entry in root:
        if entry.tag.rsplit("}", 1)[-1] not in ("url", "sitemap"):
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in entry}
        url = fields.get("loc", "")
        if url.startswith(("https://", "http://")):
            out[url] = fields.get("lastmod", "")
    return out


def _modified_at(raw) -> float:
    """A crawl hint only. Never a publication date; ambiguous/future times are ignored."""
    from collector.sources.posting_dates import instant
    value = instant(raw)
    stamp = value.timestamp() if value else 0
    return stamp if 0 < stamp <= time.time() else 0




RECHECK_DAYS = 14          # a page read and not kept (a sales job, say) is left alone this long


def _checked_recently(urls, modified=None): return set()




def _jobs_from_pages(urls: list[str], company: str, token: str, polite,
                     plain: bool = False, texts: dict | None = None,
                     rendered: bool = False, modified: dict | None = None) -> list[dict]:
    """Every URL as a job: saved ones from the database, new ones from their own page (its job
    data, or with `plain` its heading and text when it has none). New pages are opened software
    titles first, at most MAX_NEW_JOB_PAGES (MAX_NEW_RENDERS in the browser) per read; pages
    opened recently and not kept are skipped, so each read gets further down the list."""
    known, out = _known(urls), []
    for u in urls:
        if u in known:
            k = known[u]
            out.append({"url": u, "title": k["title"], "company": k["company"] or company,
                        "location": k["location"], "posted_at": k["posted_at"],
                        "posted_field": k["posted_field"], "posted_checked_at": k["posted_checked_at"],
                        "description": k["description"], "department": k["department"] or "",
                        "ats": "site", "token": token})
    from collector.sources.posting_dates import instant
    checked = {u: instant(k["posted_checked_at"]) for u, k in known.items()}
    changes = {u: _modified_at(raw) for u, raw in (modified or {}).items()}
    changed = {u for u in urls if changes.get(u, 0) > (checked[u].timestamp() if checked.get(u) else 0)}
    fresh = [u for u in urls if u not in known or not checked[u] or u in changed
             or time.time() - checked[u].timestamp() >= 86400]
    # Rejected pages keep the longer retry interval; saved jobs get daily date revalidation.
    skip = _checked_recently([u for u in fresh if u not in known], modified=modified)
    fresh = [u for u in fresh if u not in skip]
    fresh.sort(key=lambda u: (0 if u in changed else 1 if u not in known else 2,
                             not scorer.is_software_title(_clean_title((texts or {}).get(u, ""), company)),
                             -changes.get(u, 0)))
    opened: list[str] = []
    for u in fresh[:MAX_NEW_RENDERS if rendered else MAX_NEW_JOB_PAGES]:
        opened.append(u)
        code, final, html, _ = _get(u, polite, rendered)
        if code in (403, 429):
            opened.pop()
            break                         # the host asked us to stop
        if code != 200:
            opened.pop()               # transient failures must not suppress retries for 14 days
            continue
        postings = webscout.jsonld_jobs(html)
        if postings:
            out = [j for j in out if j["url"] != u]
            out.append(_job_from_posting(postings[0], final or u, company, token))
        elif plain:
            j = _job_from_html(html, final or u, company, token, (texts or {}).get(u, ""))
            if j:
                out = [old for old in out if old["url"] != u]
                out.append(j)
    _mark_checked(opened)
    return out


def _dig(obj, path: str):
    for part in [x for x in (path or "").split(".") if x]:
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def run(recipe: dict, company: str = "", token: str = "") -> list[dict]:
    """The jobs a recipe lists today, in the shape every reader returns."""
    polite, m = _polite(), recipe.get("method")
    company = company or recipe.get("company") or ""
    token = token or recipe.get("token") or ""
    rx = re.compile(recipe["link_regex"]) if recipe.get("link_regex") else None
    if m in ("jsonld", "links", "render"):
        urls, jobs, texts = [], [], {}
        for page in [recipe["list_url"], *(recipe.get("more_list_urls") or [])][:6]:
            code, final, html, _ = _get(page, polite, rendered=m == "render")
            if code != 200:
                continue
            if rx is None:                # the list page carries the postings itself
                for p in webscout.jsonld_jobs(html):
                    # a posting without its own address gets one, so jobs don't overwrite each other
                    u = p.get("url") or f"{final}#{urllib.parse.quote(_text(p.get('title'))[:80])}"
                    jobs.append(_job_from_posting(p, urllib.parse.urljoin(final, u), company, token))
            else:
                urls += [u for u in _links(html, final, rx, texts) if u not in urls]
        return jobs + _jobs_from_pages(urls, company, token, polite, plain=m != "jsonld", texts=texts,
                                       rendered=recipe.get("job_pages") == "render")
    if m == "sitemap":
        code, _, xml = polite.get(recipe["sitemap_url"], limit=4_000_000)
        entries = _sitemap_entries(xml) if code == 200 else {}
        urls = [u for u in entries if not rx or rx.search(u)][:MAX_LIST_LINKS]
        return _jobs_from_pages(urls, company, token, polite, modified=entries) if code == 200 else []
    if m == "json":
        code, _, body = polite.get(recipe["json_url"], limit=8_000_000)
        try:
            items = _dig(json.loads(body), recipe.get("items_path", "")) if code == 200 else None
        except ValueError:
            items = None
        f, out = recipe.get("fields") or {}, []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            url = f.get("url") or ""
            url = (url.format(**{k: v for k, v in it.items() if isinstance(v, (str, int))})
                   if "{" in url else str(_dig(it, url) or ""))
            title = str(_dig(it, f.get("title", "")) or "").strip()
            if title and url.startswith("http"):
                out.append({"url": url, "title": title[:200], "company": company,
                            "location": _text(_dig(it, f.get("location", "")))[:200],
                            # Recipes with an ambiguous date field stay unknown until audited.
                            "posted_at": str(_dig(it, f.get("posted", "")) or ""),
                            "posted_field": "JobPosting.datePosted" if f.get("posted") == "datePosted" else "",
                            "description": enrich.to_text(str(_dig(it, f.get("description", "")) or "")),
                            "department": "", "ats": "site", "token": token})
        return out
    if m == "rss":
        code, _, xml = polite.get(recipe["feed_url"])
        out = []
        for item in re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", xml if code == 200 else "", re.S):
            title = re.search(r"<title[^>]*>(.*?)</title>", item, re.S)
            link = (re.search(r"<link[^>]*href=\"([^\"]+)\"", item) or re.search(r"<link>(.*?)</link>", item, re.S))
            desc = re.search(r"<(?:description|summary|content)[^>]*>(.*?)</(?:description|summary|content)>", item, re.S)
            if title and link:
                strip = lambda s: enrich.to_text(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S))
                out.append({"url": link.group(1).strip(), "title": strip(title.group(1))[:200],
                            "company": company, "location": "", "posted_at": "",
                            "description": strip(desc.group(1)) if desc else "", "department": "",
                            "ats": "site", "token": token})
        return out
    return []


# ── the recipes, stored next to the jobs ─────────────────────────────────────────────────────────

# The cloud runner never reads the application database or launches a browser.
def _known(urls): return {}
def _checked(urls): return set()
def _mark_checked(urls): pass
def _get(url, polite, rendered=False):
    if rendered: return 0, url, "", None
    code, final, body = polite.get(url, limit=2_000_000)
    return code, final, body, None
