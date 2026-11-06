"""Deterministic relevance score for a posting vs YOU — 0-100 plus the reasons why. NO LLM.

The old scorer looked only at the job TITLE, and hard-coded someone else's idea of a good job.
That fails badly in both directions:

  * "Member of Technical Staff" — the standard engineer title at OpenAI, Anthropic and
    Mistral — scored 7 and was DISCARDED, while those are exactly the roles worth seeing.
  * It ignored the `profile` argument it was handed, so tuning your profile changed nothing.

This version reads `finder:` out of config/profile.yaml and scores against the DESCRIPTION as
well as the title. The key idea is a **fallback**: when a title carries no signal, we don't
give up — we look at what the job actually says it does. A posting that talks about shipping
code in TypeScript is a software job whatever HR chose to call it.

Everything is transparent and tunable: no embeddings, no model, just weights you can read.
"""
from __future__ import annotations

import functools
import re

from collector.sources import enrich, regions

# ---- scoring weights ---------------------------------------------------------------------
# Role and experience are now GATES (a job fails them or it doesn't), so they must not also hand
# out big points — doing that pushed every surviving job to 100 and killed the ranking signal
# entirely. The stack carries the ordering, because "has some part of my techstack" is the thing
# the user actually cares about, and *how much* of the job's stack you know is the real gradient.
W_ROLE_TITLE = 18        # a role you named, vs merely a software role
W_ROLE_DESC = 8          # title said nothing; the description proved it's hands-on engineering
W_SKILL_CAP = 45         # ← the dominant signal: how much of the JOB's stack you already know
W_LOC = 20               # remote / a city you'd work in
W_SENIOR_FIT = 5         # states a year count, and it's within your ceiling
P_SENIOR_GAP = -3        # (kept for reference; experience is now a gate, not a penalty)
P_NO_SPONSOR = -12       # explicitly refuses sponsorship — a dead end from India
P_INTERN = -55

# Signals that the PERSON writes code — deliberately NOT "software" or "engineering team",
# because every job at Stripe says those. Proving the company does engineering is not proving
# the role does; that mistake once scored an "Operations Associate, GTM" at 64.
_ENG_SIGNALS = re.compile(
    r"(?i)(?:\bwrite\s+(?:code|software)\b|\bcodebase\b|\bsource\s+code\b|\bproduction\s+code\b|"
    r"\bcode\s+review\b|\bship\s+(?:code|features)\b|\bdebug(?:ging)?\b|\brefactor\w*\b|"
    r"\bwriting\s+(?:clean|maintainable|production)\b|\bdesign\s+and\s+(?:build|implement)\b|"
    r"\bbuild\s+and\s+maintain\b|\bmicro-?services\b|\bdistributed\s+systems\b|"
    r"\bfull[\s-]?stack\b|\bback-?end\b|\bfront-?end\b|\bAPIs?\s+(?:design|development)\b|"
    r"\bsoftware\s+(?:development|engineering)\s+(?:experience|background)\b|\bSDLC\b)")
_ENG_TITLE = re.compile(r"(?i)\b(?:engineer|engineering|developer|programmer|architect|sde|swe)\b")
_INTERN = re.compile(r"(?i)\b(?:intern|internship|co-?op|apprentice)\b")
# "Member of Technical Staff" contains the word "staff" but is NOT a Staff-level role — it's the
# ordinary IC engineer title at OpenAI, Anthropic and Mistral, held from new-grad upward. Matching
# it as Staff (≈8 years) rejected exactly the roles we went to some trouble to rescue.
_MTS = re.compile(r"(?i)\b(?:member\s+of\s+)?technical\s+staff\b")
_TITLE_STAFF = re.compile(r"(?i)\b(?:staff|principal|distinguished|fellow|architect)\b")
_TITLE_SENIOR = re.compile(r"(?i)\b(?:senior|sr\.?|lead)\b")
_TITLE_JUNIOR = re.compile(r"(?i)\b(?:junior|jr\.?|entry[\s-]level|new\s?grad|graduate|associate)\b")

# ── SOFTWARE ONLY ────────────────────────────────────────────────────────────────────────
# "Engineer" is not the same as "software engineer". With SpaceX, Rivian, Lucid, Anduril and
# Oklo on the board list, a bare \bengineer\b match drags in propulsion, avionics and
# structural engineers by the hundred — SpaceX alone contributed 455 non-software roles.
#
# So software is an ALLOW-list (a positive requirement), and these two deny-lists take
# precedence over everything, including a promising description.

# Physical / hardware disciplines — a different profession that shares the word "engineer".
_NOT_SOFTWARE = re.compile("(?i)\\b(?:" + "|".join([
    "mechanical", "electrical", "hardware", "civil", "chemical", "industrial", "manufacturing",
    "aerospace", "propulsion", "structural", "thermal", "materials", "avionics", "optical",
    "antenna", "photonics", "mechatronics", "controls", "biomedical", "chemist", "physicist",
    "technician", "machinist", "welder", "fabrication", "facilities", "hvac", "electronics",
    "power", "battery", "vehicle", "flight", "propellant", "launch", "spacecraft",
    r"(?:cnc|cam|cmm|plc|mastercam)[\s/-]*(?:programmer|programming|operator)",
    r"(?:programmer|programming)[\s/-]+(?:cnc|cam|cmm|plc|mastercam)",
]) + r")\b")

# A publisher may spell the software tool as two words. Normalize only the
# classification input; the original title and physical power exclusions stay intact.
_SOFTWARE_TOOL_SPELLING = re.compile(r"\bpower\s+shell\b", re.I)

# Customer-facing "engineers" — the job is the customer, not the codebase.
_CUSTOMER_FACING = re.compile("(?i)(?:" + "|".join([
    r"\bbusiness\s+developer\b",
    r"\bsolutions?\s+(?:engineer|architect|consultant)", r"\bsales\s+engineer",
    r"\bsales\s+development\s+(?:executive|representative|associate)\b",
    r"\bsupport\s+engineer", r"\btechnical\s+support", r"\bcustomer\s+(?:success\s+)?engineer",
    r"\bimplementation\s+engineer", r"\bdelivery\s+(?:engineer|architect)",
    r"\bfield\s+(?:engineer|application)", r"\bpre-?sales", r"\bprofessional\s+services",
    r"\bscientist\b",                       # data/research scientist ≠ software engineer
    # DevRel is advocacy, not engineering — but the word "Developer" in the title sails
    # straight through the software allow-list, so it needs naming explicitly.
    r"\bdeveloper\s+(?:relations|advocate|evangelist|experience)", r"\bdevrel\b",
    r"\bcommunity\s+engineer",
    # AI data-labelling gigs ("Python Coding Specialist — Freelance AI Trainer") are piecework,
    # not software engineering roles.
    r"\bai\s+trainer\b", r"\bdata\s+annotat\w+", r"\b(?:coding|code)\s+specialist\b",
]) + ")")

# The positive definition: titles that ARE software engineering.
_SOFTWARE = re.compile("(?i)(?:" + "|".join([
    r"\bsoftware\s+(?:engineer|developer|architect)", r"\bsoftware\s+development\s+engineer",
    r"\bsoftware\s+design\s+engineer\b",
    r"\bswe\b", r"\bsde\s*[123i]*\b",
    r"\b(?:back|front)[\s-]?end\b", r"\bfull[\s-]?stack\b", r"\bweb\s+(?:developer|engineer)",
    r"\bmobile\s+(?:engineer|developer)", r"\b(?:ios|android)\s+(?:engineer|developer)",
    r"\bplatform\s+engineer", r"\binfrastructure\s+engineer", r"\bdevops\b", r"\bsre\b",
    r"\bcloud\s+engineer",                  # the same family as platform / infrastructure
    r"\bsite\s+reliability", r"\bdata\s+engineer", r"\bmachine\s+learning\s+engineer",
    r"\bml\s+engineer", r"\bai\s+engineer", r"\bsecurity\s+engineer", r"\bsdet\d*\b",
    r"\bqa\s+(?:engineer|automation)", r"\btest\s+engineer", r"\bautomation\s+engineer",
    r"\b(?:embedded|firmware)\s+(?:engineer|developer)", r"\bsystems?\s+engineer",
    r"\bapplication\s+(?:engineer|developer)", r"\bprogrammer\b", r"\bdeveloper\b",
    r"\bmember\s+of\s+technical\s+staff\b", r"\btechnical\s+staff\b",
    # no trailing \b: "Forward Deployed Engineering" is the same team, titled by its name
    r"\bresearch\s+engineer\b", r"\bforward\s+deployed\s+engineer",
]) + ")")

# Titles that are NOT an individual-contributor engineering job, however much the description
# talks about engineering. These are people-management / program / ops / GTM roles.
# Two tiers, because these words don't all mean the same thing:
#
# MGMT — you're managing people, not writing code. Rejected even when "Engineer" is in the
# title ("Engineering Manager", "Manager, Forward Deployed Engineering").
_MGMT = re.compile("(?i)(?:" + "|".join([
    r"\bmanager\b", r"\bdirector\b", r"\bvp\b", r"\bvice\s+president\b", r"\bhead\s+of\b",
    r"\bchief\b", r"\bpresident\b", r"\bTPM\b",
]) + ")")
# NON-ENG — words common to business roles, but which also appear in perfectly good IC titles
# ("Platform Engineer, Operations"). So these only disqualify a title that has NO engineering
# noun in it at all — otherwise we'd bin real jobs for a stray word.
_NON_ENG = re.compile("(?i)(?:" + "|".join([
    r"\boperations?\b", r"\bstrategy\b", r"\banalyst\b", r"\bconsultant\b", r"\bGTM\b",
    r"\bevangelist\b", r"\badvocate\b", r"\bpartnerships?\b", r"\brecruit\w*\b",
    r"\b(?:business|customer|community|people|talent|account)\s+\w+",
]) + ")")

_DEFAULT = {
    "years_experience": 5,
    "target_roles": ["full stack", "software engineer", "backend", "frontend", "web developer"],
    "role_aliases": ["member of technical staff", "technical staff", "sde",
                     "software development engineer", "product engineer"],
    "skills": ["JavaScript", "TypeScript", "React", "Node.js", "Python", "AWS", "SQL"],
    "locations": ["remote", "india"],
    "exclude": ["sales", "recruiter", "marketing", "counsel", "attorney", "accountant"],
    "min_score": 30,
}


def _cfg(profile):
    f = dict(_DEFAULT)
    f.update(((profile or {}).get("finder") or {}))
    return f


def fingerprint(profile) -> str:
    """What every score depends on: these rules (this file) and your finder settings. A job scored
    under another fingerprint is out of date; the rescore chore brings it back in line."""
    import hashlib
    import json
    with open(__file__, "rb") as fh:
        src = fh.read()
    rules = json.dumps(_cfg(profile), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(src + rules).hexdigest()[:16]


@functools.lru_cache(maxsize=None)
def _word(phrase: str) -> re.Pattern:
    """`phrase` as whole words, in lower-cased text. A plural "s" is allowed ("Web Developers").

    Every phrase here used to be matched as a bare substring, which read "Java" inside
    "JavaScript", "SQL" inside "PostgreSQL" and "sales" inside "Salesforce". On the live list that
    credited 12,958 jobs with Java and 8,761 with SQL for a tag they do not carry.
    """
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"s?(?![a-z0-9])")


def _in_title(phrases, title: str):
    """The first of `phrases` found in the title as whole words, else None."""
    return next((p for p in phrases if _word(p).search(title)), None)


# A skill that stands for a family: knowing SQL means a MySQL or PostgreSQL job uses it.
_COVERS = {"sql": {"mysql", "postgresql"}}


def matched_skills(skills, tech: str, title: str) -> list[str]:
    """Your skills this job uses: those among its stack tags (whole tags, so "Java" is not
    "JavaScript"), those its title names (tagged the way the description was, so "ReactJS" is
    React, or as whole words), and a family skill one of its tags belongs to (SQL for MySQL)."""
    tags = {t.strip().lower() for t in (tech or "").split(",")} - {""}
    tags |= {t.lower() for t in enrich.tech(title or "")}
    low = (title or "").lower()
    return [sk for sk in skills
            if sk.lower() in tags or _word(sk).search(low) or tags & _COVERS.get(sk.lower(), set())]


# Where a remote job can be held from India: no region named, India itself, or APAC (which
# usually includes India). "Remote - United States", "Mexico, Remote" and "Remote (EMEA)" are
# remote for someone else: 2,360 of the 11,964 strong matches were those (2026-10-27).
_REMOTE_FROM_INDIA = {"India", "APAC", "Remote / Global", "Other"}


# A remote job whose DESCRIPTION rules out someone in India: only statements about who may apply.
# A time-zone overlap ("overlap with EST") is not one, and a company calling itself US-based is
# not one either. 195 of the 1,860 strong remote matches said one of these (2026-10-27).
_ELSEWHERE = r"(?:u\.?s\.?a?|united states|canada|uk|united kingdom|eu|european union|europe|emea|latam|latin america)"
_REMOTE_EXCLUDES = re.compile(
    r"(?i)authori[sz]ed to work in (?:the )?" + _ELSEWHERE + r"\b"
    r"|(?:must|should|need to|required to) (?:be )?(?:reside|residing|live|living|located|based)"
    r" (?:in|within) (?:the )?(?:contiguous )?" + _ELSEWHERE + r"\b"
    r"|\bremote (?:in|within|across) (?:the )?(?:contiguous )?" + _ELSEWHERE + r"\b"
    r"|\bu\.?s\.? (?:citizens?|persons?)\b|\bsecurity clearance\b")
# …unless it also says you can be in India, or anywhere: then it does not rule India out.
_REMOTE_INCLUDES = re.compile(r"(?i)\bindia\b|\banywhere\b|\bworldwide\b|\bglobal(?:ly)? remote\b"
                              r"|\bany (?:country|location)\b|\bapac\b")


def _remote_excludes_india(desc: str) -> bool:
    return bool(_REMOTE_EXCLUDES.search(desc or "")) and not _REMOTE_INCLUDES.search(desc or "")


def _wanted_place(loc: str, wanted, desc: str = "") -> bool:
    """Is the job somewhere you said you can work? "india" means anywhere regions.py knows as
    India, so a Bengaluru posting counts and an Indiana one does not. "remote" means remote and
    not ruling out India: neither its location nor its description ties it to another country."""
    for place in wanted:
        p = place.lower()
        if p == "india":
            if regions.is_india(loc):
                return True
        elif p == "remote":
            region = regions.first(loc)
            if (_word("remote").search(loc) and region in _REMOTE_FROM_INDIA
                    # APAC as a region usually includes India; one APAC country ("Remote,
                    # Australia") does not
                    and (region != "APAC" or re.search(r"(?i)\bapac\b|\basia\b", loc))
                    and not _remote_excludes_india(desc)):
                return True
        elif _word(place).search(loc):
            return True
    return False


def is_software_title(title: str) -> bool:
    """Is THIS posting a software-engineering role, judged from the title alone?

    Used to answer "does this company hire software engineers at all" across thousands of
    boards, where fetching every description is not an option. Deliberately title-only and
    conservative: a company qualifies on evidence, not on having a careers page. Without this,
    a harvest of "every company with an ATS board" is 40% healthcare and staffing agencies —
    BAYADA Home Health Care has 2,583 open roles and not one of them is an engineering job.
    """
    t = (title or "").lower()
    if not t:
        return False
    if (_NOT_SOFTWARE.search(_SOFTWARE_TOOL_SPELLING.sub("powershell", t))
            or _CUSTOMER_FACING.search(t) or _MGMT.search(t)):
        return False
    if _NON_ENG.search(t) and not _ENG_TITLE.search(t):
        return False
    return bool(_SOFTWARE.search(t))


def title_plausible(title: str, profile: dict | None = None) -> bool:
    """Could this title POSSIBLY be a job for you, judging by the title alone?

    Used by the poller to decide whether a board that hides its descriptions behind a
    per-posting call is worth paying for. It has to be title-only by definition: we can't read
    the description to decide whether to fetch the description. Deliberately generous — it only
    rules out the things a title alone can settle (sales, management), and lets everything else
    through to be judged properly on its text.
    """
    f = _cfg(profile)
    t = (title or "").lower()
    if _in_title(f["exclude"], t):
        return False
    if _MGMT.search(t):
        return False
    if _NON_ENG.search(t) and not _ENG_TITLE.search(t):
        return False
    return True


def score(job: dict, profile: dict | None = None) -> tuple[int, list[str]]:
    """→ (0-100, reasons). A score of 0 means EXCLUDED.

    THE WHOLE DEFINITION, in the user's words: "just software engineer jobs with experience up to
    5 years and have some part of my techstack". Three rules, and nothing else:

        1. it must be a SOFTWARE ENGINEERING role
        2. it must ask for NO MORE than max_job_yoe years   (unstated = fine, ads often omit it)
        3. it must use AT LEAST ONE thing from your stack

    Anything failing one of those is out. Everything surviving is IN, and the score only decides
    the ORDER — it never hides a job. Location, salary, sponsorship and seniority are ranking
    nudges, not gates: they move a job up or down, they don't delete it.
    """
    f = _cfg(profile)
    title = (job.get("title") or "").lower()
    loc = (job.get("location") or "").lower()
    desc = job.get("description") or ""
    # `tech` is the stack we already parsed out of the description in enrich.py — reuse it
    # rather than re-scanning prose for the same keywords.
    tech = job.get("tech") or ""
    tech_l = tech.lower() if isinstance(tech, str) else ", ".join(tech).lower()

    reasons: list[str] = []

    # ---- hard reject: a job you'd never take, however well it scores otherwise
    bad = _in_title(f["exclude"], title)
    if bad:
        return 0, [f"excluded: {bad}"]
    # …and management roles, which are not IC engineering however much the description talks
    # about code.
    if _MGMT.search(title):
        return 0, ["management role, not IC engineering"]
    # …and business roles — but ONLY when the title carries no engineering noun, so that a
    # legitimate "Platform Engineer, Operations" survives the word "Operations".
    if _NON_ENG.search(title) and not _ENG_TITLE.search(title):
        return 0, ["not an engineering role"]
    # …and engineers of a different profession entirely (mechanical, propulsion, avionics…),
    # and "engineers" whose job is the customer (Solutions/Sales/Support). Both take
    # precedence over the description — a propulsion engineer's posting still says "design
    # and build".
    if _NOT_SOFTWARE.search(_SOFTWARE_TOOL_SPELLING.sub("powershell", title)):
        return 0, ["not software (hardware/physical engineering)"]
    if _CUSTOMER_FACING.search(title):
        return 0, ["customer-facing, not software engineering"]

    s = 0
    matched = matched_skills(f["skills"], tech_l, job.get("title") or "")

    # ---- role fit: SOFTWARE is a positive requirement, not a bonus
    hit = _in_title(f["target_roles"], title)
    alias = _in_title(f["role_aliases"], title)
    if hit:
        s += W_ROLE_TITLE
        reasons.append(f"title '{hit}' +{W_ROLE_TITLE}")
    elif alias:
        s += W_ROLE_TITLE
        reasons.append(f"alias '{alias}' +{W_ROLE_TITLE}")
    elif _SOFTWARE.search(title):
        s += W_ROLE_TITLE - 8
        reasons.append(f"software title +{W_ROLE_TITLE - 8}")
    else:
        # THE RESCUE PATH — for titles that say nothing at all ("Member of Technical Staff").
        # Demand BOTH: the role describes writing code, AND it names a real stack. Either alone
        # is too weak; together they're hard to fake. A bare \bengineer\b is NOT enough — that's
        # how propulsion engineers got in.
        sig = len(set(m.group(0).lower() for m in _ENG_SIGNALS.finditer(desc)))
        if sig >= 4 and len(matched) >= 2:
            s += W_ROLE_DESC
            reasons.append(f"description reads as hands-on software "
                           f"({sig} signals, {len(matched)} stack) +{W_ROLE_DESC}")
        else:
            return 0, ["not a software engineering role"]

    # ═══ RULE 3 — must use at least SOME of your stack ══════════════════════════════════
    if not matched:
        return 0, ["no overlap with your tech stack"]

    # Scored as a SHARE of the job's stack, not a raw count. A flat "+4 per skill, capped at 24"
    # saturates the moment the profile lists many skills — with 36 skills, six matches maxes it
    # out, nearly every job hits the ceiling and the signal goes dead (it dropped nDCG to 0.26).
    # What matters is how much of the JOB is stuff you know: a role wanting exactly
    # Python+Django+React fits better than one wanting 15 technologies of which you have 6.
    job_stack = [t for t in (tech_l.split(", ") if tech_l else []) if t]
    coverage = len(matched) / max(len(job_stack), len(matched))        # 0..1
    pts = int(W_SKILL_CAP * coverage) + min(len(matched), 3)           # depth + a little breadth
    s += pts
    reasons.append(f"stack {'/'.join(matched[:4])} "
                   f"({len(matched)}/{len(job_stack) or len(matched)} of the job's) +{pts}")

    # ---- location
    if _wanted_place(loc, f["locations"], desc):
        s += W_LOC
        reasons.append(f"location +{W_LOC}")

    # ═══ RULE 2 — asks no more than max_job_yoe years ═══════════════════════════════════
    # Checked two ways, because only ~half of postings state a number: the stated requirement,
    # and the level implied by the title (which is always there). Either one over the ceiling
    # rules the job out.
    ceiling = float(f.get("max_job_yoe") or 5)
    want = int(job.get("yoe") or 0)
    if want and want > ceiling:
        return 0, [f"asks {want}y — over your {ceiling:g}y ceiling"]

    mts = bool(_MTS.search(title))                          # "Member of Technical Staff" ≠ Staff
    lvl_years = (0 if mts else
                 8 if _TITLE_STAFF.search(title) else       # Staff / Principal / Architect
                 5 if _TITLE_SENIOR.search(title) else      # Senior / Lead
                 1 if _TITLE_JUNIOR.search(title) else 0)   # Junior / Associate / Entry / Grad
    if lvl_years > ceiling:
        return 0, [f"title implies ~{lvl_years}y — over your {ceiling:g}y ceiling"]
    if _INTERN.search(title):
        return 0, ["internship"]

    # ── the three rules are satisfied. Everything below only decides ORDER. ─────────────
    if want:
        s += W_SENIOR_FIT
        reasons.append(f"asks {want}y ≤ {ceiling:g}y +{W_SENIOR_FIT}")
    if lvl_years == 1:
        s += 8
        reasons.append("entry-level +8")

    # A posting that explicitly refuses sponsorship is a dead end from India — push it DOWN,
    # but never delete it (it may be reachable from an Indian office).
    if job.get("sponsor") == "no" and not regions.is_india(loc):
        s += P_NO_SPONSOR
        reasons.append(f"no sponsorship {P_NO_SPONSOR}")

    return max(1, min(100, s)), reasons


def why(job: dict, profile: dict | None = None) -> list[str]:
    """score()'s reasons in plain words, for the job panel. Hundreds of jobs show 99 and a 74 can
    sit above them, and a bare number never said what it was made of."""
    pts, reasons = score(job, profile)
    if not pts:
        return reasons
    out = []
    for r in reasons:
        m = re.match(r"(?:title|alias) '.*' \+(\d+)$", r)
        if m:
            out.append(f"the title is a role you want (+{m[1]})")
        elif m := re.match(r"software title \+(\d+)$", r):
            out.append(f"a software title (+{m[1]})")
        elif m := re.match(r"description reads as hands-on software .* \+(\d+)$", r):
            out.append(f"the description is hands-on coding (+{m[1]})")
        elif m := re.match(r"stack .* \((\d+)/(\d+) of the job's\) \+(\d+)$", r):
            names = matched_skills(_cfg(profile)["skills"], (job.get("tech") or "").lower(),
                                   job.get("title") or "")
            # One listed technology can match several profile skills (PostgreSQL also
            # matches SQL). Those are profile matches, not a fraction of job tags.
            out.append(f"matches your skills: {', '.join(names)} (+{m[3]})")
        elif m := re.match(r"location \+(\d+)$", r):
            out.append(f"a place you can work (+{m[1]})")
        elif m := re.match(r"asks (\d+)y ≤ (\S+)y \+(\d+)$", r):
            out.append(f"asks {m[1]} year{'' if m[1] == '1' else 's'}, within your {m[2]} (+{m[3]})")
        elif m := re.match(r"entry-level \+(\d+)$", r):
            out.append(f"an entry-level title (+{m[1]})")
        elif m := re.match(r"no sponsorship (-\d+)$", r):
            out.append(f"says no visa sponsorship ({m[1]})")
        else:
            out.append(r)
    if not any(r.startswith("location") for r in reasons):
        out.append(f"not a place you can work (+0 of {W_LOC})")
    return out
