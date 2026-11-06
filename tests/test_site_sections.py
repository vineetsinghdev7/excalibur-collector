import unittest
from unittest.mock import patch

from collector.sources import site_sections, sites


RECIPE = {
    "method": "sections", "list_url": "https://employer.example/careers",
    "section": {"tag": "article", "class": "vacancy"},
    "fields": {
        "title": {"select": {"tag": "h3"}},
        "location": {"select": {"class": "location"}},
        "employment": {"select": {"class": "employment"}},
        "status": {"select": {"class": "status"}},
    },
}


def card(title, location="", extra="", anchor=""):
    return (f'<article class="vacancy featured" id="{anchor}"><h3>{title}</h3>'
            f'<span class="location">{location}</span>{extra}</article>')


def read(body, recipe=None):
    return site_sections.extract(body, RECIPE["list_url"], "Employer", "employer/careers",
                                 recipe or RECIPE)


class VacancySections(unittest.TestCase):
    def test_each_job_keeps_its_own_location_and_employment(self):
        rows = read(card("Backend &amp; API Engineer", "Remote, India",
                         '<div class="employment">Contract</div><p>Build APIs.</p>', "backend")
                    + card("Software Engineer", "London, UK", anchor="london")
                    + "<footer>Our offices: Mumbai and Bengaluru</footer>")
        self.assertEqual([j["location"] for j in rows], ["Remote, India", "London, UK"])
        self.assertEqual(rows[0]["title"], "Backend & API Engineer")
        self.assertEqual(rows[0]["emp_hint"], "Contract")
        self.assertEqual(rows[1]["emp_hint"], "")
        self.assertEqual(rows[0]["url"], RECIPE["list_url"] + "#backend")
        self.assertNotIn("Mumbai", rows[0]["description"])

    def test_missing_job_location_is_not_filled_from_office_footer(self):
        self.assertEqual(read(card("Engineer") + "<footer>India</footer>"), [])

    def test_comments_templates_navigation_and_footer_are_not_jobs(self):
        fake = card("Fake Engineer", "India")
        body = f"<!--{fake}--><template>{fake}</template><nav>{fake}</nav><footer>{fake}</footer>"
        self.assertEqual(read(body + card("Real Engineer", "Pune"))[0]["title"], "Real Engineer")
        self.assertEqual(len(read(body + card("Real Engineer", "Pune"))), 1)

    def test_closed_and_expired_roles_are_withheld(self):
        body = (card("A Engineer", "Pune", '<span class="status">Expired</span>')
                + card("B Engineer", "Pune", "<p>This position has been filled.</p>")
                + card("C Engineer", "Pune", "<p>Build data pipelines.</p>"))
        self.assertEqual([r["title"] for r in read(body)], ["C Engineer"])

    def test_common_closure_notices_are_not_active_vacancies(self):
        for notice in ("This job is closed.", "Applications are now closed.",
                       "This position is no longer available.", "This opening has now been filled."):
            with self.subTest(notice=notice):
                self.assertEqual(read(card("Engineer", "India", "<p>"+notice+"</p>")), [])

    def test_section_chain_excludes_inactive_cards_and_duplicate_tabs(self):
        active = card("Engineer", "India")
        inactive = active.replace('vacancy featured', 'vacancy inactive')
        body = '<div id="all">'+active+inactive+'</div><div id="tab">'+active+'</div>'
        recipe = {**RECIPE, "section": [{"id": "all"}, {"tag": "article", "attrs": {"class": "vacancy featured"}}]}
        self.assertEqual(len(read(body, recipe)), 1)

    def test_ambiguous_duplicate_identity_is_withheld(self):
        body = card("Engineer", "India") * 2 + card("Other Engineer", "India")
        self.assertEqual([r["title"] for r in read(body)], ["Other Engineer"])

    def test_record_fragments_are_stable_across_reordering(self):
        a, b = card("Engineer", "India"), card("Engineer", "Japan")
        self.assertEqual({r["url"] for r in read(a + b)}, {r["url"] for r in read(b + a)})
        self.assertEqual(len(read(a + b)), 2)

    def test_page_dates_are_never_used_as_publication_dates(self):
        rows = read('<meta property="article:published_time" content="2026-11-06">'
                    + card("Engineer", "India", "<time>Updated today</time>"))
        self.assertEqual(rows[0]["posted_at"], "")
        self.assertEqual(rows[0]["posted_field"], "")

    def test_scoped_selector_chain_regex_and_child_anchor(self):
        recipe = {**RECIPE, "fields": {
            "title": {"select": {"tag": "h3"}},
            "location": {"select": [{"class": "details"}, {"tag": "p"}],
                         "regex": r"Location:\s*(.+)"},
            "anchor": {"select": {"tag": "h3"}, "attr": "id"},
        }}
        body = ('<article class="vacancy"><h3 id="role-19">Engineer</h3>'
                '<div class="details"><p>Location: Bengaluru / Remote</p></div></article>')
        job = read(body, recipe)[0]
        self.assertEqual(job["location"], "Bengaluru / Remote")
        self.assertEqual(job["url"], RECIPE["list_url"] + "#role-19")

    def test_unmatched_location_regex_and_non_web_application_links_are_rejected(self):
        recipe = {**RECIPE, "fields": {
            **RECIPE["fields"], "location": {"regex": r"Location:\s*(.+)"},
        }}
        self.assertEqual(read(card("Engineer", "India"), recipe), [])
        recipe["fields"] = {**RECIPE["fields"], "url": {"select": {"tag": "a"}, "attr": "href"}}
        self.assertEqual(read(card("Engineer", "India", '<a href="javascript:void(0)">Apply</a>'), recipe), [])

    def test_reader_uses_one_list_request_and_does_not_fetch_each_card(self):
        body = card("Engineer", "India") + card("Other Engineer", "India")
        with patch.object(sites, "_get", return_value=(200, RECIPE["list_url"], body, [])) as get:
            rows = sites.run(RECIPE, "Employer", "employer/careers")
        self.assertEqual(len(rows), 2)
        get.assert_called_once()

    def test_pathological_depth_is_withheld(self):
        self.assertEqual(read("<div>" * 300 + card("Engineer", "India") + "</div>" * 300), [])
