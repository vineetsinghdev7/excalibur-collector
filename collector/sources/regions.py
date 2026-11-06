"""Which part of the world a job posting is in.

Both exported lists break their results down by region, and both had written out their own table
of countries and cities to do it. The tables had drifted — badly enough to change the answer:

    a role in Ahmedabad counted as India in one list and as nothing in the other;
    the same for Brisbane, Shenzhen, Bangkok, Hamburg, Rome, Cape Town, Lima, Bellevue.

So the two lists disagreed about where the jobs were, and neither was wrong on its own terms.
This is the union — every place either table knew.

The two exports still ASK different questions, and that difference is real: one wants the single
region a posting is in, the other wants every region a company hires across. Those are two
functions over one table, which is the shape the duplication was hiding.
"""
from __future__ import annotations

import re

# India, in one place. The scorer (+20 for a place you can work), the Jobs page's Location filter
# and both exports used to keep their own city lists, and the scorer's was a bare substring test
# that read "Indiana" as India. Cities fold their old and new names together (Bangalore ->
# Bengaluru). Word-bounded, so "Indiana" and "Indianapolis" are not India.
INDIA_CITIES = {"bengaluru": "Bengaluru", "bangalore": "Bengaluru", "hyderabad": "Hyderabad",
                "pune": "Pune", "chennai": "Chennai", "gurugram": "Gurugram", "gurgaon": "Gurugram",
                "noida": "Noida", "mumbai": "Mumbai", "delhi": "Delhi", "kolkata": "Kolkata",
                "ahmedabad": "Ahmedabad", "kochi": "Kochi", "cochin": "Kochi",
                "thiruvananthapuram": "Thiruvananthapuram", "trivandrum": "Thiruvananthapuram",
                "jaipur": "Jaipur", "indore": "Indore", "chandigarh": "Chandigarh",
                "coimbatore": "Coimbatore", "mysuru": "Mysuru", "mysore": "Mysuru",
                "bhubaneswar": "Bhubaneswar", "nagpur": "Nagpur", "vadodara": "Vadodara",
                "visakhapatnam": "Visakhapatnam", "lucknow": "Lucknow",
                "mohali": "Mohali", "bhilai": "Bhilai", "vellore": "Vellore",
                "kozhikode": "Kozhikode", "calicut": "Kozhikode"}
# State-only locations remain states, not inferred cities. Punjab alone is ambiguous.
_INDIA_STATES = ("kerala",)
_INDIA = r"\b(?:india|" + "|".join((*INDIA_CITIES, *_INDIA_STATES)) + r")\b"
_INDIA_RX = re.compile(_INDIA, re.I)
_INDIA_CITY_RX = re.compile(r"\b(" + "|".join(INDIA_CITIES) + r")\b", re.I)


def is_india(loc: str) -> bool:
    """Is this location in India? One answer for the scorer, the Jobs page and the exports."""
    return bool(_INDIA_RX.search(loc or ""))


def india_cities(text: str) -> list[str]:
    """The Indian cities a location names, by their current names, sorted."""
    return sorted({INDIA_CITIES[m.lower()] for m in _INDIA_CITY_RX.findall(text or "")})


# Order matters: the first match wins in `first()`, so the specific regions come before the
# broad ones. India before APAC, UK before Europe.
REGIONS = [
    ("India", _INDIA),
    ("UK & Ireland", r"united kingdom|\buk\b|london|manchester|edinburgh|cambridge|bristol|ireland|dublin"),
    ("Europe", r"germany|berlin|munich|hamburg|france|paris|spain|madrid|barcelona|netherlands|amsterdam|poland|warsaw|krakow|portugal|lisbon|porto|sweden|stockholm|norway|oslo|denmark|copenhagen|finland|helsinki|switzerland|zurich|geneva|austria|vienna|italy|milan|rome|estonia|tallinn|lithuania|latvia|romania|bucharest|czech|prague|ukraine|kyiv|belgium|brussels|greece|athens|europe|emea"),
    ("Israel & MEA",
     r"israel|tel aviv|herzliya|dubai|abu dhabi|\buae\b|saudi|egypt|cairo|nigeria|lagos|kenya|"
     r"nairobi|south africa|cape town"),
    ("Canada", r"canada|toronto|vancouver|montreal|ottawa|waterloo|calgary"),
    ("APAC",
     r"singapore|japan|tokyo|australia|sydney|melbourne|brisbane|new zealand|auckland|korea|seoul|"
     r"hong kong|china|shanghai|beijing|shenzhen|taiwan|taipei|indonesia|jakarta|vietnam|hanoi|"
     r"philippines|manila|malaysia|kuala lumpur|thailand|bangkok|apac"),
    ("LATAM",
     r"brazil|s(?:a|ã)o paulo|mexico|argentina|buenos aires|colombia|bogot|chile|santiago|peru|(?<![a-z])lima(?![a-z])|"
     r"latam"),
    ("USA",
     r"\bus\b|u\.s\.|usa|united states|san francisco|new york|\bnyc\b|seattle|austin|boston|chicago|"
     r"denver|los angeles|atlanta|miami|dallas|houston|portland|san diego|san jose|palo alto|"
     r"mountain view|sunnyvale|redmond|bellevue|california|texas|washington|virginia|remote - us"),
]

# Remote is not a region, it is the absence of one — so it sits outside the table and is only
# consulted when nothing else matched.
#
# "global" is here because one of the two old copies had it and the other did not, which is how
# "SF Global HQ" counted as remote in one export and not the other. Since this is only consulted
# after every region has failed to match, widening it can move a location from "Other" to
# "Remote / Global" and can never take one away from a real region.
REMOTE = re.compile(r"(?i)\bremote\b|anywhere|distributed|global")

_RX = [(label, re.compile(rx, re.I)) for label, rx in REGIONS]


def first(loc: str) -> str:
    """The one region THIS posting is in.

    Counted per posting, not per company: a company with 200 US roles and 1 Bangalore role must
    add 1 to India, not 200.
    """
    for label, rx in _RX:
        if rx.search(loc or ""):
            return label
    return "Remote / Global" if REMOTE.search(loc or "") else "Other"


def all_of(locations) -> list[str]:
    """Every region a company hires across, in table order, plus Remote if any posting says so."""
    out = []
    for loc in locations:
        for label, rx in _RX:
            if rx.search(loc or "") and label not in out:
                out.append(label)
        if REMOTE.search(loc or "") and "Remote" not in out:
            out.append("Remote")
    return out
