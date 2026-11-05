"""Bounded canonical-page comparisons for recent publication claims; respect robots rules."""
import json
import time
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from collector.sources import posting_dates, webscout

_POLITE = webscout.Polite()
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_CACHE_LIMIT = 2048
_CACHE_SECONDS = 900


def _attach(job, result):
    job["posted_comparison"] = json.dumps(result)
    if result["status"] == "conflict":
        job["posted_conflict"] = "The source API and the job page report different publication dates."


def check(jobs, limit=3):
    now = datetime.now(timezone.utc)
    remaining = limit
    for job in jobs:
        job.setdefault("posted_checked_at", now.isoformat())
        p = posting_dates.publication(job, now)
        if not p or p["kind"] != "published" or posting_dates.age_days(p, now) > 1:
            continue
        if job.get("ats") == "site":
            continue  # The structured data was read from this page already.
        key = (job["url"], job.get("title", ""), p["field"], p["raw"])
        with _LOCK:
            cached = _CACHE.get(key)
            if cached:
                _CACHE.move_to_end(key)
        if cached and time.monotonic()-cached[0] < _CACHE_SECONDS:
            # Only the secondary page comparison is reused; retain its real checked time.
            # The source API is still fetched and timestamped on every due board check.
            _attach(job, cached[1])
            continue
        if remaining <= 0:
            continue
        remaining -= 1
        if not cached:
            # A changed API date needs the page body; a validator from a different claim
            # must not turn that comparison into an unusable 304 response.
            with _POLITE._lock:
                _POLITE._cache.pop(job["url"], None)
        status, final, html = _POLITE.get(job["url"], limit=1_500_000)
        result = {"url": final or job["url"], "checked_at": datetime.now(timezone.utc).isoformat(),
                  "status": "unavailable", "raw": ""}
        if status == 200:
            matches = [j for j in webscout.jsonld_jobs(html)
                       if str(j.get("title", "")).strip().casefold() == job.get("title", "").strip().casefold()]
            if len(matches) == 1:
                raw = matches[0].get("datePosted") or ""
                other = posting_dates.publication({"url": result["url"], "posted_at": raw,
                    "posted_field": "JobPosting.datePosted", "posted_checked_at": result["checked_at"]}, now)
                result.update(raw=raw, status="no_usable_date")
                if other:
                    same = (posting_dates.equivalent(p["raw"], other["raw"]) if p["precision"] == other["precision"] == "instant"
                            else p["date"] == other["date"])
                    result["status"] = "agrees" if same else "conflict"
        if status == 304 and cached:
            # Conditional validation confirms the saved page evidence is unchanged.
            result = {**cached[1], "checked_at": result["checked_at"]}
        _attach(job, result)
        with _LOCK:
            _CACHE[key] = (time.monotonic(), result)
            _CACHE.move_to_end(key)
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.popitem(last=False)
        with _POLITE._lock:
            while len(_POLITE._cache) > _CACHE_LIMIT:
                _POLITE._cache.pop(next(iter(_POLITE._cache)))
    return jobs
