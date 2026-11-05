"""Publication dates with field provenance. Never substitute a discovery/update/creation date."""
from __future__ import annotations

import re
import json
from datetime import datetime, time, timezone, timedelta

# These are publication fields. The label preserves a feed's date and a republish date rather
# than claiming either is the employer's original posting date.
FIELDS = {
    "greenhouse.first_published": "published", "ashby.publishedAt": "last_published",
    "ashby.publishedDate": "last_published", "smartrecruiters.releasedDate": "published",
    "workable.published_on": "published", "recruitee.published_at": "published",
    "breezy.published_date": "published", "teamtailor.date_published": "published",
    "keka.publishedOn": "published", "JobPosting.datePosted": "published",
    "hasjob.atom.published": "feed_published", "fossunited.data-date": "feed_published",
    "infopark.posted_date": "published", "remote-network.isoDate": "feed_published",
    "remotive.publication_date": "feed_published", "himalayas.pubDate": "feed_published",
}
ORIGINAL_FIELDS = {"greenhouse.first_published", "JobPosting.datePosted"}


def instant(raw) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (ValueError, TypeError):
        return None


def publication(job: dict, now: datetime | None = None) -> dict | None:
    field = job.get("posted_field") or ""
    raw = job.get("posted_at")
    if job.get("posted_conflict") or field not in FIELDS or not instant(job.get("posted_checked_at")) or not isinstance(raw, str):
        return None
    raw = raw.strip()
    # Reject relative dates, epochs, locale-ambiguous numbers and partially parsed garbage.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?", raw):
        return None
    now = (now or datetime.now().astimezone()).astimezone(timezone.utc)
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.year < 2000:
        return None
    date = value.date()
    precise = value.tzinfo is not None
    stamp = value.timestamp() if precise else datetime.combine(date, time.min, timezone.utc).timestamp()
    # An undated timezone may already be on tomorrow's calendar (up to UTC+14).
    # Accept that civil date without pretending it is a known instant.
    if (precise and stamp > now.timestamp()) or (not precise and date > (now + timedelta(hours=14)).date()):
        return None
    return {"date": date.isoformat(), "ts": stamp, "precision": "instant" if precise else "day",
            "kind": FIELDS[field], "field": field, "checked_at": job["posted_checked_at"],
            "url": job.get("url") or "", "raw": raw, "original": field in ORIGINAL_FIELDS}


def age_days(posting: dict | None, now: datetime) -> float | None:
    if not posting:
        return None
    now = now.astimezone(timezone.utc)
    if posting["precision"] == "day":
        return float(max(0, (now.date() - datetime.fromisoformat(posting["date"]).date()).days))
    return max(0.0, (now.timestamp() - posting["ts"]) / 86400)


def recent_original(posting: dict | None, now: datetime, *, clock_ok: bool) -> bool:
    """Strict alert gate: original publication, exact instant, recently checked, sound clock."""
    if not clock_ok or not posting or not posting.get("original") or posting.get("precision") != "instant":
        return False
    checked = instant(posting.get("checked_at"))
    return bool(checked and 0 <= now.timestamp() - posting["ts"] <= 3600
                and 0 <= (now.astimezone(timezone.utc) - checked).total_seconds() <= 900)


def equivalent(left: str, right: str) -> bool:
    a, b = instant(left), instant(right)
    return a == b if a and b else left == right


def evidence(job: dict, history=()) -> dict:
    p = publication(job)
    try:
        comparison = json.loads(job.get("posted_comparison") or "null")
    except (ValueError, TypeError):
        comparison = None
    reason = job.get("posted_conflict") or ("" if p else "The source has no usable, verified publication date.")
    return {"status": "conflict" if job.get("posted_conflict") else "source_reported" if p else "unknown",
            "reason": reason, "raw": job.get("posted_at") or "", "field": job.get("posted_field") or "",
            "checked_at": job.get("posted_checked_at") or "", "url": job.get("url") or "",
            "posting": p, "history": list(history), "comparison": comparison}
