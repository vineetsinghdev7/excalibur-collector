"""Conservative posting-level evidence for applicants based in India.

This identifies an advertised route, not an immigration or candidate-qualification
guarantee. Country names in company biographies and generic remote/relocation perks
are not evidence. Keep the original location and the source's wording intact.
"""
import html
import re
from html.parser import HTMLParser

from . import indiafeeds


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'li', 'div', 'br'): self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)


def plain(value):
    if not isinstance(value, str): return ''
    parser = _Text()
    parser.feed(value[:200000])
    return re.sub(r'[ \t\r\f\v]+', ' ', html.unescape(' '.join(parser.parts))).strip()


_EXCLUDES_INDIA = re.compile(
    r'\b(?:except|excluding|outside|not (?:in|available in|open to))\s+India\b'
    r'|\b(?:not|cannot|can[\x27’]t|don[\x27’]t)\s+(?:currently\s+)?(?:hire|hiring|accept|accepting)\s+(?:candidates |applicants )?(?:from|in) India\b'
    r'|\bIndia (?:is )?(?:excluded|not (?:supported|eligible))\b', re.I)
_RESTRICTION = re.compile(
    r'\b(?:must|required to)\s+(?:already\s+)?(?:be|hold|have|possess)\b[^.\n;]{0,65}'
    r'\b(?:citizenship|citizen|permanent residen|(?:existing|current|valid) (?:work|working) (?:permit|visa)|'
    r'(?:right|authori[sz](?:ation|ed)) to work)'
    r'|\b(?:US|U\.S\.|American|EU|European|UK|British|Canadian|Australian) citizens? (?:only|required)'
    r'|\b(?:authorization|authorisation|right|permission) to work\b[^.\n;]{0,110}\b(?:required|essential|mandatory)\b'
    r'|\b(?:US|U\.S\.|EU|UK|Canada|Australia|Europe)[ -]+only\b'
    r'|\b(?:only (?:hire|hiring|consider|accept)|must (?:currently |already )?(?:reside|be (?:based|located)))\b[^.\n;]{0,100}'
    r'|\b(?:active|current) (?:security clearance|TS/SCI)\b', re.I)
_NO_VISA = re.compile(
    r'\b(?:no|without)\s+(?:(?:the need for|current or future|visa|immigration|employment)\s+)*sponsorship\b'
    r'|\b(?:not|never|cannot|can[\x27’]t|won[\x27’]t|don[\x27’]t|doesn[\x27’]t|unable|unwilling)\b[^.\n;]{0,60}\b(?:sponsor|sponsorship|visa assistance|visa support)\b'
    r'|\b(?:sponsorship|visa support|visa assistance)\s+(?:is\s+)?(?:not|unavailable)\b', re.I)
_VISA = re.compile(
    r'\b(?:offer|offers|offering|provide|provides|providing|support|supports)\s+(?:full\s+)?(?:visa|immigration|work permit) sponsorship\b'
    r'|\b(?:visa|immigration|work permit) sponsorship\s+(?:is\s+)?(?:available|offered|provided|supported)\b'
    r'|\b(?:we|company)\s+(?:(?:can|will|do|may|are able to)\s+)?sponsor\s+(?:(?:your|a|the|work|employment)\s+)*visas?\b'
    r'|\b(?:visa|work permit)\s+(?:and relocation\s+)?(?:support|assistance)\s+(?:is\s+)?(?:available|provided|offered)\b'
    r'|\b(?:offer|offers|provide|provides)\s+(?:(?:full|relocation and)\s+)?(?:visa|work permit)\s+(?:support|assistance)\b'
    r'|\bvisa sponsorship and (?:overseas |international )?relocation support (?:is )?available\b'
    r'|\brelocation support,? (?:including|and) visa sponsorship(?: where applicable)?\b', re.I)
# Do not accidentally join unrelated list items into an offer such as
# "customer support [newline] visa sponsorship required?".
_VISA = re.compile(_VISA.pattern.replace(r'\s+', r'[ \t]+'), re.I)
_RELOCATION_VISA_BENEFIT = re.compile(r'\b(?:Japan |international |overseas )?relocation support[ \t]*\n[ \t]*visa sponsorship\s*(?:\n|$)', re.I)
_RELOCATION = re.compile(
    r'\b(?:international|overseas) relocation\s+(?:support|assistance|package)\b'
    r'|\brelocation\s+(?:support|assistance|package)\s+(?:is\s+)?(?:available\s+)?for\s+(?:international|overseas)\s+(?:hires|candidates|applicants|employees)\b', re.I)
_NO_RELOCATION = re.compile(r'\b(?:no|not|cannot|unable)\b[^.\n;]{0,45}\b(?:relocation|relocate)\b', re.I)
_WORLD = re.compile(r'^(?:(?:remote|home[- ]based)\s*[-–—,:()]?\s*)?(?:worldwide|anywhere|global|anywhere in the world|world)\s*\)?$', re.I)
_GENERIC_REMOTE = re.compile(r'^(?:remote|distributed|remote\s*[-–—,:()]?\s*(?:worldwide|global|anywhere)\)?)?$', re.I)
_REMOTE_OFFER = re.compile(
    r'\b(?:work|working) (?:remotely )?from anywhere in the world\b'
    r'|\b(?:hire|hiring|accept(?:ing)? (?:candidates|applicants)) (?:remotely )?(?:worldwide|globally|from anywhere in the world)\b'
    r'|\bremote\s*[-–—,(/]?\s*worldwide\b', re.I)


def evidence(job):
    """Return basis + source excerpt, or None when eligibility is unconfirmed.

    Negative visa wording blocks overseas routes, but does not disqualify a job
    located in India or a worldwide remote job that needs no overseas work visa.
    """
    if not isinstance(job, dict) or not isinstance(job.get('location', ''), str): return None
    location = plain(job.get('location', ''))
    title = plain(job.get('title', ''))
    description = plain(job.get('description', ''))
    text = '\n'.join((title, location, description))
    if _EXCLUDES_INDIA.search(text): return None
    # Requirements must not be overridden by a generic global benefits paragraph.
    restrictions = list(_RESTRICTION.finditer(text))
    for match in restrictions:
        clause = re.split(r'[\n.;]', text[match.start():match.end()+70])[0]
        if re.search(r'\b(?:based|located|reside|work)\s+in\s+India\b', clause, re.I): continue
        # A current residence OR willingness to relocate is not a local-only bar.
        if re.search(r'\bor willing to relocate\b', match.group(), re.I) and not re.search(r'citizen|work permit|authori[sz]|clearance', match.group(), re.I): continue
        return None
    if indiafeeds.india_eligible(location) and not _WORLD.fullmatch(location):
        return {'basis': 'india', 'evidence': location}
    if _WORLD.fullmatch(location):
        return {'basis': 'worldwide_remote', 'evidence': location}
    if _GENERIC_REMOTE.fullmatch(location):
        match = _REMOTE_OFFER.search(title + '\n' + description)
        if match: return {'basis': 'worldwide_remote', 'evidence': match.group()}
    if _NO_VISA.search(text): return None
    match = _VISA.search(text) or _RELOCATION_VISA_BENEFIT.search(text)
    if match: return {'basis': 'visa_sponsorship', 'evidence': match.group()}
    if not _NO_RELOCATION.search(text):
        match = _RELOCATION.search(text)
        if match: return {'basis': 'international_relocation', 'evidence': match.group()}
    return None
