"""Web scout — reach the companies whose jobs we can't get from an ATS API.

THE GAP THIS FILLS
The catalog holds 18,718 companies known only by a homepage: they said "we're hiring" on Hacker
News, and that's all we have. No board, no token, no feed. They are invisible to every adapter.

WHAT ACTUALLY WORKS, MEASURED — NOT ASSUMED
Four routes were probed against real companies out of the catalog before any of this was written:

  ATS FINGERPRINTING          ✅ works. A company's own careers page nearly always links, embeds
                              or redirects to the ATS it rents. The token is right there in the
                              URL. Found lever/firmex, lever/scality, bamboohr/bitcoin on a first
                              pass — 12% of a deliberately stale HN sample, and that sample was
                              full of dead startups and non-companies (itunes.apple.com).
                              THIS IS THE ONE THAT PAYS. A hit costs nothing further: the company
                              joins the watch list and a normal adapter reads it forever after.

  ROBOTS + SITEMAPS           ✅ works, secondary. Most hosts return robots.txt and declare a
                              sitemap; job-like URLs turn up in them for in-house careers sites.
                              Also the polite thing to read FIRST — see below.

  JSON-LD (schema.org)        ⚠️  weaker than expected. Google requires JobPosting markup to list
                              a job, so it should be everywhere — but the ATS pages that host most
                              postings are JavaScript-rendered and ship none in their HTML
                              (checked Greenhouse and Lever job pages directly: zero blocks). Kept
                              as a third pass because server-rendered in-house sites DO carry it,
                              which is exactly the population this module targets.

  RSS / ATOM                  ❌ effectively dead on company careers pages. Tried the five usual
                              paths across the sample: nothing. Kept only as a cheap last look.

NOT GETTING BLOCKED, THE ONLY WAY THAT LASTS
There is no clever trick here and this file will never contain one — no proxy rotation, no forged
user-agents, no CAPTCHA solving. Those get you a permanent ban rather than a temporary one, and
they'd contradict the rule the applier already holds ("anti-bot: detect and stop, never solve").
What this does instead is be a guest worth having:

  * READS robots.txt FIRST and obeys it, including Crawl-delay. A site that wants to be left
    alone is left alone.
  * IDENTIFIES ITSELF honestly, with contact info, so an admin can complain instead of ban.
  * RATE-LIMITS PER HOST, not globally. Concurrency across different hosts is free; two rapid
    requests to the SAME host is what trips a rate limiter.
  * GIVES UP FAST — at most a handful of pages per company, then never again (result cached).
  * CONDITIONAL GET, so a re-check of an unchanged page costs the server almost nothing.
  * STOPS ON 403/429 and records it, rather than retrying into a ban.

    python -m excalibur.finder.webscout --limit 200      # scout 200 unresolved companies
    python -m excalibur.finder.webscout --stats          # what it has found so far
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed




from collector import config


UA = config.user_agent("0.3")                      # one identity for the whole app

HOST_DELAY = 2.0          # min seconds between two requests to the SAME host
MAX_PAGES = 5             # per company, then give up — this is a scout, not a crawl
TIMEOUT = 12
WORKERS = 8               # across DIFFERENT hosts; per-host serialisation is enforced separately
FETCH_CAP = 400_000       # bytes; a careers page that needs more than this isn't giving us a token

# Token-bearing URL shapes. Order matters only in that the first match wins, and every one of
# these is a platform whose token is enough to poll the board forever after.
ATS_PATTERNS: tuple[tuple[str, str], ...] = (
    # greenhouse's embed form carries the token in a query param, not the path — matching the
    # path alone captured the literal word "embed" as the token on the first run.
    ("greenhouse", r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)"),
    ("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)"),   # "willowhealth.com" is a real token
    ("lever", r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),      # %20: "The%20Browser%20Company" is one
    ("ashby", r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.-]+)"),
    ("smartrecruiters", r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("workable", r"apply\.workable\.com/(?:api/v1/widget/accounts/)?([A-Za-z0-9_-]+)"),
    ("workday", r"([A-Za-z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([A-Za-z0-9_-]+)"),
    ("recruitee", r"([A-Za-z0-9_-]+)\.recruitee\.com"),
    ("breezy", r"([A-Za-z0-9_-]+)\.breezy\.hr"),
    ("teamtailor", r"([A-Za-z0-9_-]+)\.teamtailor\.com"),
    ("personio", r"([A-Za-z0-9_-]+)\.jobs\.personio\.(?:de|com)"),
    ("bamboohr", r"([A-Za-z0-9_-]+)\.bamboohr\.com/(?:careers|jobs)"),
    ("jazzhr", r"([A-Za-z0-9_-]+)\.applytojob\.com"),
    ("jobvite", r"jobs\.jobvite\.com/(?:careers/)?([A-Za-z0-9_-]+)"),
    ("rippling", r"ats\.rippling\.com/(?:[a-z]{2}-(?:[a-z]{2}|\d{3})/)?([A-Za-z0-9_-]+)"),
    ("pinpoint", r"([A-Za-z0-9_-]+)\.pinpointhq\.com"),
    ("keka", r"([A-Za-z0-9_-]+)\.keka\.com/careers"),
    # Zoho runs regional domains, so the token is the whole careers host.
    ("zoho", r"([A-Za-z0-9_-]+\.zohorecruit\.(?:com\.au|com|in|eu|jp))"),
    ("freshteam", r"([A-Za-z0-9_-]+)\.freshteam\.com"),
    ("manatal", r"careers-page\.com/([A-Za-z0-9_-]+)"),
    ("manatal", r"\b((?!www\.)[A-Za-z0-9_-]+\.careers-page\.com)\b"),
    # platforms with no adapter yet — recorded, not polled, so the gap stays measurable
    ("icims", r"careers-([A-Za-z0-9_-]+)\.icims\.com"),
    # Eightfold hosts most of its customers on ONE shared domain (app.eightfold.ai) and identifies
    # the company with a ?domain= param — matching the subdomain alone harvested the token "app"
    # from 10x Genomics, which is not a company, it's Eightfold's own front door.
    ("eightfold", r"eightfold\.ai/careers\?[^\"'\s]*domain=([A-Za-z0-9.-]+)"),
    # The lookbehind is load-bearing: without it the engine simply restarts one character later,
    # so "app.eightfold.ai" slips past the (?!app\.) guard and yields the token "pp".
    ("eightfold", r"(?<![A-Za-z0-9_.-])(?!app\.)([A-Za-z0-9_-]+)\.eightfold\.ai"),
    ("taleo", r"([A-Za-z0-9_-]+)\.taleo\.net"),
)
_COMPILED = tuple((n, re.compile(p, re.I)) for n, p in ATS_PATTERNS)

# Hosts that are never a company's own careers site — following these wastes requests and can
# look like scraping a third party.
SKIP_HOSTS = ("linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
              "youtube.com", "github.com", "medium.com", "glassdoor.com", "indeed.com",
              "crunchbase.com", "angel.co", "wellfound.com", "itunes.apple.com", "apps.apple.com")

_CAREERS_HREF = re.compile(
    r'href=["\']([^"\']{1,300}?)["\'][^>]{0,200}?>[^<]{0,60}?'
    r'(?:careers?|jobs|join\s+us|we[\'’]?re\s+hiring|open\s+roles|work\s+with\s+us)', re.I)
_CAREERS_URLISH = re.compile(
    r'href=["\']([^"\']*(?:/careers?|/jobs|/join-us|/open-roles|/opportunities)[^"\']*)["\']', re.I)
_LDJSON = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_JOBISH = re.compile(r"(?i)/(?:job|jobs|career|careers|position|opening|vacanc|role)s?[/?-]")


# ---------------------------------------------------------------------------
# Politeness
# ---------------------------------------------------------------------------
class Polite:
    """Robots-aware public page reader; source APIs use their documented public endpoints."""
    def __init__(self):
        self._robots={}; self._cache={}; self._last={}; self._lock=threading.Lock(); self.blocked=set()
    def allowed(self,url):
        from collector.sources import net
        host=urllib.parse.urlsplit(url).netloc
        with self._lock:
            if host not in self._robots:
                code,_,body=net.request('https://'+host+'/robots.txt',retries=0)
                rp=urllib.robotparser.RobotFileParser()
                if code==200:rp.parse(body.decode('utf-8','replace').splitlines())
                elif code in (404,410):rp.parse([])
                else:rp.parse(['User-agent: *','Disallow: /'])
                self._robots[host]=rp
            return self._robots[host].can_fetch(UA,url)
    def crawl_delay(self,host):
        rp=self._robots.get(host)
        return max(2,(rp.crawl_delay(UA) or 0) if rp else 0)
    def _wait(self,host,delay=2):
        from collector.sources import net
        with self._lock:
            at=max(time.monotonic(),self._last.get(host,0)+delay);self._last[host]=at
        if at>=net.DEADLINE:return False
        time.sleep(max(0,at-time.monotonic()))
        return True
    def get(self,url,limit=FETCH_CAP):
        from collector.sources import net
        host=urllib.parse.urlsplit(url).netloc
        if time.monotonic()>=net.DEADLINE or not net.public_url(url) or not self.allowed(url):return -1,url,''
        if not self._wait(host,self.crawl_delay(host)):return 0,url,''
        code,final,body=net.request(url,retries=0)
        return code,final,body[:limit].decode('utf-8','replace')



# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def fingerprint(text: str) -> tuple[str, str]:
    """Find an ATS token in a URL or a page's HTML. → (ats, token) or ('', '')."""
    for name, pat in _COMPILED:
        m = pat.search(text or "")
        if not m:
            continue
        if name == "workday" and m.lastindex and m.lastindex >= 3:
            return name, f"{m.group(1)}|{m.group(2)}|{m.group(3)}"   # tenant|wdN|site
        tok = m.group(1)
        # Shared/infrastructure hostnames and path fragments are not company tokens. Every one of
        # these was harvested as a "board" on a live run before the list existed.
        if tok.lower() in ("www", "jobs", "careers", "career", "embed", "api", "static", "assets",
                           "cdn", "app", "apply", "portal", "employer", "recruiting", "hire",
                           "my", "secure", "web", "public", "en", "us"):
            continue
        return name, tok
    return "", ""


def jsonld_jobs(html: str) -> list[dict]:
    """schema.org JobPosting blocks — published FOR machines, so reading them is the front door."""
    out = []
    for blk in _LDJSON.findall(html or ""):
        try:
            d = json.loads(blk.strip())
        except Exception:
            continue
        stack = d if isinstance(d, list) else [d]
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            if "@graph" in item and isinstance(item["@graph"], list):
                stack.extend(item["@graph"])
            t = item.get("@type") or ""
            types = t if isinstance(t, list) else [t]
            if any("JobPosting" in str(x) for x in types):
                out.append(item)
    return out


def _candidate_careers_urls(base: str, html: str) -> list[str]:
    urls, seen = [], set()
    for m in _CAREERS_HREF.finditer(html or ""):
        urls.append(m.group(1))
    urls += _CAREERS_URLISH.findall(html or "")
    urls += ["/careers", "/jobs", "/careers/", "/company/careers", "/about/careers"]
    out = []
    for href in urls:
        try:
            u = urllib.parse.urljoin(base, href.strip())
        except Exception:
            continue
        p = urllib.parse.urlparse(u)
        if p.scheme not in ("http", "https") or not p.netloc:
            continue
        if any(p.netloc.endswith(h) for h in SKIP_HOSTS):
            continue
        u = u.split("#")[0]
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


# ---------------------------------------------------------------------------
# The scout
# ---------------------------------------------------------------------------
def scout(domain: str, polite: Polite) -> dict:
    """Resolve one company domain to an ATS board, or to job postings, or to nothing.

    Bounded to MAX_PAGES requests. Returns a record for the web_scout table.
    """
    host = (domain or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    r = {"domain": host, "ats": "", "token": "", "how": "", "jobs_found": 0,
         "status": "none", "note": ""}
    if not host or any(host.endswith(h) for h in SKIP_HOSTS):
        r["status"] = "skipped"
        r["note"] = "not a company careers host"
        return r

    pages = 0
    code, final, html = polite.get(f"{polite.origin(host)}/")
    pages += 1
    if code == -1:
        r["status"] = "robots"
        r["note"] = "robots.txt disallows"
        return r
    if code in (403, 429):
        r["status"] = "blocked"
        r["note"] = f"HTTP {code} — host set aside, not retried"
        return r
    if code != 200:
        r["status"] = "unreachable"
        r["note"] = f"HTTP {code}"
        return r

    # 1 · the ATS is very often linked or embedded right on the homepage
    ats, token = fingerprint(html)
    if ats:
        r.update(ats=ats, token=token, how="homepage", status="ats")
        return r

    # 2 · follow the careers link; the URL itself, the redirect, or the page may carry the token
    for url in _candidate_careers_urls(final, html):
        if pages >= MAX_PAGES:
            break
        ats, token = fingerprint(url)
        if ats:
            r.update(ats=ats, token=token, how="careers-link", status="ats")
            return r
        code, land, page = polite.get(url)
        pages += 1
        if code in (403, 429):
            r["status"] = "blocked"
            r["note"] = f"HTTP {code} on {url[:60]}"
            return r
        if code != 200 or not page:
            continue
        ats, token = fingerprint(land)                     # a redirect landed on the ATS
        if ats:
            r.update(ats=ats, token=token, how="redirect", status="ats")
            return r
        ats, token = fingerprint(page)
        if ats:
            r.update(ats=ats, token=token, how="careers-page", status="ats")
            return r
        # 3 · no ATS — is the page itself publishing structured job data?
        jobs = jsonld_jobs(page)
        if jobs:
            r.update(jobs_found=len(jobs), how="json-ld", status="jsonld",
                     note=(jobs[0].get("title") or "")[:80])
            return r

    # 4 · last look: the sitemap the site itself advertises
    sms = polite.sitemaps(host)
    if sms and pages < MAX_PAGES:
        code, _, xml = polite.get(sms[0], limit=1_000_000)
        if code == 200:
            locs = _LOC.findall(xml)
            jobish = [u for u in locs if _JOBISH.search(u)]
            if jobish:
                r.update(jobs_found=len(jobish), how="sitemap", status="sitemap",
                         note=jobish[0][:100])
                return r
    return r


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
