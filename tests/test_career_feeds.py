"""Öffentliche ATS-Feeds einschließlich Fallback, Firmengrenzen und Budget."""
import json
import unittest

import httpx

from research_app.career_feeds import discover_feed, read_feed
from research_app.karriere import CareerScope, parse_page
from research_app.research_inputs import CompanyQuery
import test_karriere as helpers
from test_karriere import ROOT, html, job


PERSONIO = 'https://acme.jobs.personio.de/'
GREENHOUSE = 'https://job-boards.greenhouse.io/acme'
GREENHOUSE_API = 'https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true'
ASHBY_API = 'https://api.ashbyhq.com/posting-api/job-board/acme'
ASHBY_SCRIPT = '''<script>var BOARD_SLUG = "acme";
var API_URL = "https://api.ashbyhq.com/posting-api/job-board/" + BOARD_SLUG;
fetch(API_URL).then(render);</script>'''
XML = '''<workzag-jobs><position><id>42</id><name>Data Engineer (m/w/d)</name>
<office>Berlin</office><additionalOffices><office>Hamburg</office></additionalOffices>
<jobDescriptions><jobDescription><name>Aufgaben</name><value><![CDATA[<p>Daten entwickeln</p>]]></value>
</jobDescription></jobDescriptions><employmentType>permanent</employmentType>
<schedule>full-time</schedule><createdAt>2020-01-01</createdAt></position></workzag-jobs>'''


def greenhouse_job(**extra):
    return dict({'id': 42, 'internal_job_id': 12, 'title': 'Data Engineer',
                 'absolute_url': GREENHOUSE + '/jobs/42', 'location': {'name': 'Berlin'},
                 'content': '&lt;p&gt;Daten entwickeln&lt;/p&gt;', 'updated_at': '2026-11-01'}, **extra)


class FeedParsingTests(unittest.TestCase):
    def test_inline_feed_urls_are_read_without_evaluating_javascript(self):
        links = parse_page(ASHBY_SCRIPT + f'<script>fetch("{PERSONIO}xml")</script>', ROOT)['links']
        self.assertEqual([link['url'] for link in links], [ASHBY_API, PERSONIO + 'xml'])
        self.assertTrue(all(link['mechanism'] == 'embedded_job_feed' for link in links))
        self.assertEqual(parse_page('<script>fetch("https://api.ashbyhq.com/posting-api/job-board/" + guessName())</script>', ROOT)['links'], [])

    def test_ashby_scope_cannot_cross_employers(self):
        scope = CareerScope([ASHBY_API])
        scope.add_feed(discover_feed(ASHBY_API), ASHBY_API)
        self.assertTrue(scope.contains('https://jobs.ashbyhq.com/acme/123'))
        self.assertFalse(scope.contains('https://jobs.ashbyhq.com/other/123'))
        self.assertFalse(scope.contains('https://api.ashbyhq.com/posting-api/job-board/other'))

    def test_only_recognized_employer_portals_supply_feeds(self):
        for url in [ROOT, 'https://personio.de/', 'https://acme.jobs.personio.de.evil.example/',
                    'https://boards.greenhouse.io/', 'https://boards.greenhouse.io/embed/job_board?for=../beta']:
            self.assertIsNone(discover_feed(url), url)
        self.assertEqual(discover_feed(PERSONIO).url, PERSONIO + 'xml')
        self.assertEqual(discover_feed('https://acme.jobs.personio.com/').provider, 'personio')
        self.assertEqual(discover_feed(GREENHOUSE).url, GREENHOUSE_API)

    def test_personio_xml_descriptions_locations_and_no_invented_posted_date(self):
        rows = read_feed(discover_feed(PERSONIO), XML)
        self.assertEqual(rows[0]['url'], PERSONIO + 'job/42')
        self.assertIn('Daten entwickeln', rows[0]['description'])
        self.assertEqual(len(rows[0]['jobLocation']), 2)
        self.assertNotIn('datePosted', rows[0])

    def test_invalid_feed_cannot_be_interpreted_as_no_jobs(self):
        for content in ['<html></html>', '<workzag-jobs><error/></workzag-jobs>',
                        '<workzag-jobs><position><name>Engineer</name></position></workzag-jobs>',
                        '<!DOCTYPE x [<!ENTITY e "x">]><workzag-jobs/>']:
            with self.subTest(content=content), self.assertRaises(ValueError):
                read_feed(discover_feed(PERSONIO), content)
        for data in [{}, {'jobs': [{}]}, {'jobs': [], 'meta': {'total': 1}}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                read_feed(discover_feed(GREENHOUSE), json.dumps(data))

    def test_embedded_greenhouse_script_is_discovered_without_running_javascript(self):
        document = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script><script src="/app.js"></script>'
        links = parse_page(document, ROOT)['links']
        self.assertEqual(len(links), 1)
        self.assertTrue(links[0]['embedded'])
        self.assertEqual(discover_feed(links[0]['url']).url, GREENHOUSE_API)

    def test_greenhouse_anchor_does_not_need_a_german_job_label(self):
        links = parse_page('<a href="https://boards.greenhouse.io/acme">Explore opportunities</a>', ROOT)['links']
        self.assertEqual(len(links), 1)
        self.assertTrue(links[0]['career'])

    def test_shared_boards_and_feed_are_restricted_to_employer(self):
        scope = CareerScope([GREENHOUSE])
        scope.add_feed(discover_feed(GREENHOUSE), GREENHOUSE)
        for url in ['https://boards.greenhouse.io/beta/jobs/1',
                    'https://job-boards.greenhouse.io/acmeevil/jobs/1',
                    'https://boards-api.greenhouse.io/v1/boards/beta/jobs',
                    'https://boards.greenhouse.io/embed/job_board?for=beta']:
            self.assertFalse(scope.contains(url), url)
        self.assertTrue(scope.contains('https://boards.greenhouse.io/acme/jobs/1'))
        self.assertTrue(scope.contains(GREENHOUSE_API))


class FeedCrawlerTests(unittest.TestCase):
    crawl = helpers.CareerCrawlerTests.crawl

    def test_inline_ashby_feed_collects_only_listed_jobs_with_descriptions(self):
        posting = dict(title='Engineer', isListed=True, jobUrl='https://jobs.ashbyhq.com/acme/123',
                       descriptionHtml='<p>Engineering work</p>', location='Berlin', workplaceType='Hybrid',
                       publishedAt='2026-11-20T10:00:00Z')
        data = dict(jobs=[posting, dict(posting, isListed=False, jobUrl='https://jobs.ashbyhq.com/acme/private')])
        result, calls = self.crawl({ROOT + '/': ASHBY_SCRIPT, ASHBY_API: json.dumps(data),
                                   'https://api.ashbyhq.com/robots.txt': httpx.Response(401)})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(result['jobs'][0]['description'], 'Engineering work')
        self.assertEqual(result['jobs'][0]['remote'], 'Hybrid')
        self.assertEqual(result['jobs'][0]['extraction'], 'ashby_feed')
        self.assertTrue(any(source['mechanism'] == 'embedded_job_feed' for source in result['sources']))
        self.assertNotIn('https://api.ashbyhq.com/robots.txt', calls)

    def test_public_api_redirect_cannot_reach_login_or_another_employer(self):
        for target in ('https://api.ashbyhq.com/login',
                       'https://api.ashbyhq.com/posting-api/job-board/other'):
            result, calls = self.crawl({ROOT + '/': ASHBY_SCRIPT,
                                       ASHBY_API: httpx.Response(302, headers={'location': target})})
            self.assertNotIn(target, calls)
            self.assertFalse(result['coverage']['complete'])

    def test_invalid_ashby_response_is_not_an_empty_success(self):
        result, _ = self.crawl({ROOT + '/': ASHBY_SCRIPT, ASHBY_API: json.dumps({'error': 'unavailable'})})
        self.assertNotEqual(result['status'], 'no_results')
        self.assertFalse(result['coverage']['complete'])

    def test_direct_personio_job_link_reads_feed_and_skips_remaining_details(self):
        links = ''.join(f'<a href="{PERSONIO}job/{number}">Engineer (m/w/d)</a>' for number in (42, 43, 44))
        result, calls = self.crawl({ROOT + '/': links, PERSONIO + 'xml': XML}, career_max_pages=2)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['jobs']), 1)
        self.assertFalse(result['pending_urls'])
        self.assertEqual(calls.count(PERSONIO + 'xml'), 1)
        self.assertFalse(any('/job/' in url for url in calls))

    def test_direct_personio_job_keeps_html_fallback_when_feed_unavailable(self):
        url = PERSONIO + 'job/42'
        result, calls = self.crawl({ROOT + '/': f'<a href="{url}">Engineer (m/w/d)</a>',
                                   PERSONIO + 'xml': httpx.Response(404), url: html(job(url=url))})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['jobs']), 1)
        self.assertIn(url, calls)
        self.assertEqual(result['adapter_attempts'][0]['status'], 'fallback_html')

    def test_employer_iframe_personio_feed_without_loading_portal_html(self):
        pages = {ROOT + '/': f'<a href="/careers">Careers</a>',
                 ROOT + '/careers': f'<iframe src="{PERSONIO}"></iframe>', PERSONIO + 'xml': XML}
        result, calls = self.crawl(pages)
        self.assertEqual(result['status'], 'ok')
        row = result['jobs'][0]
        self.assertEqual(row['extraction'], 'personio_feed')
        self.assertEqual(row['locations'], ['Berlin', 'Hamburg'])
        self.assertEqual(row['description'], 'Aufgaben Daten entwickeln')
        self.assertIsNone(row['date_posted'])
        self.assertEqual(row['source_url'], PERSONIO + 'xml')
        self.assertNotIn(PERSONIO, calls)
        self.assertTrue(any(source['mechanism'] == 'public_job_feed' for source in result['sources']))

    def test_greenhouse_prospects_are_not_counted_as_jobs(self):
        data = {'jobs': [greenhouse_job(), greenhouse_job(id=43, internal_job_id=None,
                         title='Future opportunities', absolute_url=GREENHOUSE + '/jobs/43')]}
        result, _ = self.crawl({ROOT + '/': f'<a href="{GREENHOUSE}">Careers</a>',
                               GREENHOUSE_API: json.dumps(data)})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(result['jobs'][0]['description'], 'Daten entwickeln')
        self.assertEqual(result['excluded'][0]['reason'], 'general_application')

    def test_invalid_or_disabled_feed_falls_back_to_html(self):
        for content in ['<workzag-jobs><error/></workzag-jobs>', httpx.Response(404)]:
            result, calls = self.crawl({PERSONIO + 'xml': content, PERSONIO: html(job(url='/job/42'))},
                                       company=CompanyQuery('Acme', 'Acme', career_urls=[PERSONIO]))
            self.assertEqual(result['status'], 'ok')
            self.assertIn(PERSONIO, calls)
            self.assertEqual(result['adapter_attempts'][0]['status'], 'fallback_html')

    def test_empty_feed_is_evidence_of_no_published_jobs(self):
        result, _ = self.crawl({PERSONIO + 'xml': '<workzag-jobs/>'},
                               company=CompanyQuery('Acme', 'Acme', career_urls=[PERSONIO]))
        self.assertEqual(result['status'], 'no_results')
        self.assertTrue(result['coverage']['complete'])

    def test_feed_jobs_and_http_limits_are_enforced(self):
        data = {'jobs': [greenhouse_job(), greenhouse_job(id=43, absolute_url=GREENHOUSE + '/jobs/43')]}
        company = CompanyQuery('Acme', 'Acme', career_urls=[GREENHOUSE])
        result, _ = self.crawl({GREENHOUSE_API: json.dumps(data)}, company=company, career_max_jobs=1)
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['coverage']['job_limit_reached'])
        result, calls = self.crawl({}, company=company, max_requests=1)
        self.assertTrue(result['coverage']['request_limit_reached'])
        self.assertNotIn(GREENHOUSE, calls)

    def test_feed_redirect_cannot_cross_tenants(self):
        target = 'https://boards-api.greenhouse.io/v1/boards/beta/jobs'
        result, calls = self.crawl({GREENHOUSE_API: httpx.Response(302, headers={'location': target}),
                                   GREENHOUSE: '<main id="app"></main>'},
                                   company=CompanyQuery('Acme', 'Acme', career_urls=[GREENHOUSE]))
        self.assertNotIn(target, calls)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['adapter_attempts'][0]['status'], 'fallback_html')

    def test_jobs_from_another_board_are_excluded(self):
        data = {'jobs': [greenhouse_job(absolute_url='https://job-boards.greenhouse.io/beta/jobs/42')]}
        result, _ = self.crawl({GREENHOUSE_API: json.dumps(data)},
                               company=CompanyQuery('Acme', 'Acme', career_urls=[GREENHOUSE]))
        self.assertFalse(result['jobs'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['excluded'][0]['reason'], 'outside_employer_scope')

    def test_redirect_to_portal_preserves_original_redirect_evidence(self):
        result, _ = self.crawl({ROOT + '/': httpx.Response(302, headers={'location': PERSONIO}),
                               PERSONIO: '<main></main>', PERSONIO + 'xml': XML})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['pages'][0]['portal_redirects'][0]['target'], PERSONIO)


if __name__ == '__main__':
    unittest.main()
