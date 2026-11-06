"""Read explicitly scoped vacancy sections on an employer's careers page.

Recipes select elements by tag, class, id or exact attributes. Fields never fall
back to the rest of the page: an office address in a footer is not a job location.
"""
from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

_VOID = set("area base br col embed hr img input link meta param source track wbr".split())
_DROP = set("script style noscript nav header footer svg form template".split())
_BLOCK = set("address article aside blockquote br dd div dl dt h1 h2 h3 h4 h5 h6 hr li main ol p section table td th tr ul".split())
_CLOSED = re.compile(
    r"\b(?:job|opening|position|role|vacancy|applications?)\s+"
    r"(?:(?:is|are)\s+(?:now\s+)?|(?:has|have)\s+(?:now\s+)?been\s+)?"
    r"(?:closed|filled|expired|no longer available)\b"
    r"|\bno longer accepting applications\b|\bnot currently hiring\b", re.I)


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def walk(self):
        for child in self.children:
            if isinstance(child, Node) and child.tag not in _DROP:
                yield child
                yield from child.walk()

    def text(self):
        if self.tag in _DROP:
            return ""
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                value = child.text()
                parts.append("\n" + value + "\n" if child.tag in _BLOCK else value)
        return "\n".join(filter(None, (re.sub(r"\s+", " ", line).strip()
                                      for line in "".join(parts).splitlines())))


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 25000 or len(self.stack) > 256:
            raise ValueError("Careers page exceeds section-parser node budget")
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def matches(node, selector):
    if not isinstance(selector, dict) or not selector:
        return False
    if selector.get("tag") and node.tag != selector["tag"]:
        return False
    if selector.get("id") and node.attrs.get("id") != selector["id"]:
        return False
    classes = selector.get("class", "")
    if isinstance(classes, str):
        classes = classes.split()
    if not set(classes).issubset(set((node.attrs.get("class") or "").split())):
        return False
    return all(k in node.attrs and node.attrs[k] == v
               for k, v in (selector.get("attrs") or {}).items())


def _select(node, selectors):
    current = [node]
    for selector in selectors if isinstance(selectors, list) else [selectors]:
        following, seen = [], set()
        for parent in current:
            for child in parent.walk():
                if id(child) not in seen and matches(child, selector):
                    following.append(child)
                    seen.add(id(child))
        current = following
    return current


def _field(section, spec):
    if not isinstance(spec, dict) or not spec:
        return ""
    nodes = _select(section, spec["select"]) if spec.get("select") else [section]
    index = spec.get("index", 0)
    if not isinstance(index, int) or index < 0 or index >= len(nodes):
        return ""
    node = nodes[index]
    value = node.attrs.get(spec["attr"], "") if spec.get("attr") else node.text()
    if spec.get("regex"):
        match = re.search(spec["regex"], value or "", re.I)
        if not match:
            return ""
        value = match.group(1) if match.lastindex else match.group()
    return (value or "").strip()


def extract(body, page_url, company, token, recipe):
    """Return current sections only. Dates are deliberately not inferred from HTML."""
    parser = _Tree()
    try:
        parser.feed(body[:2_000_000])
        parser.close()
    except (ValueError, RecursionError):
        return []
    fields = recipe.get("fields") or {}
    if not fields.get("title") or not fields.get("location"):
        return []
    jobs = []
    for section in _select(parser.root, recipe.get("section")):
        title = _field(section, fields["title"])
        location = _field(section, fields["location"])
        description = _field(section, fields.get("description") or {"text": True})
        status = _field(section, fields.get("status", {}))
        if not title or not location or len(title) > 200 or len(location) > 200:
            continue
        if status.lower() in ("closed", "expired", "filled", "inactive") or _CLOSED.search(description):
            continue
        link = _field(section, fields.get("url", {}))
        if link:
            url = urljoin(page_url, link)
            if urlsplit(url).scheme not in ("http", "https"):
                continue
        else:
            # Same careers page, with a deterministic record fragment, as for inline
            # JobPosting data. Prefer a real publisher anchor when one is available.
            anchor = (_field(section, fields.get("anchor", {})) or section.attrs.get("id")
                      or (title + " - " + location))
            url = page_url.split("#")[0] + "#" + quote(anchor, safe="")
        jobs.append(dict(url=url, title=title, location=location, company=company,
                         description=description[:20000], department="", posted_at="",
                         posted_field="", emp_hint=_field(section, fields.get("employment", {})),
                         ats="site", token=token))
        if len(jobs) >= 400:
            break
    # Ambiguous duplicate identities are withheld instead of silently overwriting.
    counts = Counter(job["url"] for job in jobs)
    return [job for job in jobs if counts[job["url"]] == 1]
