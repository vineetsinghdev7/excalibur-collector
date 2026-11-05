"""Public Jobvite tables/cards and their same-board listing navigation."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class Listing(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, token):
        super().__init__(convert_charrefs=True)
        self.token = token
        self.stack, self.fields = [], []
        self.row = None
        self.rows, self.links = [], []

    def listing_url(self, value):
        url = urljoin("https://jobs.jobvite.com/" + self.token + "/jobs", value)
        p = urlsplit(url)
        if (p.scheme == "https" and p.netloc == "jobs.jobvite.com" and
                p.path.rstrip("/") in (f"/{self.token}/search", f"/{self.token}/jobs/viewall")):
            return url.split("#", 1)[0]
        return ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        if tag not in self.VOID:
            self.stack.append(tag)
        depth = len(self.stack)
        if self.row is None and (tag in ("tr", "li") or classes & {"jv-job-item", "jv-featured-job"}):
            self.row = {"depth": depth, "url": "", "title": [], "location": []}
        if self.row is not None:
            for key, names in (("title", {"jv-job-list-name", "jv-featured-job-title"}),
                               ("location", {"jv-job-list-location", "jv-featured-job-location"})):
                if classes & names:
                    self.fields.append((depth, key))
            if tag == "a":
                href = attrs.get("href", "")
                p = urlsplit(urljoin("https://jobs.jobvite.com", href))
                if p.scheme == "https" and p.netloc == "jobs.jobvite.com" and p.path.startswith(f"/{self.token}/job/"):
                    self.row["url"] = "https://jobs.jobvite.com" + p.path
        if tag in ("a", "form"):
            url = self.listing_url(attrs.get("href" if tag == "a" else "action", ""))
            kind = ("next" if "jv-pagination-next" in classes else
                    "all" if urlsplit(url).path.rstrip("/").endswith("/jobs/viewall") else "search")
            if url and (kind != "search" or tag == "form"):
                self.links.append((kind, url))

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.row is not None and depth <= self.row["depth"]:
            if self.row["url"] and self.row["title"]:
                self.rows.append({k: " ".join(v).strip() if isinstance(v, list) else v
                                  for k, v in self.row.items() if k != "depth"})
            self.row = None
        self.fields = [(d, key) for d, key in self.fields if d < depth]
        del self.stack[depth - 1:]

    def handle_data(self, data):
        if self.row is not None:
            for key in {key for _, key in self.fields}:
                self.row[key].append(data)


def collect(token, read):
    pending = [f"https://jobs.jobvite.com/{token}/jobs"]
    visited, jobs = set(), {}
    # Keep a broken pagination loop or a huge board within one polling budget. This
    # returns an ordinary list, never an authoritative snapshot that could close jobs.
    while pending and len(visited) < 10:
        url = pending.pop(0)
        if url in visited:
            continue
        visited.add(url)
        page = Listing(token)
        page.feed(read(url))
        for row in page.rows:
            jobs[row["url"]] = row
        for kind, link in page.links:
            if kind == "next" or kind == "all" or (kind == "search" and not page.rows):
                if link not in visited and link not in pending:
                    pending.append(link)
    return list(jobs.values())
