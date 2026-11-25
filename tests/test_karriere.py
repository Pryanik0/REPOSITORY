"""Karrieresuche mit simulierten Firmen-/ATS-Seiten, ohne Netzwerk."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openpyxl import Workbook

import main
from research_app.karriere import CareerScope, career_link_url, collect_karriere, crawl_company, job_key, parse_page
from research_app.research import SOURCE_GROUPS, make_parser
from research_app.research_inputs import CompanyQuery, load_companies, select_companies
from research_app.site_http import CrawlError, SiteHTTP, host_key


NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
ROOT = 'https://acme.example'


def job(title='Entwickler (m/w/d)', url='/jobs/engineer', **extra):
    return dict({'@type': 'JobPosting', 'title': title, 'url': url,
                 'description': '<p>Software entwickeln</p>', 'hiringOrganization': {'name': 'Acme'},
                 'jobLocation': {'address': {'addressLocality': 'Berlin', 'addressCountry': {'name': 'DE'}}}}, **extra)


def html(*items, body=''):
    return '<html><body><main>' + body + '</main><script type="application/ld+json">' + json.dumps(list(items)) + '</script></body></html>'


def arguments(**extra):
    return SimpleNamespace(**dict({'career_max_pages': 40, 'career_max_jobs': 100,
                                  'career_max_requests': 100, 'career_keyword': []}, **extra))


class CareerParsingTests(unittest.TestCase):
    def test_application_forms_and_language_versions_do_not_count_twice(self):
        self.assertEqual(job_key('https://jobs.example/Engineer-de-j42.html'),
                         job_key('https://jobs.example/Engineer-de-f42.html'))
        self.assertEqual(job_key('https://www.seachefs.com/job/detail/artist--bf7hd4qh'),
                         job_key('https://www.seachefs.com/en/job/detail/artist--bf7hd4qh'))
        document = html(body='<h1>Engineer (m/w/d)</h1><p>' + 'Job description. ' * 15 + '</p>')
        parsed = parse_page(document, 'https://jobs.example/Engineer-de-f42.html', detail_hint=True)
        self.assertFalse(parsed['jobs'])
        self.assertEqual(parsed['excluded'][0]['reason'], 'application_form')
        links = parse_page('<a href="/Engineer-de-f42.html">Jetzt bewerben</a>', 'https://jobs.example')['links']
        self.assertFalse(links)
        self.assertEqual(job_key('https://www.rosenberger.com/career/jobs/detail/?tx_rossapjobs_job[job]=1424&cHash=one'),
                         job_key('https://www.rosenberger.com/de/karriere/stellenangebote/detail/?tx_rossapjobs_job[job]=1424&cHash=two'))
        self.assertNotEqual(job_key('https://www.rosenberger.com/career/jobs/detail/?tx_rossapjobs_job[job]=1424'),
                            job_key('https://www.rosenberger.com/career/jobs/detail/?tx_rossapjobs_job[job]=1468'))

    def test_recruiting_slogan_does_not_replace_the_actual_job_heading(self):
        document = html(body='<h1>Smile with our team!</h1><h1>Payroll Specialist (m/w/d)</h1>'
                        '<p>' + 'Tasks and requirements. ' * 10 + '</p><h1>See you soon!</h1>')
        parsed = parse_page(document, ROOT + '/Vacancies/42/Description/1', detail_hint=True)
        self.assertEqual(parsed['jobs'][0]['title'], 'Payroll Specialist (m/w/d)')

    def test_recruitment_information_and_talent_community_are_not_vacancies(self):
        for title in ['Job Openings', 'Application Process', 'Bewerbungsprozess', 'Work with us',
                      'Join our Talent Community']:
            with self.subTest(title=title):
                document = html(body='<h1>' + title + '</h1><p>' + 'Information about recruiting. ' * 10 +
                                '</p><a href="/apply">Apply now</a>')
                self.assertFalse(parse_page(document, ROOT + '/jobs/information', detail_hint=True)['jobs'])

    def test_search_variants_keep_pagination_and_location_but_not_sort_or_language(self):
        self.assertEqual(career_link_url('https://jobs.example/search/?startrow=25&locationsearch=Berlin&locale=en_US&sortColumn=title&sortDirection=desc'),
                         'https://jobs.example/search/?startrow=25&locationsearch=Berlin')
        self.assertEqual(job_key('https://jobs.example/Engineer-de-j42.html'),
                         job_key('https://jobs.example/Engineer-eng-j42.html'))
        self.assertNotEqual(job_key('https://jobs.example/Engineer-de-j42.html'),
                            job_key('https://jobs.example/Engineer-de-j43.html'))

    def test_graph_locations_expiry_and_invalid_json(self):
        document = html({'@graph': [job(), job('Alt', '/jobs/old', validThrough='2026-11-11'),
                                  job('Heute', '/jobs/today', validThrough='2026-11-12')]})
        result = parse_page(document + '<script type="application/ld+json">bad json</script>', ROOT, now=NOW)
        self.assertEqual([r['title'] for r in result['jobs']], ['Entwickler (m/w/d)', 'Heute'])
        self.assertEqual(result['jobs'][0]['locations'], ['Berlin, DE'])
        self.assertEqual(result['excluded'][0]['reason'], 'expired')

    def test_expiry_timestamp_and_date_use_berlin_day(self):
        stamp = datetime(2026, 11, 12, 23, 30, tzinfo=timezone.utc)
        result = parse_page(html(job(validThrough='2026-11-12')), ROOT, now=stamp)
        self.assertFalse(result['jobs'])
        result = parse_page(html(job(validThrough='2026-11-12T11:59:59Z')), ROOT, now=NOW)
        self.assertFalse(result['jobs'])

    def test_navigation_is_not_a_job_and_closed_html_is_excluded(self):
        result = parse_page('<h1>Karriere</h1><a href="/jobs">Offene Stellen</a>', ROOT, detail_hint=True)
        self.assertFalse(result['jobs'])
        result = parse_page('<main><h1>Entwickler (m/w/d)</h1><p>Diese Stelle ist bereits besetzt.</p></main>', ROOT, detail_hint=True)
        self.assertEqual(result['excluded'][0]['reason'], 'closed')

    def test_available_job_is_not_mistaken_for_closed(self):
        result = parse_page(html(job(), body='<p>Diese Stelle ist verfügbar.</p>'), ROOT, now=NOW, detail_hint=True)
        self.assertEqual(len(result['jobs']), 1)

    def test_multiple_jobs_without_urls_are_not_merged(self):
        first, second = job(), job('Analyst')
        first.pop('url'); second.pop('url')
        self.assertFalse(parse_page(html(first, second), ROOT, now=NOW)['jobs'])

    def test_html_detail_and_tracking_link_cleanup(self):
        document = '<main><h1>Entwickler (m/w/d)</h1><p>' + 'Aufgaben und Qualifikationen. ' * 10 + '</p><a href="/apply">Jetzt bewerben</a></main>'
        parsed = parse_page(document, ROOT + '/jobs/engineer', detail_hint=True)
        self.assertEqual(parsed['jobs'][0]['extraction'], 'html_job_detail')
        links = parse_page('<a href="/jobs/engineer?utm_source=x#top">Entwickler (m/w/d)</a>', ROOT)['links']
        self.assertEqual(links[0]['url'], ROOT + '/jobs/engineer')

    def test_footer_relative_domain_is_not_a_job_link(self):
        result = parse_page('<a href="www.agency.example">Agency</a><a href="#top">Top</a><iframe src=""></iframe><a href="">Empty</a>', ROOT + '/jobs/engineer/')
        self.assertFalse(result['links'])

    def test_direct_nonstandard_html_job_url(self):
        document = '<main><h1>Entwickler (m/w/d)</h1><p>' + 'Aufgaben und Qualifikationen. ' * 10 + '</p></main>'
        self.assertEqual(len(parse_page(document, ROOT + '/opportunity')['jobs']), 1)

    def test_hidden_empty_state_is_not_proof_of_no_jobs(self):
        result = parse_page('<main><div hidden>Derzeit keine offenen Stellen.</div><div id="app"></div></main>', ROOT)
        self.assertFalse(result['explicit_empty'])

    def test_general_applications_are_not_open_positions(self):
        result = parse_page(html(job('Initiativbewerbung'), job('HERMES JobAgent', '/jobs/alert')), ROOT)
        self.assertFalse(result['jobs'])
        self.assertEqual({r['reason'] for r in result['excluded']}, {'general_application'})


class CareerCrawlerTests(unittest.TestCase):
    def test_university_career_board_does_not_introduce_other_employers(self):
        vacancy = ROOT + '/jobs/engineer'
        board = 'https://www.careerservice.kit.edu/de/studierende/berufsorientierung/kit-karrieremesse/'
        result, calls = self.crawl({
            ROOT + '/': f'<a href="{vacancy}">Engineer (m/w/d)</a><a href="{board}">KIT Karrieremesse</a>',
            vacancy: html(job(url=vacancy)),
        })
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['jobs']), 1)
        self.assertFalse(any('kit.edu' in url for url in calls))

    def test_application_form_is_not_fetched_as_an_extra_job(self):
        vacancy = ROOT + '/Engineer-de-j42.html'
        form = ROOT + '/Engineer-de-f42.html'
        result, calls = self.crawl({
            ROOT + '/': f'<a href="{vacancy}">Engineer (m/w/d)</a>',
            vacancy: html(job(url=vacancy), body=f'<a href="{form}">Jetzt bewerben</a>'),
        })
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['jobs']), 1)
        self.assertNotIn(form, calls)

    def test_sea_chefs_language_variants_share_one_job_and_page_budget(self):
        root = 'https://www.seachefs.com'
        german = root + '/job/detail/artist--bf7hd4qh'
        english = root + '/en/job/detail/artist--bf7hd4qh'
        result, calls = self.crawl({
            root + '/': f'<a href="{english}">Artist (m/w/d)</a><a href="{german}">Artist (m/w/d)</a>',
            german: html(job('Artist (m/w/d)', german)),
        }, company=CompanyQuery('Acme', 'Acme', urls=[root]), career_max_pages=2)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['jobs']), 1)
        self.assertFalse(result['pending_urls'])
        self.assertNotIn(english, calls)

    def test_language_duplicates_and_career_fairs_do_not_consume_pages(self):
        first = ROOT + '/Engineer-de-j42.html'
        second = ROOT + '/Engineer-eng-j42.html'
        result, calls = self.crawl({ROOT + '/': f'<a href="{first}">Engineer (m/w/d)</a>'
                                   f'<a href="{second}">Engineer (m/w/d)</a>'
                                   '<a href="https://www.her-career.com/">her career</a>',
                                   first: html(job(url=first))}, career_max_pages=2)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['jobs']), 1)
        self.assertFalse(result['pending_urls'])
        self.assertNotIn(second, calls)
        self.assertFalse(any('her-career.com' in url for url in calls))

    def crawl(self, pages, *, company=None, **options):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            if request.url.path == '/robots.txt':
                return httpx.Response(200, text='User-agent: *\nDisallow: /jobs/secret')
            value = pages.get(str(request.url))
            if value is None:
                return httpx.Response(404)
            if isinstance(value, httpx.Response):
                return value
            return httpx.Response(200, text=value, headers={'content-type': 'text/html'})
        with TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with SiteHTTP(output=Path(folder), delay=0, client=client,
                             max_requests=options.pop('max_requests', 100)) as http:
                result = crawl_company(company or CompanyQuery('Acme', 'Acme', urls=[ROOT]), arguments(**options), http)
        return result, calls

    def test_configured_sitemap_collects_only_jobs_in_selected_locations(self):
        portal = 'https://careers.acme.example'
        sitemap = portal + '/sitemap.xml'
        english = portal + '/job/Munich-Analyst/123-en_US/'
        job_url = portal + '/job/Munich-Analyst/123-de_DE/'
        foreign_job = portal + '/job/Boston-Analyst/124/'
        company = CompanyQuery('Acme', 'Acme', urls=[ROOT], career_urls=[sitemap],
                               career_sitemap_locations=['Munich'])
        pages = {
            sitemap: f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                     f'<url><loc>{english}</loc></url><url><loc>{job_url}</loc></url>'
                     f'<url><loc>{foreign_job}</loc></url></urlset>',
            english: '<div itemscope itemtype="http://schema.org/JobPosting">'
                     '<h1 itemprop="title">Analyst</h1><div itemprop="description">Analyze data</div></div>',
            job_url: '<div itemscope itemtype="http://schema.org/JobPosting">'
                     '<h1 itemprop="title">Analyst</h1><div itemprop="description">Analyze data</div>'
                     f'<a href="{foreign_job}">Related job</a></div>',
        }
        result, calls = self.crawl(pages, company=company)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([(r['url'], r['locations']) for r in result['jobs']], [(job_url, ['Munich'])])
        self.assertEqual(result['jobs'][0]['reference'], '123')
        self.assertNotIn(foreign_job, calls)
        self.assertNotIn(ROOT + '/', calls)

    def test_company_to_ats_pagination_duplicates_and_aggregator(self):
        portal = 'https://acme.jobs.personio.de/'
        pages = {ROOT + '/': '<a href="/karriere">Karriere</a>',
                 ROOT + '/karriere': f'<iframe src="data:image/svg+xml;base64,x" data-src="{portal}"></iframe><a href="https://www.stepstone.de/jobs/x">Jobs</a>',
                 portal: '<a href="/job/1">Entwickler (m/w/d)</a><a href="?page=2" rel="next">Weiter</a>',
                 portal + '?page=2': '<a href="/job/1?utm_source=x">Entwickler (m/w/d)</a><a href="/job/2">Analyst (m/w/d)</a>',
                 portal + 'job/1': html(job(url='/job/1')),
                 portal + 'job/2': html(job('Analyst (m/w/d)', '/job/2'))}
        result, calls = self.crawl(pages)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['jobs']), 2)
        self.assertFalse(any('stepstone' in call for call in calls))
        self.assertEqual(calls.count(portal + 'job/1'), 1)
        self.assertTrue(any(source['url'] == portal and source['linked_from'] == ROOT + '/karriere'
                            for source in result['sources']))

    def test_shared_ats_tenant_is_not_crossed(self):
        scope = CareerScope(['https://jobs.smartrecruiters.com/Acme'])
        for url in ['https://jobs.smartrecruiters.com/Beta/123', 'https://jobs.smartrecruiters.com/AcmeEvil/123']:
            self.assertFalse(scope.admit(dict(url=url, career=True, detail=True, embedded=False), ROOT))
        self.assertTrue(scope.contains('https://jobs.smartrecruiters.com/Acme/123'))

    def test_cross_domain_redirect_chain_records_sources(self):
        portal = 'https://careers.acme.example/'
        ats = 'https://acme.jobs.personio.de/'
        pages = {ROOT + '/': '<a href="/karriere">Karriere</a>',
                 ROOT + '/karriere': httpx.Response(301, headers={'location': portal}),
                 portal: httpx.Response(302, headers={'location': ats}),
                 ats: html(job(url='/job/1'))}
        result, calls = self.crawl(pages)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['jobs']), 1)
        redirects = [r for r in result['sources'] if r['mechanism'] == 'http_redirect']
        self.assertEqual([r['url'] for r in redirects], [portal, ats])
        self.assertIn(ats + 'robots.txt', calls)
        self.assertEqual(result['pages'][-1]['redirects'][-1]['target'], ats)

    def test_aggregator_redirect_is_not_followed(self):
        result, calls = self.crawl({ROOT + '/': httpx.Response(302, headers={'location': 'https://www.linkedin.com/jobs'})})
        self.assertEqual(result['status'], 'error')
        self.assertFalse(any('linkedin' in call for call in calls))

    def test_messenger_share_link_is_not_an_employer_portal(self):
        scope = CareerScope([ROOT])
        self.assertFalse(scope.admit(dict(url='https://api.whatsapp.com/send?text=Jobs',
                                         career=True, detail=True, embedded=False), ROOT))

    def test_meta_refresh_follows_the_company_portal(self):
        portal = 'https://careers.acme.example/'
        result, _ = self.crawl({ROOT + '/': f'<meta http-equiv="refresh" content="0; URL={portal}">',
                               portal: html(job(url='/jobs/1'))})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['sources'][-1]['mechanism'], 'meta_refresh')

    def test_redirect_keeps_tenant_boundary_and_remote_robots(self):
        portal = 'https://jobs.smartrecruiters.com/Acme'
        company = CompanyQuery('Acme', 'Acme', career_urls=[portal])
        result, calls = self.crawl({portal: httpx.Response(302, headers={'location': '/Beta/job'})}, company=company)
        self.assertEqual(result['status'], 'error')
        self.assertFalse(any('/Beta/job' in call for call in calls))
        result, calls = self.crawl({ROOT + '/': httpx.Response(302, headers={'location': 'https://portal.example/jobs/secret'})})
        self.assertEqual(result['status'], 'error')
        self.assertFalse(any(call.endswith('/jobs/secret') for call in calls))

    def test_cached_redirect_chain_is_authorized_for_each_fresh_scope(self):
        target = 'https://portal.example/karriere'
        calls = []
        def handler(request):
            calls.append(str(request.url))
            if request.url.path == '/robots.txt':
                return httpx.Response(200, text='User-agent: *\nDisallow:')
            if str(request.url) == ROOT + '/':
                return httpx.Response(302, headers={'location': target})
            return httpx.Response(200, text=html(job(url='/jobs/1')), headers={'content-type': 'text/html'})
        company = CompanyQuery('Acme', 'Acme', urls=[ROOT])
        with TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            http = SiteHTTP(output=Path(folder), client=client, delay=0)
            first = crawl_company(company, arguments(), http)
            count = len(calls)
            second = crawl_company(company, arguments(), http)
            self.assertEqual(len(calls), count)
            self.assertEqual(first['sources'], second['sources'])
            self.assertEqual(second['jobs'][0]['url'], 'https://portal.example/jobs/1')
            with self.assertRaises(CrawlError):
                http.get(ROOT + '/', {host_key(ROOT)})

    def test_canonical_domain_redirect_of_robots_and_page(self):
        portal = 'https://careers.acme.example'
        def handler(request):
            url = str(request.url)
            if url in (ROOT + '/', ROOT + '/robots.txt'):
                return httpx.Response(301, headers={'location': portal + request.url.path})
            if request.url.path == '/robots.txt':
                return httpx.Response(200, text='User-agent: *\nDisallow:')
            return httpx.Response(200, text=html(job()), headers={'content-type': 'text/html'})
        with TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            http = SiteHTTP(output=Path(folder), client=client, delay=0)
            result = crawl_company(CompanyQuery('Acme', 'Acme', urls=[ROOT]), arguments(), http)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(any(s['mechanism'] == 'robots_redirect' for s in result['sources']))

    def test_robots_denied_job_marks_incomplete(self):
        result, calls = self.crawl({ROOT + '/': '<a href="/jobs/secret">Entwickler (m/w/d)</a>'})
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(any(call.endswith('/jobs/secret') for call in calls))
        self.assertIn('robots.txt', result['errors'][0]['error'])

    def test_page_job_and_request_limits_are_visible(self):
        pages = {ROOT + '/': '<a href="/jobs/1">A (m/w/d)</a><a href="/jobs/2">B (m/w/d)</a>',
                 ROOT + '/jobs/1': html(job(url='/jobs/1')),
                 ROOT + '/jobs/2': html(job('B', '/jobs/2'))}
        for option, value, flag in [('career_max_pages', 1, 'page_limit_reached'),
                                    ('career_max_jobs', 1, 'job_limit_reached'),
                                    ('max_requests', 1, 'request_limit_reached')]:
            with self.subTest(option=option):
                result, _ = self.crawl(pages, **{option: value})
                self.assertTrue(result['coverage'][flag])
                self.assertFalse(result['coverage']['complete'])

    def test_many_structured_jobs_on_single_page_respect_limit(self):
        result, _ = self.crawl({ROOT + '/': html(job(url='/jobs/1'), job('B', '/jobs/2'))}, career_max_jobs=1)
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(result['status'], 'partial')

    def test_shell_does_not_mean_zero_vacancies(self):
        result, _ = self.crawl({ROOT + '/': '<div id="app"></div><script src="app.js"></script>'})
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['reason'], 'search_incomplete')

    def test_explicit_zero_and_missing_url(self):
        result, _ = self.crawl({ROOT + '/': '<p>Derzeit keine offenen Stellen.</p>'})
        self.assertEqual(result['status'], 'no_results')
        result, calls = self.crawl({}, company=CompanyQuery('Acme', 'Acme'))
        self.assertEqual(result['status'], 'skipped')
        self.assertFalse(calls)

    def test_keyword_filter_runs_after_collection(self):
        result, _ = self.crawl({ROOT + '/': html(job(), job('Analyst', '/jobs/analyst'))}, career_keyword=['ANALYST'])
        self.assertEqual([r['title'] for r in result['jobs']], ['Analyst'])
        self.assertEqual(result['jobs_before_keyword_filter'], 2)

    def test_closed_detail_removes_previously_advertised_job(self):
        pages = {ROOT + '/': html(job(url='/jobs/1'), body='<a href="/jobs/1">Entwickler (m/w/d)</a>'),
                 ROOT + '/jobs/1': '<h1>Entwickler (m/w/d)</h1><p>Diese Stelle ist bereits besetzt.</p>'}
        result, _ = self.crawl(pages)
        self.assertFalse(result['jobs'])
        self.assertEqual(result['status'], 'no_results')

    def test_untrusted_structured_job_is_not_a_complete_empty_search(self):
        result, _ = self.crawl({ROOT + '/': html(job(url='https://unknown.example/jobs/1'))})
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['coverage']['complete'])

    def test_redirect_to_visited_url_does_not_consume_extra_page(self):
        pages = {ROOT + '/': httpx.Response(302, headers={'location': '/karriere'}),
                 ROOT + '/karriere': '<a href="/jobs/1">A (m/w/d)</a>',
                 ROOT + '/jobs/1': html(job(url='/jobs/1'))}
        result, _ = self.crawl(pages, career_max_pages=2)
        self.assertEqual(len(result['jobs']), 1)


class CareerIntegrationTests(unittest.TestCase):
    def test_large_first_company_cannot_exhaust_later_company_budget(self):
        companies = [CompanyQuery('First', 'First', urls=[ROOT]),
                     CompanyQuery('Last', 'Last', urls=['https://last.example'])]
        def handler(request):
            if request.url.path == '/robots.txt':
                return httpx.Response(200, text='User-agent: *\nAllow: /')
            if request.url.host == 'acme.example' and request.url.path == '/':
                body = ''.join(f'<a href="/jobs/{i}">Engineer {i} (m/w/d)</a>' for i in range(8))
            else:
                body = html(job(url=str(request.url)))
            return httpx.Response(200, text=body, headers={'content-type': 'text/html'})
        with TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with SiteHTTP(output=Path(folder), client=client, delay=0, max_requests=8) as http:
                with patch('research_app.karriere.SiteHTTP', return_value=http), redirect_stdout(StringIO()):
                    result = collect_karriere(companies, arguments(output=Path(folder), career_max_requests=8))
                self.assertEqual(result['Last']['status'], 'ok')
                self.assertEqual(result['Last']['count'], 1)
                self.assertEqual(result['First']['coverage']['http']['requests_attempted'], 4)
                self.assertEqual(result['Last']['coverage']['http']['allocated_requests'], 4)
                self.assertLessEqual(http.requests, 8)

    def test_csv_and_excel_career_urls_survive_selection(self):
        with TemporaryDirectory() as folder:
            csv = Path(folder) / 'sources.csv'
            csv.write_text('company_key,url,career_url,enabled\nAcme,https://acme.example,https://acme.example/karriere,true\nHidden,,,false\n', encoding='utf-8')
            self.assertEqual(len(load_companies(csv)), 1)
            self.assertEqual(load_companies(csv)[0].career_urls, [ROOT + '/karriere'])
            workbook = Workbook(); sheet = workbook.active
            sheet.append(['Unternehmen', 'Branche', 'career_url'])
            sheet.append(['Acme', 'Logistik', 'https://acme.example/jobs'])
            excel = Path(folder) / 'companies.xlsx'; workbook.save(excel); workbook.close()
            args = make_parser().parse_args(['--sources', str(csv), '--companies', str(excel)])
            selected = select_companies(args)[0]
            self.assertEqual(selected.career_urls, [ROOT + '/jobs', ROOT + '/karriere'])
            self.assertEqual(selected.urls, [ROOT])

    def test_preview_command_does_not_fetch(self):
        with patch('research_app.karriere.SiteHTTP.get', side_effect=AssertionError('network')), redirect_stdout(StringIO()) as stream:
            code = main.main(['karriere', '--company', 'Acme', '--career-url', ROOT + '/jobs', '--list-career-sites'])
        self.assertEqual(code, 0)
        self.assertIn(ROOT + '/jobs', stream.getvalue())
        self.assertIn('karriere', SOURCE_GROUPS['primary'])

    def test_command_writes_combined_research_batch(self):
        def handler(request):
            if request.url.path == '/robots.txt':
                return httpx.Response(200, text='User-agent: *\nDisallow:')
            return httpx.Response(200, text=html(job()), headers={'content-type': 'text/html'})
        with TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with SiteHTTP(output=Path(folder) / 'karriere', delay=0, client=client) as http:
                with patch('research_app.karriere.SiteHTTP', return_value=http), redirect_stdout(StringIO()):
                    code = main.main(['karriere', '--company', 'Acme', '--career-url', ROOT,
                                      '--output', folder])
            self.assertEqual(code, 0)
            batch = json.loads((Path(folder) / 'research' / 'batch.json').read_text(encoding='utf-8'))
            self.assertEqual(set(batch['companies'][0]['providers']), {'karriere'})
            self.assertEqual(batch['companies'][0]['providers']['karriere']['count'], 1)

    def test_outputs_and_batch_reference_saved_jobs(self):
        company = CompanyQuery('Acme', 'Acme')
        from research_app.karriere import crawl_company as real_crawl
        skipped = real_crawl(company, arguments(), None)
        skipped['status'] = 'ok'
        skipped['jobs'] = parse_page(html(job()), ROOT)['jobs']
        with TemporaryDirectory() as folder:
            args = arguments(output=Path(folder))
            with patch('research_app.karriere.crawl_company', return_value=skipped), redirect_stdout(StringIO()):
                result = collect_karriere([company], args)
            snapshot = Path(result['Acme']['path'])
            saved = json.loads(snapshot.read_text(encoding='utf-8'))
            self.assertEqual(saved['jobs'][0]['company_key'], 'Acme')
            self.assertTrue((snapshot.parent / 'jobs' / saved['jobs'][0]['id'] / 'job.json').exists())
            self.assertTrue((Path(folder) / 'karriere' / 'README.md').exists())


if __name__ == '__main__':
    unittest.main()
