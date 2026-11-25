"""Regressionen aus dem Sammellauf vom 10.11.2026, ohne Netzwerkanfragen."""
import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from research_app.branches import normalize_branches
from research_app.normalize_branches import normalize_file
from research_app.research_inputs import CompanyQuery
from research_app.union_articles import UnionSource, crawl_union, discover_links, discover_searches, make_plan
from research_app.kununu_parser import next_review_page, parse_reviews, review_ids, reviews_require_login
from research_app.kununu_zyte import KununuZyte
from research_app.kununu_storage import save_snapshot
from research_app.register_jobs import run_company
from research_app.register_documents import publication_document
from research_app.unternehmensregister import FinancialReport, UnternehmensregisterClient

NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)
ROOT = 'https://www.kununu.com/de/acme'


def review(uid, kind='employer'):
    return dict(uuid=uid, type=kind, title=uid, score=4,
                publishedAt='2026-09-01T00:00:00Z', texts=[dict(id='positive', text='Gute Zusammenarbeit')],
                ratings=[dict(id='atmosphere', score=4)])


def card(uid, role='Angestellte/r', stamp='2026-09-01T00:00:00Z'):
    return f'''<article data-testid="review-{uid}"><time datetime="{stamp}"></time>
        <h3>{uid}</h3><span data-testid="review-rating">4,0</span>
        <div class="employmentInfoBlock"><b>{role}</b></div>
        <div><h4>Gut am Arbeitgeber finde ich</h4><p>Gute Zusammenarbeit</p></div>
        <div><h4>Arbeitsatmosphäre</h4><span data-score="4"></span></div></article>'''


def kununu_html(rows=(), visible='', navigation='', page=1, pages=1):
    state = dict(props=dict(initialReduxState=dict(profile=dict(countryCode='de', slug='acme'),
                                                  reviews=dict(reviews=list(rows), page=page, pagesCount=pages))))
    return f'''<html><head><link rel="canonical" href="{ROOT}"></head><body><main>
        <h1>Acme als Arbeitgeber</h1><p>4,0 kununu Score 20 Bewertungen</p>
        {visible}{navigation}</main><script id="__NEXT_DATA__" type="application/json">
        {json.dumps(state)}</script></body></html>'''


BUTTON = '<button id="reviews-read-more-cta">Mehr Bewertungen lesen</button>'


class BranchTests(unittest.TestCase):
    def test_compound_branches_match_both_sectors(self):
        companies = [CompanyQuery('a', 'a', branches=['Logistik']),
                     CompanyQuery('b', 'b', branches=['Abfallwirtschaft'])]
        plan = make_plan(companies, [UnionSource('Example Union', ['Transport, Logistik und Entsorgung'])])
        self.assertEqual(plan[0]['company_keys'], ['a', 'b'])

    def test_social_and_it_aliases(self):
        pairs = [('Sozialwesen', 'Pflege und Gesundheitswesen'),
                 ('IT & Telekommunikation', 'Digitale Wirtschaft und IT'),
                 ('Bildung & Wissenschaft', 'Bildung und Erziehung')]
        for company_branch, source_branch in pairs:
            self.assertTrue(make_plan([CompanyQuery('a', 'a', branches=[company_branch])],
                                      [UnionSource('V', [source_branch])]))

    def test_unknown_branches_are_not_guessed(self):
        self.assertEqual(normalize_branches('Unbekannt, Spezial; Logistik | LOGISTIK'),
                         ['Unbekannt, Spezial', 'Logistik'])
        self.assertFalse(make_plan([CompanyQuery('a', 'a', branches=['Rechtsberatung'])],
                                   [UnionSource('V', ['Branchenübergreifend'])]))

    def test_normalization_is_idempotent_and_preserves_other_cells(self):
        with TemporaryDirectory() as folder:
            p = Path(folder) / 'input.xlsx'
            b = Workbook(); s = b.active
            s.append(['Gewerkschaft', 'Branche', 'Website']); s.append(['Example Union', 'Transport, Logistik und Entsorgung', 'https://example.org'])
            s['A2'].font = Font(bold=True)
            b.save(p); b.close()
            original = p.read_bytes()
            self.assertEqual(len(normalize_file(p, write=True)), 1)
            self.assertEqual(normalize_file(p, write=True), [])
            b = load_workbook(p)
            self.assertEqual(b.active['B2'].value, 'Logistik; Abfallwirtschaft')
            self.assertEqual(b.active['C2'].value, 'https://example.org')
            self.assertTrue(b.active['A2'].font.bold); b.close()
            self.assertEqual(next((Path(folder)/'backups').rglob('*.xlsx')).read_bytes(), original)

    def test_csv_changes_only_branch(self):
        with TemporaryDirectory() as folder:
            p = Path(folder)/'sources.csv'
            p.write_text('company_key,url,Branche\n"Acme, Inc",https://example.org,Sozialwesen\n', encoding='utf-8')
            normalize_file(p, write=True)
            with p.open(encoding='utf-8') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row, dict(company_key='Acme, Inc', url='https://example.org', Branche='Gesundheit & Sozialwesen'))


class KununuTests(unittest.TestCase):
    def test_failed_refresh_preserves_previous_snapshot_without_mixing_reviews(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'snapshot.json'
            save_snapshot(path, dict(status='ok', reviews=[review('one')]))
            previous = path.read_bytes()
            save_snapshot(path, dict(status='error', reason='blocked', reviews=[]))
            current = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(current['reviews'], [])
            self.assertEqual(Path(current['previous_snapshot_path']).read_bytes(), previous)

    def test_profile_only_still_fetches_profile(self):
        result, calls = self.run_client(lambda data: dict(url=data['url'], statusCode=200,
                                                         browserHtml=kununu_html()), limit=0)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([call['url'] for call in calls], [ROOT])

    def test_reviews_page_supplies_profile_in_one_request(self):
        result, calls = self.run_client(lambda data: dict(url=data['url'], statusCode=200,
                                                         browserHtml=kununu_html([review('one')])), limit=1)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['profile']['overall_score'], 4.0)
        self.assertEqual(result['profile_source'], ROOT+'/kommentare')
        self.assertEqual(len(calls), 1)

    def test_missing_profile_metrics_is_not_reported_as_ok(self):
        html = kununu_html([review('one')]).replace('4,0 kununu Score 20 Bewertungen', '')
        result, calls = self.run_client(lambda data: dict(url=data['url'], statusCode=200, browserHtml=html), limit=1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['reason'], 'unusable_profile')
        self.assertEqual(len(result['reviews']), 1)
        self.assertEqual(len(calls), 1)

    def test_fenced_button_stops_without_paid_continuation(self):
        html = kununu_html([review('one')], navigation=BUTTON, pages=5)
        html = html.replace('"initialReduxState":',
                            '"pageProps": {"query": {"featureToggles": "reviewFencing=true"}}, "initialReduxState":')
        self.assertFalse(reviews_require_login(html))
        self.assertFalse(reviews_require_login(html.replace('reviewFencing=true', 'reviewFencing=false')))
        self.assertFalse(reviews_require_login(html.replace(BUTTON, '<a id="reviews-read-more-cta" href="/de/acme/kommentare/2">Mehr Bewertungen lesen</a>')))
        html += '<div data-testid="reviews-gate">Bitte anmelden</div>'
        result, calls = self.run_client(lambda data: dict(url=data['url'], statusCode=200, browserHtml=html))
        self.assertEqual(result['reason'], 'reviews_login_required')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['reviews']), 1)
        self.assertEqual(len(calls), 1)

    def test_login_gate_excludes_fake_review(self):
        html = kununu_html([review('one')],
                           '<div class="_reviewsGateBlurContainer_123">'+card('fake')+'</div>'
                           '<div data-testid="reviews-gate"><a id="reviews-gate-cta-button">Login</a></div>')
        self.assertTrue(reviews_require_login(html))
        self.assertEqual(review_ids(html, ROOT), {'one'})
        self.assertEqual([r['id'] for r in parse_reviews(html, ROOT, 15, until=NOW)], ['one'])

    def test_no_guessed_pagination(self):
        self.assertIsNone(next_review_page(kununu_html(pages=5, navigation=BUTTON), ROOT))
        html = kununu_html(navigation='<a href="/de/acme/kommentare/2">Mehr Bewertungen lesen</a>')
        self.assertEqual(next_review_page(html, ROOT), ROOT+'/kommentare/2')

    def test_merge_hydrated_cards_excludes_apprentices_and_ads(self):
        html = kununu_html([review('old'), review('trainee', 'apprenticeship')],
                           card('old')+card('new')+card('trainee')+card('new-trainee', 'Auszubildende:r')+
                           '<article data-testid="review-ad"><h3>Advertisement</h3></article>')
        rows = parse_reviews(html, ROOT, 15, until=NOW)
        self.assertEqual([r['id'] for r in rows], ['old', 'new'])
        self.assertEqual(rows[1]['factor_scores']['atmosphere'], 4)

    def run_client(self, reply, limit=3):
        calls = []
        def handler(request):
            data = json.loads(request.content); calls.append(data)
            return httpx.Response(200, json=reply(data))
        with TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('research_app.kununu_zyte.review_window', return_value=(NOW.replace(year=2024), NOW)):
            scraper = KununuZyte(api_key='test-key', max_requests=10, max_reviews=limit,
                                 client=client, output=Path(folder))
            result = scraper.research(CompanyQuery('Acme', 'Acme', kununu_url=ROOT))
        return result, calls

    def test_load_more_merges_live_dom_without_guessing_url(self):
        def reply(data):
            visible = card('three') if any(a['action'] == 'click' for a in data.get('actions', [])) else ''
            return dict(url=data['url'], statusCode=200,
                        browserHtml=kununu_html([review('one'), review('two')], visible, BUTTON, pages=5),
                        actions=[dict(status='success') for _ in data.get('actions', [])])
        result, calls = self.run_client(reply)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['reviews']), 3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]['url'], ROOT+'/kommentare')
        self.assertEqual(calls[1]['url'], ROOT+'/kommentare')
        self.assertEqual([a['action'] for a in calls[1]['actions']],
                         ['waitForSelector', 'evaluate', 'click', 'waitForSelector', 'evaluate'])
        self.assertIn('review-collapsible-factors-toggle', calls[0]['actions'][-1]['source'])

    def test_repeated_cards_stop_after_one_continuation(self):
        def reply(data):
            return dict(url=ROOT+'/kommentare', statusCode=200,
                        browserHtml=kununu_html([review('one')], navigation=BUTTON, pages=5),
                        actions=[dict(status='success') for _ in data.get('actions', [])])
        result, calls = self.run_client(reply)
        self.assertEqual(result['reason'], 'review_navigation_no_progress')
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(result['reviews']), 1)

    def test_404_and_foreign_redirect_do_not_fetch_reviews(self):
        for status, url in [(404, ROOT), (405, ROOT), (200, 'https://www.kununu.com/de/someone-else')]:
            result, calls = self.run_client(lambda data: dict(url=url, statusCode=status, browserHtml=kununu_html()))
            self.assertEqual(result['status'], 'error')
            self.assertEqual(len(calls), 1)

    def test_action_failure_is_not_reported_as_complete(self):
        def reply(data):
            return dict(url=data['url'], statusCode=200,
                        browserHtml=kununu_html([review('one')], card('two') if any(
                            a['action'] == 'click' for a in data.get('actions', [])) else '', BUTTON),
                        actions=[dict(status='returned', error='timeout')])
        result, _ = self.run_client(reply)
        self.assertEqual(result['reason'], 'review_actions_incomplete')
        self.assertEqual(len(result['reviews']), 2)


class FakeTransport:
    def __init__(self):
        self.requests = 0
        self.robots = {}
        self.urls = []

    def get(self, url, hosts, **kwargs):
        self.requests += 1; self.urls.append(url)
        parts = urlsplit(url)
        if parts.path == '/':
            content = '<html><main><form action="/suche"><input name="q"></form></main></html>'
        elif parts.path == '/suche':
            name = parse_qs(parts.query)['q'][0]
            content = f'<html><main><p><a href="/news/{name}">{name} meldet wichtige Neuigkeiten</a></p></main></html>'
        else:
            name = parts.path.rsplit('/', 1)[-1]
            content = f'''<html><head><meta property="og:type" content="article">
                <meta property="article:published_time" content="2026-10-01"></head>
                <body><main><article><h1>Neuigkeiten von {name}</h1>
                <p>{(name+' berichtet über neue Entwicklungen und Investitionen am Standort. ')*12}</p>
                </article></main></body></html>'''
        return dict(url=url, final_url=url, status=200, content_type='text/html',
                    fetched_at=NOW.isoformat(), text=content)


class CrawlerTests(unittest.TestCase):
    def test_igmetall_body_is_not_discarded_as_teaser_or_mixed_with_contact(self):
        from research_app.union_articles import extract_article
        html = '''<html><head><meta property="og:type" content="article">
            <meta property="article:published_time" content="2026-09-03"></head><body>
            <h1>Volkswagen: gemeinsame Erklärung</h1>
            <section class="contentBlock"><div class="teaserGrid-list__slide"><p>'''
        body = 'Volkswagen: Die Beschäftigten verlangen verlässliche Zusagen für die Zukunft ihrer Standorte. ' * 12
        html += body + '</p></div></section><section class="contact"><p>Artur Siemens Pressesprecher</p></section></body></html>'
        with patch('research_app.union_articles.trafilatura.extract', return_value='Nur eine kurze Einleitung'):
            article, reason = extract_article(html, 'https://www.igmetall.de/presse/pressemitteilungen/2026/test',
                                             start=NOW.replace(month=5).date(), end=NOW.date())
        self.assertIsNone(reason)
        self.assertIn('Volkswagen', article['text'])
        self.assertNotIn('Siemens', article['text'])

    def test_feed_mode_skips_search_forms_but_collects_company_articles(self):
        transport = FakeTransport()
        source = UnionSource('IG Metall', urls=['https://example.org'],
                         news_urls=['https://example.org', 'https://example.org/news/Acme'], site_search=False)
        with TemporaryDirectory() as folder:
            result = crawl_union(transport, source, Path(folder), self.args(),
                                   companies=[CompanyQuery('Acme', 'Acme')], today=NOW.date())
        self.assertEqual(result['search_mode'], 'feeds_and_links')
        self.assertEqual(result['searches'], [])
        self.assertFalse(any('/suche?' in url for url in transport.urls))
        self.assertEqual(len(result['articles']), 1)

    def test_igmetall_uses_official_feeds_and_keeps_custom_news_url(self):
        from research_app.gewerkschaften import load_gewerkschaften, IGMETALL_FEEDS
        for custom in ([], ['https://www.igmetall.de/custom-news']):
            source = UnionSource('IG Metall', urls=['https://www.igmetall.de'], news_urls=custom[:])
            with patch('research_app.gewerkschaften.load_union_sources', return_value=[source]):
                result = load_gewerkschaften(Path('unused.xlsx'))[0]
            self.assertFalse(result.site_search)
            if custom:
                self.assertEqual(result.news_urls, custom)
            else:
                self.assertEqual(result.news_urls[:2], IGMETALL_FEEDS)

    def args(self, **overrides):
        return SimpleNamespace(**(dict(article_months=6, article_keyword=[], gewerkschaften_max_pages=3,
                                      gewerkschaften_max_articles=10, gewerkschaften_max_search_pages=None) | overrides))

    def test_18_searches_do_not_consume_article_budget(self):
        transport = FakeTransport()
        companies = [CompanyQuery(f'Company{i:02}', f'Company{i:02}') for i in range(18)]
        with TemporaryDirectory() as folder:
            result = crawl_union(transport, UnionSource('V', urls=['https://example.org']), Path(folder),
                                   self.args(), companies=companies, today=NOW.date())
        self.assertEqual(len(result['searches']), 18)
        self.assertTrue(all(s['status']=='fetched' for s in result['searches']))
        self.assertEqual(result['page_coverage']['content_pages'], 3)
        self.assertEqual(len(result['articles']), 2)
        self.assertNotEqual(result['articles'][0]['company_matches'], result['articles'][1]['company_matches'])

    def test_search_limit_leaves_explicit_unattempted_queries(self):
        with TemporaryDirectory() as folder:
            result = crawl_union(FakeTransport(), UnionSource('V', urls=['https://example.org']), Path(folder),
                                   self.args(gewerkschaften_max_search_pages=2),
                                   companies=[CompanyQuery(f'C{i}', f'C{i}') for i in range(4)], today=NOW.date())
        self.assertEqual(sum(s['status']=='not_attempted' for s in result['searches']), 2)
        self.assertTrue(result['truncated'])

    def test_excludes_local_office_search_form(self):
        html = '<form action="/geschaeftsstellensuche"><input name="searchTerm"></form>'
        html += '<form action="/suche"><input name="q"></form>'
        result = discover_searches(html, 'https://example.org', {'example.org'}, [CompanyQuery('Acme','Acme')])
        self.assertEqual(len(result), 1)
        self.assertIn('/suche?', result[0]['url'])

    def test_pagination_keeps_company_query_and_skips_empty_search_archive(self):
        html = '<a rel="next" href="/suche?page=2">Weiter</a>'
        for url in ('https://example.org/suche', 'https://example.org/suche?q=Acme'):
            self.assertEqual(discover_links(html, url, {'example.org'}, search_results=True), [])
        html = '<a rel="next" href="/suche?page=2&amp;q=Acme">Weiter</a>'
        links = discover_links(html, 'https://example.org/suche?q=Acme', {'example.org'}, search_results=True)
        self.assertEqual(len(links), 1)

    def test_pagination_waits_until_first_article_budget_has_been_used(self):
        class PagedTransport(FakeTransport):
            def get(self, url, hosts, **kwargs):
                result = super().get(url, hosts, **kwargs)
                if '/suche?' in url and 'page=' not in url:
                    name = parse_qs(urlsplit(url).query)['q'][0]
                    result['text'] += f'<a rel="next" href="/suche?q={name}&amp;page=2">Weiter</a>'
                return result
        transport = PagedTransport()
        with TemporaryDirectory() as folder:
            crawl_union(transport, UnionSource('V', urls=['https://example.org']), Path(folder), self.args(),
                          companies=[CompanyQuery('Acme','Acme'),CompanyQuery('Beta','Beta')], today=NOW.date())
        first_page_two = next(i for i,url in enumerate(transport.urls) if 'page=2' in url)
        self.assertGreater(first_page_two, max(i for i,url in enumerate(transport.urls) if '/news/' in url))


class RegisterTests(unittest.TestCase):
    def run_register(self, count=1, more=False, **options):
        class Client:
            page = SimpleNamespace(url='https://example.org/search')
            pages_read = 1
            has_more = more
            def search(self, *args, **kwargs):
                return [FinancialReport('Acme', f'Jahresabschluss {i}', '2026-09-01', 'de',
                                        f'https://example.org/{i}', '') for i in range(count)]
            def download(self, report, output):
                return dict(status='downloaded', files=[], documents=[dict(format='html', text='Valid report', tables=[])])
        args = SimpleNamespace(**(dict(category='both', years=2, max_pages=1, max_publications=30,
                                       open=None, download=True, report_index=None, max_reports=3) | options))
        with TemporaryDirectory() as folder:
            return run_company(Client(), CompanyQuery('Acme', 'Acme'), Path(folder), args)

    def test_more_pages_cannot_be_complete(self):
        result = self.run_register(more=True)
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['coverage']['search_complete'])

    def test_download_limit_is_explicit(self):
        result = self.run_register(count=5)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['coverage']['downloads_succeeded'], 3)
        self.assertTrue(result['coverage']['download_limit_reached'])

    def test_empty_search_is_not_a_download_failure(self):
        self.assertEqual(self.run_register(count=0)['status'], 'no_results')
        self.assertEqual(self.run_register(count=0, more=True)['status'], 'partial')

    def test_short_official_publication_is_valid(self):
        doc = publication_document('<div class="publicationcontent"><p>Die Gesellschaft ist aufgelöst.</p></div>')
        self.assertIn('aufgelöst', doc['text'])
        with self.assertRaises(ValueError):
            publication_document('<main>Navigation Drucken Laden</main>')

    def test_invalid_html_is_saved_before_validation(self):
        client = UnternehmensregisterClient.__new__(UnternehmensregisterClient)
        page = MagicMock(); client.page = page
        page.get_by_text.return_value.count.return_value = 0
        page.get_by_role.return_value.count.return_value = 0
        page.locator.return_value.inner_text.return_value = ''
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.inner_html.return_value = '<main>Unvollständig</main>'
        with TemporaryDirectory() as folder:
            result = client.download(SimpleNamespace(url='https://example.org'), Path(folder))
            self.assertEqual(result['status'], 'error')
            self.assertEqual(Path(result['diagnostic_html_path']).read_text('utf-8'), '<main>Unvollständig</main>')


if __name__ == '__main__':
    unittest.main()
