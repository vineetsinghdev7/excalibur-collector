"""Read Manatal's public subdomain career pages without guessing missing job fields."""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, parse_qs

HOST = re.compile(r"[a-z0-9-]+\.careers-page\.com", re.I)
POOL = re.compile(r"\b(?:talent pool|talent pipeline|talent-interest draft|future opportunities|building a candidate network|welcomes expressions of interest|not (?:an? )?(?:immediate|active|current|specific) (?:job|vacancy|opening))\b", re.I)


class Listing(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.row = None
        self.rows = []
        self.description = False
        self.next_anchor = None
        self.next_url = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        if tag == "article" and "job-card" in classes:
            self.row = {"description": []}
        if self.row is not None:
            if tag == "a" and "job-title-link" in classes:
                url = urljoin(self.url, attrs.get("href", ""))
                parsed = urlsplit(url)
                if (parsed.scheme == "https" and parsed.netloc == urlsplit(self.url).netloc
                        and re.fullmatch(r"/jobs/[a-zA-Z0-9-]+", parsed.path)):
                    self.row.update(url=url, title=attrs.get("data-job-title", "").strip(),
                        location=", ".join(x for x in (attrs.get("data-job-city", "").strip(),
                                                      attrs.get("data-job-country", "").strip()) if x))
            if tag == "p":
                self.description = True
        if tag == "a" and "btn-pagination" in classes and "btn-pagination-disabled" not in classes:
            self.next_anchor = (urljoin(self.url, attrs.get("href", "")), [])

    def handle_data(self, value):
        if self.row is not None and self.description:
            self.row["description"].append(value)
        if self.next_anchor is not None:
            self.next_anchor[1].append(value)

    def handle_endtag(self, tag):
        if tag == "p":
            self.description = False
        if tag == "article" and self.row is not None:
            self.row["description"] = " ".join(self.row["description"]).strip()
            if (self.row.get("url") and self.row.get("title") and
                    not POOL.search(self.row["title"] + " " + self.row["description"])):
                self.rows.append(self.row)
            self.row = None
        if tag == "a" and self.next_anchor is not None:
            url, label = self.next_anchor
            p = urlsplit(url)
            if (" ".join(label).strip().casefold() == "next" and p.scheme == "https"
                    and p.netloc == urlsplit(self.url).netloc and p.path in ("", "/")
                    and parse_qs(p.query).get("page", [""])[0].isdigit()):
                self.next_url = url.split("#", 1)[0]
            self.next_anchor = None


def collect(host, read):
    if not HOST.fullmatch(host) or host.casefold() == "www.careers-page.com":
        return []
    url = "https://" + host + "/"
    visited, rows = set(), {}
    # Bounded pagination; ordinary lists never assert that unseen jobs have closed.
    while url and url not in visited and len(visited) < 30:
        visited.add(url)
        page = Listing(url)
        page.feed(read(url))
        for row in page.rows:
            rows[row["url"]] = row
        url = page.next_url
    return list(rows.values())
