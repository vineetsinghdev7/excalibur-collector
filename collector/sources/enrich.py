"""Turn a raw job description into STRUCTURED, filterable facts — deterministically.

This is what makes the finder feel like hiring.cafe: you can filter by salary, years of
experience, visa sponsorship, tech stack and education, because we parse those out of the
posting text instead of guessing from the title.

No LLM. Every extractor is word-boundary regex over the description, and every one of them
is allowed to return "unknown" (`None` / `""` / `[]`). That is the important design rule:
a *missing* fact is always safe (the job simply doesn't get that filter), whereas a
*wrong* fact silently hides jobs you should have seen. So all extractors are conservative
and bail out rather than guess.
"""
from __future__ import annotations

import html as _html
import re

# ---------------------------------------------------------------- description cleanup

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def to_text(raw: str) -> str:
    """HTML (Greenhouse serves escaped HTML) → readable plain text, newlines preserved."""
    if not raw:
        return ""
    s = _html.unescape(raw)
    s = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", s)
    s = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", s)
    s = _TAG.sub(" ", s)
    s = _html.unescape(s)
    s = _WS.sub(" ", s)
    s = _NL.sub("\n\n", s)
    return "\n".join(line.strip() for line in s.split("\n")).strip()


# ---------------------------------------------------------------- salary

# 120,000 | 120000 | 120.5 | 15,00,000 (Indian lakh grouping)
# Must END on a digit. Written as `\d[...]*` it was greedy over trailing punctuation and swallowed
# the sentence break: in "$150,000 - $180,000. Plus a $25,000 fertility reimbursement" the second
# number matched "180,000. ", putting m.end() inside the NEXT sentence — so every position-based
# check (the perk window, the trailing currency code, the hourly/monthly window) read the wrong
# text. The value still came out right only because _to_num scrubbed it afterwards.
_NUM = r"\d(?:[\d,. \s]*\d)?"
_CUR = {"$": "USD", "us$": "USD", "usd": "USD", "£": "GBP", "gbp": "GBP", "€": "EUR", "eur": "EUR",
        "₹": "INR", "inr": "INR", "rs": "INR", "rs.": "INR", "c$": "CAD", "cad": "CAD",
        "a$": "AUD", "aud": "AUD", "chf": "CHF", "sgd": "SGD", "s$": "SGD", "¥": "JPY", "zł": "PLN"}
_CUR_RX = r"(?:\$|£|€|₹|¥|US\$|C\$|A\$|S\$|USD|GBP|EUR|INR|CAD|AUD|CHF|SGD|PLN|Rs\.?)"
_HOURLY = re.compile(r"(?i)\b(?:per\s*hour|hourly|/\s*h(?:r|our)\b|an\s+hour)\b")
_MONTHLY = re.compile(r"(?i)\b(?:per\s*month|monthly|/\s*mo(?:nth)?\b|a\s+month)\b")
_HOURS_PER_YEAR = 2080
# a range: <cur> <num><k?> [–|-|to] <cur?> <num><k?>
_RANGE = re.compile(
    rf"({_CUR_RX})\s*({_NUM})\s*([KkMmLl]|lakhs?|LPA|lpa)?\s*(?:[-–—]|\bto\b)\s*"
    rf"(?:{_CUR_RX})?\s*({_NUM})\s*([KkMmLl]|lakhs?|LPA|lpa)?")
_SINGLE = re.compile(rf"({_CUR_RX})\s*({_NUM})\s*([KkMmLl]|lakhs?|LPA|lpa)?")


def _to_num(raw: str, suffix: str | None) -> float | None:
    s = re.sub(r"[,\s ]", "", raw or "").rstrip(".")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    sfx = (suffix or "").lower()
    if sfx == "k":
        v *= 1_000
    elif sfx == "m":
        v *= 1_000_000
    elif sfx in ("l", "lakh", "lakhs", "lpa"):
        v *= 100_000
    return v


# A job posting is FULL of money that is not your salary: funding raised, transaction volume,
# 401(k) matches, tuition and fertility reimbursements. Grabbing the first currency-looking
# number is therefore wrong — it once read Checkr's "$25K fertility reimbursement" as the pay,
# and PhonePe's "INR 150 lakh crore" of transaction volume as a ₹1.5cr salary. So a number
# only counts as compensation if it's SPEAKING about compensation:
_COMP_CTX = re.compile(
    # NB "compensation(?:\s+…)?" — the suffix group INCLUDING its whitespace is optional. It used
    # to be `compensation\s+(?:range|package|band)?`, where the \s+ was mandatory, so the single
    # most common header in a US posting — "Compensation:" with a colon, which pay-transparency
    # laws all but mandate — never matched. 62 postings in the store stated their pay that way
    # and had it read as no pay at all.
    r"(?i)(?:base\s+(?:salary|pay)|salary\s+range|compensation(?:\s+(?:range|package|band))?"
    r"|pay\s+(?:range|band|scale|rate)|hourly\s+rate|\bcomp\s*:"
    r"|salary|remuneration|total\s+(?:cash|compensation)|on[\s-]target\s+earnings|\bOTE\b|\bCTC\b"
    r"|per\s+annum|annual\s+base|expected\s+pay|pay\s+transparency|starting\s+(?:salary|pay))")
# …and NOT speaking about a benefit, a perk, or the company's own scale:
_NOT_COMP = re.compile(
    r"(?i)(?:reimburse\w*|401\s*\(?k\)?|tuition|fertility|adoption|wellness|stipend\s+for|discount"
    r"|donation|match(?:es|ing)?\s+up\s+to|insurance|coverage|budget|allowance|credit"
    r"|raised|funding|valuation|revenue|\bARR\b|\bTPV\b|transaction|crore|market\s+cap|assets\s+under)")
_CUR_CODE = re.compile(r"(?i)\b(USD|GBP|EUR|INR|CAD|AUD|CHF|SGD|PLN|JPY)\b")


def _try_match(m, src, is_range: bool) -> dict | None:
    if is_range:
        cur_tok, a, sa, b, sb = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        lo, hi = _to_num(a, sa or sb), _to_num(b, sb or sa)
    else:
        cur_tok, a, sa = m.group(1), m.group(2), m.group(3)
        lo = hi = _to_num(a, sa)
    if lo is None:
        return None

    before = src[max(0, m.start() - 150): m.start()]
    after_full = src[m.end(): m.end() + 60]
    # The perk test looks only as far as the end of THIS clause. "$25,000 fertility reimbursement"
    # must still be rejected — the perk word is part of the same phrase — but
    # "…$180,000. Plus a $25,000 fertility reimbursement" must not lose the salary just because a
    # benefit is described in the next sentence. `after_full` is kept unclipped for the currency
    # code, which legitimately trails the number ("$191,100 — $191,100 CAD").
    # …and still only 45 characters of it. Widening this to 60 while clause-scoping cost two real
    # salaries that trail a long benefits list ("a base salary of $200,000 USD plus competitive
    # equity compensation, 401k with employer…"): the extra 15 characters reached the 401k.
    after = re.split(r"(?:\.\s|\.$|[;\n•|])", after_full[:45])[0]
    cues = list(_COMP_CTX.finditer(before))
    if not cues:                                        # not talking about pay at all
        return None
    # WHICH text can disqualify this number. Only what sits BETWEEN the compensation cue and the
    # number itself — the phrase that actually attaches the number to the cue — plus what
    # immediately follows it.
    #
    # This used to scan a flat 80 characters backwards, which meant any benefits sentence near the
    # pay line deleted a real salary: "Health insurance included. Salary range: $150,000-$180,000"
    # scored nothing, and so did the same line preceded by 401k, budget, allowance, discount,
    # coverage or credits. Postings list perks next to pay almost by convention, so this fired
    # constantly — it accounted for a large share of the 653 stored jobs whose visible salary
    # range parsed to zero.
    #
    # The guard's real job is unchanged and still works: "$25K fertility reimbursement" is caught
    # by the `after` test, and "401k match up to $12,000" never had a compensation cue to begin
    # with. What it no longer does is punish a salary for the company of the sentence before it.
    tail = before[cues[-1].end():]
    if _NOT_COMP.search(tail) or _NOT_COMP.search(after):
        return None                                    # a perk or a company-scale figure

    # "$191,100 — $191,100 CAD": the trailing CODE is authoritative, the bare $ is not
    code = _CUR_CODE.search(after_full)
    cur = code.group(1).upper() if code else _CUR.get(cur_tok.lower().rstrip("."), "USD")

    window = before[-60:] + src[m.start(): m.end() + 60]
    period = "year"
    if _HOURLY.search(window):
        period, lo, hi = "hour", lo * _HOURS_PER_YEAR, (hi or lo) * _HOURS_PER_YEAR
    elif _MONTHLY.search(window):
        period, lo, hi = "month", lo * 12, (hi or lo) * 12
    if hi is None or hi < lo:
        hi = lo

    floor, ceil = (100_000, 20_000_000) if cur == "INR" else (10_000, 2_000_000)
    if not (floor <= lo <= ceil and floor <= hi <= ceil):
        return None
    return {"min": int(lo), "max": int(hi), "cur": cur, "period": period}


def salary(text: str, structured: str = "") -> dict:
    """→ {min, max, cur, period} in ANNUAL terms, or {} when we can't tell confidently.

    `structured` is an ATS-provided summary (Ashby's "$151K – $231K") — trusted outright,
    since it's the company's own comp field rather than prose we scraped out of a paragraph.
    Everything else must survive the compensation-context test above. Ranges are tried before
    single figures, because a range is nearly always a real pay band.
    """
    if structured:
        # The ATS's own comp field IS the compensation context, so satisfy the gate explicitly
        # rather than hoping the summary string happens to contain the word "salary".
        s = "base salary range " + structured
        for rx, is_range in ((_RANGE, True), (_SINGLE, False)):
            m = rx.search(s)
            if m:
                r = _try_match(m, s, is_range)
                if r:
                    return r
    src = (text or "")[:9000]
    if not src:
        return {}
    for rx, is_range in ((_RANGE, True), (_SINGLE, False)):
        for m in rx.finditer(src):
            r = _try_match(m, src, is_range)
            if r:
                return r
    return {}


# ---------------------------------------------------------------- years of experience

_YOE = re.compile(
    r"(?i)(\d{1,2})\s*(?:\+|plus)?\s*(?:[-–—]|\bto\b)?\s*(\d{1,2})?\s*\+?\s*"
    r"(?:years?|yrs?)(?:\s+of)?(?:\s+(?:relevant|professional|industry|hands[\s-]on|practical|combined))*"
    r"\s+(?:experience|exp\b|software|engineering|development|work)")
_BLANKS = re.compile(r"\s+")
_YOE_ALT = re.compile(r"(?i)(?:minimum|at\s+least|min\.?)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)")
# "N years of <the actual thing>". _YOE above only counts a year figure when the words
# experience/exp/software/engineering/development/work follow it, so every posting that asks for
# "7+ years of distributed systems", "5 years building payment infrastructure" or "3+ years with
# Kubernetes" recorded no requirement at all — 4,329 stored postings, 29% of those that state a
# number.
#
# Deliberately an ALLOW-LIST of connectors rather than "years + anything". Over-detecting here is
# the dangerous direction: a wrong yoe is compared against your ceiling and can hide a job you
# should have seen, whereas a missing one just falls back to "unstated = fine". So this matches
# only the shapes that introduce a requirement, and the lookahead drops the company-brag and
# elapsed-time senses — "founded 5 years ago", "10 years running", "3 years in business".
_YOE_OF = re.compile(
    r"(?i)(\d{1,2})\s*(?:\+|plus)?\s*(?:[-–—]|\bto\b)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)"
    r"\s+(?:of|in|with|using|building|developing|writing|working|leading|designing|shipping|"
    r"architecting|managing|programming|delivering|maintaining)\b"
    # not a possessive — "15+ years of THEIR health records" is a product claim, not a requirement
    r"(?!\s+(?:their|our|his|her|its|your\s+life))"
    r"(?!\s+(?:age|business|operation|existence|a\s+row))")
_YOE_NOT = re.compile(r"(?i)\b(?:ago|back|later|running|straight|in\s+a\s+row)\b")
# The loose pattern must ALSO sit inside a requirements passage. Without this it read the founding
# team's CV as the job's bar — "a team from Google, Facebook and Yahoo! with more than 20 years of
# digital advertising" became a 20-year requirement, which is the failure that HIDES jobs rather
# than merely missing a fact. The strict patterns above don't need this: "N years of experience"
# is self-identifying.
_YOE_CTX = re.compile(
    r"(?i)(?:require|requirement|qualification|you\s+(?:have|bring|will\s+have|should)|must\s+have"
    r"|we(?:'re|\s+are)\s+looking\s+for|what\s+(?:you|we)|minimum|at\s+least|ideally|preferr?ed"
    r"|proven|track\s+record|seeking|candidate|your\s+(?:background|experience)|skills"
    r"|experience\s+(?:in|with)|background\s+in|bring\s+to|nice\s+to\s+have|bonus\s+points)")


def yoe(text: str) -> int | None:
    """Years of experience the posting actually requires → int, or None if unstated.

    Two different rules, because they mean different things:
      * WITHIN one range ("3–5 years") the LOW end is the bar — 3 gets you in the door.
      * ACROSS separate requirements the MAX is the bar, because they're conjunctive:
        "Minimum 5 years iOS" AND "minimum 2 years leading" means you need 5, not 2.
    Taking the min across requirements is what makes a Staff role claim it wants 2 years.
    """
    if not text:
        return None
    # One space per blank stretch. _YOE and _YOE_OF open with five optional \s* runs in a row, so
    # a digit followed by a long blank stretch (HTML turned into text leaves those) made the match
    # polynomial in its length: 23 s for 200 spaces, hours for a few thousand. The regex engine
    # holds Python's lock all that time, so the whole app froze, web pages included.
    text = _BLANKS.sub(" ", text)
    found = []
    for m in _YOE.finditer(text):
        v = int(m.group(1))                      # range low, not high
        if 0 < v <= 25:
            found.append(v)
    for m in _YOE_ALT.finditer(text):
        v = int(m.group(1))
        if 0 < v <= 25:
            found.append(v)
    for m in _YOE_OF.finditer(text):
        # "over the past 5 years we grew" — the elapsed-time sense, not a requirement. Checked on
        # the few words either side rather than the whole posting, which would veto everything.
        if _YOE_NOT.search(text[max(0, m.start() - 24): m.end() + 24]):
            continue
        if not _YOE_CTX.search(text[max(0, m.start() - 140): m.start()]):
            continue                             # not inside a requirements passage
        v = int(m.group(1))                      # range low, same rule as _YOE
        if 0 < v <= 25:
            found.append(v)
    return max(found) if found else None


# ---------------------------------------------------------------- visa sponsorship

_NO_SPONSOR = re.compile(
    r"(?i)(?:"
    r"(?:will|can|does|do|are)\s*(?:not|n't)\s*(?:be\s+able\s+to\s+)?(?:offer|provide|consider)?\s*"
    r"(?:visa\s+|immigration\s+|employment\s+)?sponsor(?:ship)?"
    r"|un(?:able|willing)\s+to\s+sponsor"
    r"|not\s+(?:able|in\s+a\s+position)\s+to\s+sponsor"
    r"|no\s+(?:visa\s+)?sponsorship"
    r"|without\s+(?:the\s+need\s+for\s+)?(?:current\s+or\s+future\s+)?(?:visa\s+)?sponsorship"
    r"|does\s+not\s+sponsor"
    r"|sponsorship\s+is\s+not\s+(?:available|offered|provided)"
    r")")
_YES_SPONSOR = re.compile(
    r"(?i)(?:"
    r"(?:will|can|do|does|are\s+able\s+to|happy\s+to|glad\s+to)\s+(?:offer|provide|consider)?\s*"
    r"(?:visa\s+|immigration\s+)?sponsor(?:ship)?"
    r"|sponsorship\s+(?:is\s+)?(?:available|offered|provided)"
    r"|(?:visa|h-?1b|immigration)\s+sponsorship\s+available"
    r"|we\s+sponsor"
    r")")


def sponsorship(text: str) -> str:
    """'no' | 'yes' | '' — negatives checked FIRST because they're the explicit, load-bearing
    statement ("we will not sponsor"); a bare mention of the word must never read as a yes."""
    if not text:
        return ""
    if _NO_SPONSOR.search(text):
        return "no"
    if _YES_SPONSOR.search(text):
        return "yes"
    return ""


# ---------------------------------------------------------------- education

_EDU = [("PhD", r"(?i)\b(?:ph\.?\s?d|doctorate|doctoral)\b"),
        ("Master's", r"(?i)\b(?:master'?s?\s+degree|m\.?s\.?c?\b|mtech|m\.tech|mba)\b"),
        ("Bachelor's", r"(?i)\b(?:bachelor'?s?|b\.?s\.?c?\b|b\.?tech|btech|b\.?e\.?\b|undergraduate\s+degree)\b")]
_EDU_RX = [(lbl, re.compile(rx)) for lbl, rx in _EDU]
_NO_DEGREE = re.compile(r"(?i)(?:degree\s+(?:is\s+)?not\s+required|no\s+degree\s+required"
                        r"|or\s+equivalent\s+(?:practical\s+)?experience)")


def education(text: str) -> str:
    """Lowest degree the posting mentions (that's the bar you must clear), '' if none.
    'or equivalent experience' → '' , because the degree isn't actually a gate."""
    if not text:
        return ""
    if _NO_DEGREE.search(text):
        return ""
    hits = [lbl for lbl, rx in _EDU_RX if rx.search(text)]
    for lvl in ("Bachelor's", "Master's", "PhD"):     # lowest mentioned = the real floor
        if lvl in hits:
            return lvl
    return ""


# ---------------------------------------------------------------- tech stack

# (label, pattern, case_sensitive) — short/ambiguous names ("Go", "R", "C") are matched
# case-SENSITIVELY against the original text, or they'd hit every "go to" and "or".
TECH = [
    ("Python", r"\bpython\b", False), ("JavaScript", r"\bjavascript\b|\bjs\b", False),
    ("TypeScript", r"\btypescript\b|\bts\b", False), ("Java", r"\bjava\b(?!script)", False),
    ("Go", r"\bGolang\b|\bGo\b(?=[,./)\s]|$)", True), ("Rust", r"\brust\b", False),
    ("C++", r"c\+\+", False), ("C#", r"c#|\.net\b|dotnet", False), ("C", r"\bC\b(?=[,/)\s]|$)", True),
    ("Ruby", r"\bruby\b", False), ("PHP", r"\bphp\b", False), ("Scala", r"\bscala\b", False),
    ("Kotlin", r"\bkotlin\b", False), ("Swift", r"\bswift\b", False), ("Elixir", r"\belixir\b", False),
    ("R", r"\bR\b(?=[,/)\s]|$)", True), ("SQL", r"\bsql\b", False), ("Perl", r"\bperl\b", False),
    ("React", r"\breact(?:\.?js)?\b(?!\s*native)", False), ("React Native", r"react\s*native", False),
    ("Vue", r"\bvue(?:\.?js)?\b", False), ("Angular", r"\bangular\b", False), ("Svelte", r"\bsvelte\b", False),
    ("Next.js", r"next\.?js\b", False), ("Node.js", r"node\.?js\b|\bnode\b", False),
    ("Django", r"\bdjango\b", False), ("Flask", r"\bflask\b", False), ("FastAPI", r"fastapi", False),
    ("Rails", r"\brails\b|ruby on rails", False), ("Spring", r"\bspring\s*(?:boot|framework)?\b", False),
    ("Express", r"express(?:\.js)?\b", False),
    ("GraphQL", r"graphql", False), ("gRPC", r"\bgrpc\b", False), ("REST", r"\brest(?:ful)?\s*api", False),
    ("AWS", r"\baws\b|amazon web services", False), ("GCP", r"\bgcp\b|google cloud", False),
    ("Azure", r"\bazure\b", False), ("Kubernetes", r"kubernetes|\bk8s\b", False),
    ("Docker", r"\bdocker\b", False), ("Terraform", r"terraform", False),
    ("Kafka", r"\bkafka\b", False), ("Spark", r"\bspark\b", False), ("Airflow", r"airflow", False),
    # NB: the label must match the string in profile.yaml exactly — the scorer tests
    # `skill.lower() in tech_list`, so a profile saying "PostgreSQL" never matched a label
    # saying "Postgres", and the skill silently contributed nothing.
    ("PostgreSQL", r"postgres(?:ql)?|\bpsql\b", False), ("MySQL", r"\bmysql\b", False),
    ("MongoDB", r"mongo(?:db)?", False), ("Redis", r"\bredis\b", False),
    ("Celery", r"\bcelery\b", False), ("NGINX", r"\bnginx\b", False),
    ("Prisma", r"\bprisma\b", False), ("Redux", r"\bredux\b", False),
    ("Tailwind", r"tailwind", False), ("pytest", r"\bpytest\b", False),
    # the AI/LLM tooling that is this candidate's actual differentiator — without these
    # entries the enricher could not see LangChain or LangGraph in a posting at all
    ("LangChain", r"lang\s?chain", False), ("LangGraph", r"lang\s?graph", False),
    ("HuggingFace", r"hugging\s?face", False),
    ("Elasticsearch", r"elastic\s?search", False), ("Snowflake", r"snowflake", False),
    ("dbt", r"\bdbt\b", False), ("Hadoop", r"hadoop", False),
    ("PyTorch", r"pytorch", False), ("TensorFlow", r"tensorflow", False),
    ("LLM / GenAI", r"\bllms?\b|large language model|generative ai|\bgenai\b|\brag\b", False),
    ("Machine Learning", r"machine learning|\bml\b(?!\s*ops)", False),
    # the AI-engineering roles being targeted (evals/RL environments, LLM infra, agent runtimes,
    # AI backends, agents, dev tools). Each name is one the profile lists, so the scorer can match
    # it. Ambiguous short forms are left out: bare "SSE" is also server-side encryption, "PPO" a
    # health plan, "eval" a JavaScript call, and "bedrock" an everyday word.
    ("RAG", r"\brag\b|retrieval[\s-]augmented", False),
    ("Prompt Engineering", r"\bprompt[\s-]+(?:engineer(?:ing|s)?|design|tuning|optimi[sz]ation"
                           r"|management|templates?|chaining)\b"
                           r"|(?<!without )(?<!minimal )(?<!little )\bprompting\b", False),
    ("Vector Search", r"\bvector\s+(?:search|databases?|dbs?|stores?|index(?:es|ing)?|embeddings?)\b"
                      r"|\bpgvector\b|\bpinecone\b|\bweaviate\b|\bqdrant\b|\bmilvus\b|\bchroma(?:db)?\b"
                      r"|\bfaiss\b", False),
    ("MCP", r"\bmcp\b|model\s+context\s+protocol", False),
    ("AI Agents", r"\b(?:ai|llm|autonomous)\s+agents?\b|\bagentic\b|\bmulti[\s-]agent\b"
                  r"|\bagent\s+(?:frameworks?|loops?|runtimes?|orchestration)\b"
                  r"|\b(?:tool|function)[\s-]calling\b", False),
    ("LLM Evals", r"\bevals\b|\b(?:llm|model)\s+evaluations?\b|\bllm[\s-]as[\s-]a[\s-]judge\b"
                  r"|\bevaluation\s+(?:harness(?:es)?|frameworks?|pipelines?|suites?)\b|\bgraders?\b"
                  r"|\bswe[\s-]?bench\b|\bterminal[\s-]?bench\b|\bpass@(?:k|\d+)|\bhumaneval\b|\bmmlu\b"
                  r"|\b(?:llm|agent|ai|model|eval(?:uation)?)\s+benchmarks?\b"
                  r"|\brubric[\s-]based\b|\b(?:evaluation|grading|scoring|judge)\s+rubrics?\b"
                  r"|\b(?:llm|ai|model)[\s-]judges?\b|\bjudge\s+models?\b", False),
    ("Reinforcement Learning", r"reinforcement\s+learning|\brlhf\b|\brlaif\b|\breward\s+models?\b"
                               r"|\brl\s+(?:environments?|training|pipelines?|gyms?)\b|\bgrpo\b"
                               r"|\breward\s+(?:functions?|design|shaping|hacking|signals?)\b"
                               r"|\b(?:agent|rl)\s+trajector(?:y|ies)\b|\btrajectory\s+data\b", False),
    ("Sandboxing", r"\bsandbox(?:es|ing|ed)?\b|\bgvisor\b|\bfirecracker\b|\bseccomp\b|\bnsjail\b"
                   r"|\bcontainer\s+isolation\b", False),
    ("SSE / WebSockets", r"server[\s-]sent\s+events|\bweb[\s-]?sockets?\b", False),
    ("OpenTelemetry", r"open[\s-]?telemetry|\botel\b|\bdistributed\s+tracing\b", False),
    ("LLM APIs", r"\bopenai\b|\banthropic\b|\bclaude\b|\b(?:aws|amazon)\s+bedrock\b|\bvertex\s+ai\b"
                 r"|\bgemini\s+api\b|\blitellm\b|\b(?:llm|model|ai)\s+(?:gateway|router|proxy)\b"
                 r"|\bvllm\b", False),
    ("Spring AI", r"\bspring\s+ai\b", False), ("LangChain4j", r"\blangchain4j\b", False),
    ("LSP / AST", r"\blanguage\s+server|\blsp\b|\babstract\s+syntax\s+trees?\b|\basts?\b"
                  r"|\btree[\s-]sitter\b|\bcode\s+(?:search|indexing)\b", False),
    ("Terraform", r"terraform", False), ("CI/CD", r"ci/cd|continuous integration", False),
    ("Linux", r"\blinux\b", False), ("iOS", r"\bios\b", False), ("Android", r"\bandroid\b", False),
    ("Flutter", r"flutter", False), ("Unity", r"\bunity\b", False),
    ("Distributed Systems", r"distributed systems", False), ("Microservices", r"microservices?", False),
    # From the candidate's own work: the agent-evaluation harness (coding agents, git patches) and
    # webhook backends. Kept last so the 14-tag cap can only drop these, never an earlier skill.
    # "Git / GitHub", not "Git": the scorer also tests skill-in-title as a substring, and "git"
    # sits inside "digital". Lowercase "cursor" is a database or mouse cursor, so only "Cursor".
    ("Coding Agents", r"\bcoding\s+agents?\b|\bclaude\s+code\b|\bopenhands\b|\bcodex\b|\bcopilot\b"
                      r"|\bai\s+coding\s+(?:tools?|assistants?|agents?)\b|\bai[\s-]assisted\s+(?:coding|development)\b"
                      r"|\bswe[\s-]agent\b|(?-i:\bCursor\b)(?!\s*(?:-based|based|pagination))", False),
    ("Git / GitHub", r"\bgit\b|\bgithub\b|\bgitlab\b|\bbitbucket\b", False),
    ("Webhooks", r"\bwebhooks?\b", False),
]
_TECH_RX = [(lbl, re.compile(rx if cs else rx, 0 if cs else re.I)) for lbl, rx, cs in TECH]


def tech(text: str) -> list[str]:
    """Technologies named in the posting. Deduped, capped — a stack list, not a word cloud."""
    if not text:
        return []
    found, seen = [], set()
    for lbl, rx in _TECH_RX:
        if lbl not in seen and rx.search(text):
            seen.add(lbl)
            found.append(lbl)
    return found[:14]


# ---------------------------------------------------------------- employment type

_EMP = [("Internship", r"(?i)\b(?:intern(?:ship)?|co-?op)\b"),
        ("Contract", r"(?i)\b(?:contract(?:or)?|freelance|temporary|fixed[\s-]term|c2c)\b"),
        ("Part-time", r"(?i)\bpart[\s-]time\b"),
        ("Full-time", r"(?i)\bfull[\s-]time\b|\bpermanent\b")]
_EMP_RX = [(lbl, re.compile(rx)) for lbl, rx in _EMP]


def employment(title: str, text: str, structured: str = "") -> str:
    s = (structured or "").lower()
    if "intern" in s:
        return "Internship"
    if "contract" in s or "temporary" in s:
        return "Contract"
    if "part" in s and "time" in s:
        return "Part-time"
    blob = (title or "") + " " + (text or "")[:1500]
    for lbl, rx in _EMP_RX:
        if rx.search(blob):
            return lbl
    return "Full-time"


# ---------------------------------------------------------------- one-shot

def enrich(job: dict) -> dict:
    """Add every structured facet we can prove from the description. Never raises —
    a posting that resists parsing just carries fewer facets."""
    text = job.get("description") or ""
    try:
        sal = salary(text, job.get("salary_hint", ""))
        job["salary_min"] = sal.get("min", 0)
        job["salary_max"] = sal.get("max", 0)
        job["salary_cur"] = sal.get("cur", "")
        job["salary_period"] = sal.get("period", "")
        job["yoe"] = yoe(text) or 0
        job["sponsor"] = sponsorship(text)
        job["edu"] = education(text)
        job["tech"] = ", ".join(tech(text))
        job["emp_type"] = employment(job.get("title", ""), text, job.get("emp_hint", ""))
    except Exception:
        pass
    return job
