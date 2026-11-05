"""Free public feeds and niche India job boards, retaining each publisher's attribution.

These are multi-employer sources. Their names never replace the hiring company. Remote is
not sufficient evidence of India eligibility; unspecified and country-restricted roles stay out.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from collections import OrderedDict
from html.parser import HTMLParser
import html
import json
from pathlib import Path
import re
import threading
import time
import tempfile
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

from collector.sources import enrich, net, regions, webscout

FEEDS = {
    "hasjob": ("Hasjob", "https://hasjob.co/"),
    "fossunited": ("FOSS United", "https://fossunited.org/jobs"),
    "infopark": ("Infopark", "https://infopark.in/companies-job"),
    "remotejava": ("Remote Java Jobs", "https://www.remotejavajobs.com/"),
    "remotefrontend": ("Remote Frontend Jobs", "https://www.remotefrontendjobs.com/"),
    "remotebackend": ("Remote Backend Jobs", "https://www.remotebackendjobs.com/"),
    "remotive": ("Remotive", "https://remotive.com/"),
    "fresherflow": ("FresherFlow India Jobs", "https://fresherflow.github.io/India-Jobs-Internships/"),
    "himalayas": ("Himalayas", "https://himalayas.app/jobs/countries/india"),
}
_polite = webscout.Polite()
_bodies: OrderedDict[str, str] = OrderedDict()
_body_lock = threading.Lock()


def _read(url: str) -> str:
    status, _, body = _polite.get(url)
    with _body_lock:
        if status == 304:
            return _bodies.get(url, "")
        if status == 200:
            _bodies[url] = body
            _bodies.move_to_end(url)
            while len(_bodies) > 64:
                old, _ = _bodies.popitem(last=False)
                _polite._cache.pop(old, None)  # Do not request a 304 after discarding its body.
            return body
    return ""


def india_eligible(location: str) -> bool:
    loc = (location or "").strip()
    if re.search(r"(?:except|excluding|not (?:in|available in)|outside)\s+india\b", loc, re.I):
        return False
    if regions.is_india(loc) or re.search(r"\bncr\b", loc, re.I):
        return True
    # A generic 'remote', 'Asia' or 'APAC' tag does not establish country eligibility.
    return bool(re.fullmatch(r"(?:remote\s*[-–—,:()]?\s*)?(?:worldwide|anywhere|global|anywhere in the world|world)\s*\)?", loc, re.I))


def _job(token: str, **fields) -> dict:
    from collector.sources.adapters import _blank
    fields["posted_field"] = {
        "hasjob": "hasjob.atom.published", "fossunited": "fossunited.data-date",
        "infopark": "infopark.posted_date", "remotejava": "remote-network.isoDate",
        "remotefrontend": "remote-network.isoDate", "remotebackend": "remote-network.isoDate",
        "remotive": "remotive.publication_date", "himalayas": "himalayas.pubDate",
    }.get(token, "")
    if fields.get("description"):
        source_url = FEEDS[token][1] if token == "fresherflow" else fields.get("url", FEEDS[token][1])
        text = re.sub(r"\n\nSource: [^\n]+$", "", fields["description"])
        fields["description"] = credit(token, source_url) + "\n\n" + text
    return _blank(ats="indiafeed", token=token, **fields)


def credit(token: str, url: str) -> str:
    return f"Source: {FEEDS[token][0]} — {url}"


def _save_cache(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False) as out:
        temporary = Path(out.name)
        json.dump(data, out)
    temporary.replace(path)


def _cached_json(token: str, url: str, ttl: int):
    from collector import paths
    cache = Path(paths.DB).parent / f"indiafeed-{token}-api.json"
    now = time.time()
    try:
        saved = json.loads(cache.read_text())
        if now - saved["checked_at"] < ttl:
            return saved.get("data")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    data = net.get_json(url, retries=1)
    # Failed attempts are cached too: retries must not bypass the publisher's polling limit.
    _save_cache(cache, {"checked_at": now, "data": data})
    return data


def hasjob(raw: str) -> list[dict]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    jobs = []
    for entry in root.findall("a:entry", ns):
        location = entry.findtext("a:location", "", ns)
        if not india_eligible(location):
            continue
        content = entry.findtext("a:content", "", ns)
        match = re.search(r"<strong[^>]*>\s*<a[^>]*>(.*?)</a>", content, re.S | re.I)
        company = enrich.to_text(match.group(1)).strip() if match else ""
        link = entry.find("a:link", ns)
        url = link.get("href", "") if link is not None else ""
        if not company or not url.startswith("https://hasjob.co/"):
            continue
        jobs.append(_job("hasjob", title=entry.findtext("a:title", "", ns), company=company,
                         url=url, location=location, posted_at=entry.findtext("a:published", "", ns),
                         description=enrich.to_text(content) + "\n\n" + credit("hasjob", url)))
    return jobs


class FossCards(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jobs = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag != "div" or "job-item" not in attrs.get("class", "").split():
            return
        location = attrs.get("data-location", "")
        if not india_eligible(location):
            return
        route = attrs.get("data-route", "")
        company = attrs.get("data-company-display", "")
        if not re.fullmatch(r"/jobs/[A-Za-z0-9_-]+", route) or not company:
            return
        url = "https://fossunited.org" + route
        self.jobs.append(_job("fossunited", title=attrs.get("data-title", ""), company=company,
                              location=location, url=url, detail=url,
                              posted_at=attrs.get("data-date", ""), emp_hint=attrs.get("data-type", "")))


def infopark_page(raw: str, today: date | None = None) -> list[dict]:
    today = today or date.today()
    jobs = []
    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", raw, re.S | re.I):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", tr, re.S | re.I)
        if len(cells) != 5:
            continue
        posted, title, company, closing = [enrich.to_text(v).strip() for v in cells[:4]]
        try:
            if datetime.strptime(closing, "%d %b %Y").date() < today:
                continue
            posted = datetime.strptime(posted, "%d-%m-%Y").date().isoformat()
        except ValueError:
            # An unparseable deadline must not silently become an evergreen listing.
            continue
        match = re.search(r'href=["\'](https://infopark.in/company-jobs/details/\d+/\d+)["\']', cells[4])
        if not match or not title or not company:
            continue
        url = match.group(1)
        jobs.append(_job("infopark", title=title, company=company, url=url, detail=url,
                         location="Kerala, India", posted_at=posted,
                         expires_at=datetime.strptime(closing, "%d %b %Y").strftime("%Y-%m-%d 18:30:00")))
    return jobs


def remote_network(data: list, token: str) -> list[dict]:
    jobs = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        # geoRegions=Asia also includes Singapore-only and Pakistan-only jobs. Use actual location.
        location = entry.get("location") or entry.get("geoLabel") or ""
        if not isinstance(location, str) or not india_eligible(location):
            continue
        identifier = entry.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
            continue
        company = entry.get("company") or {}
        company = company.get("name") if isinstance(company, dict) else ""
        company = company or entry.get("author", "")
        if not company or not entry.get("title"):
            continue
        # Publisher requires linking to its page, not replacing it with the downstream apply URL.
        # The same listing can belong to several boards. Pick one publisher page consistently
        # across those feeds so the normal URL upsert prevents duplicate jobs.
        sites = set((entry.get("sites") or "").strip(",").split(","))
        canonical = next((name for site, name in (("java", "remotejava"), ("frontend", "remotefrontend"),
                                                  ("backend", "remotebackend")) if site in sites), token)
        url = FEEDS[canonical][1] + identifier
        jobs.append(_job(token, title=entry["title"], company=company, location=location,
                         url=url, posted_at=entry.get("isoDate") or "",
                         description=enrich.to_text(entry.get("contentSnippet") or "") + "\n\n" + credit(canonical, url),
                         salary_hint=entry.get("salary") or "", remote_hint="Remote"))
    return jobs


def remotive(data: dict) -> list[dict]:
    jobs = []
    for entry in data.get("jobs", []) if isinstance(data, dict) else []:
        location = entry.get("candidate_required_location") or ""
        url = entry.get("url") or ""
        if not india_eligible(location) or urlsplit(url).hostname != "remotive.com":
            continue
        jobs.append(_job("remotive", title=entry.get("title", ""), company=entry.get("company_name", ""),
                         url=url, location=location, posted_at=entry.get("publication_date", ""),
                         description=enrich.to_text(entry.get("description") or "") + "\n\n" + credit("remotive", url),
                         salary_hint=entry.get("salary") or "", emp_hint=entry.get("job_type") or "", remote_hint="Remote"))
    return jobs


def fresherflow(data: dict) -> list[dict]:
    jobs = []
    for entry in data.get("jobs", []) if isinstance(data, dict) else []:
        if entry.get("status") != "PUBLISHED":
            continue
        locations = entry.get("locations")
        location = "; ".join(s for s in locations if isinstance(s, str)) if isinstance(locations, list) else ""
        url = entry.get("applyLink") or ""
        from collector.sources.net import public_url
        if not india_eligible(location) or not public_url(url) or not entry.get("company") or not entry.get("title"):
            continue
        jobs.append(_job("fresherflow", title=entry["title"], company=entry["company"], url=url,
                         # dateAdded records discovery by this aggregator, not publication.
                         location=location, posted_at="",
                         description=(entry.get("notesHighlights") or "") + "\n\n" + credit("fresherflow", FEEDS["fresherflow"][1]),
                         emp_hint="Internship" if entry.get("type") == "INTERNSHIP" else "",
                         remote_hint="Remote" if entry.get("workMode") == "REMOTE" else ""))
    return jobs


def himalayas(data: dict, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    jobs = []
    for entry in data.get("jobs", []) if isinstance(data, dict) else []:
        countries, zones = entry.get("locationRestrictions"), entry.get("timezoneRestrictions")
        # An explicitly empty country restriction means worldwide per the API contract.
        if not isinstance(countries, list) or countries and not any(c in ("India", "IN") for c in countries):
            continue
        if not isinstance(zones, list) or zones and 5.5 not in zones:
            continue
        expiry = entry.get("expiryDate")
        if not isinstance(expiry, (int, float)) or expiry <= now:
            continue
        url, guid = entry.get("applicationLink", ""), entry.get("guid", "")
        if urlsplit(url).hostname != "himalayas.app" or urlsplit(guid).hostname != "himalayas.app":
            continue
        if not entry.get("title") or not entry.get("companyName"):
            continue
        jobs.append(_job("himalayas", title=entry["title"], company=entry["companyName"], url=url,
                         location="Remote, India" if countries else "Worldwide",
                         posted_at=datetime.fromtimestamp(entry["pubDate"], timezone.utc).isoformat() if isinstance(entry.get("pubDate"), (int, float)) else "",
                         expires_at=datetime.fromtimestamp(expiry, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                         description=enrich.to_text(entry.get("description") or "") + "\n\n" + credit("himalayas", guid),
                         emp_hint=entry.get("employmentType") or "", remote_hint="Remote"))
    return jobs


def _himalayas_feed() -> list[dict]:
    from collector import paths
    cache = Path(paths.DB).parent / "indiafeed-himalayas.json"
    now = time.time()
    saved = {}
    try:
        saved = json.loads(cache.read_text())
        if now - saved["checked_at"] < saved.get("ttl", 86400):
            return himalayas(saved, now)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if not isinstance(saved, dict):
        saved = {}
    resume = (saved and not saved.get("complete") and saved.get("next_page", 1) > 1
              and now - saved.get("started_at", 0) < 86400)
    jobs = list(saved.get("jobs", [])) if resume else []
    seen, complete = {j["guid"] for j in jobs if j.get("guid")}, False
    start_page = saved["next_page"] if resume else 1
    next_page = start_page
    # Public data refreshes daily. Persist a daily cache across watcher restarts, make paced
    # requests, stop on any failed/blocked response, and cap the search at 2,000 results.
    for page in range(start_page, 101):
        next_page = page
        if page > start_page:
            time.sleep(1.1)
        data = net.get_json(f"https://himalayas.app/jobs/api/search?q=software&country=IN&sort=recent&page={page}", retries=1)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            break
        batch = data["jobs"]
        for job in batch:
            if job.get("guid") and job["guid"] not in seen:
                seen.add(job["guid"]); jobs.append(job)
        if not batch or page * 20 >= data.get("totalCount", 2001):
            complete = True
            break
        next_page = page + 1
    capped = next_page > 100
    snapshot = {"checked_at": now, "started_at": saved["started_at"] if resume else now,
                "ttl": 86400 if complete or capped else 3600, "complete": complete,
                "next_page": 1 if complete or capped else next_page, "jobs": jobs}
    _save_cache(cache, snapshot)
    return himalayas(snapshot, now)


def fetch(token: str, company: str = "") -> list[dict]:
    if token not in FEEDS:
        return []
    if token == "fresherflow":
        return fresherflow(_cached_json(token, "https://raw.githubusercontent.com/FresherFlow/India-Jobs-Internships/main/data/jobs.json", 21600))
    if token == "himalayas":
        return _himalayas_feed()
    if token.startswith("remote") and token != "remotive":
        board = {"remotejava": "java", "remotefrontend": "frontend", "remotebackend": "backend"}[token]
        # Documented, keyless API. One capped request, no pagination beyond publisher's 250 limit.
        data = _cached_json(token, f"{FEEDS[token][1]}api/jobs?board={board}&regions=asia,worldwide&limit=250", 21600)
        return remote_network(data, token)
    if token == "remotive":
        return remotive(_cached_json(token, "https://remotive.com/api/remote-jobs?category=software-dev&limit=500", 21600))
    if token == "hasjob":
        body = _read("https://hasjob.co/feed")
        return hasjob(body) if body else []
    if token == "fossunited":
        body = _read(FEEDS[token][1])
        parser = FossCards()
        if body:
            parser.feed(body)
        return parser.jobs
    jobs, seen = [], set()
    for page in range(1, 31):
        url = FEEDS[token][1] + (f"?page={page}" if page > 1 else "")
        body = _read(url)
        if not body:
            break
        for job in infopark_page(body):
            if job["url"] not in seen:
                seen.add(job["url"])
                jobs.append(job)
        if not re.search(rf'companies-job\?page={page + 1}(?:["\'&])', html.unescape(body)):
            break
    return jobs


def detail(url: str) -> str:
    host = urlsplit(url).hostname
    token = {"fossunited.org": "fossunited", "infopark.in": "infopark"}.get(host)
    if not token:
        return ""
    body = _read(url)
    if not body:
        return ""
    postings = webscout.jsonld_jobs(body)
    description = next((p.get("description") for p in postings if p.get("description")), "")
    if not description:
        match = re.search(r"<(?:main|article)\b[^>]*>(.*?)</(?:main|article)>", body, re.S | re.I)
        description = match.group(1) if match else body
    return credit(token, url) + "\n\n" + enrich.to_text(description)
