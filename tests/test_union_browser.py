"""Browser-Fallback, Cache und entfernte Quellen ohne externe Requests prüfen."""
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from research_app.research import SOURCE_GROUPS, make_parser
from research_app.gewerkschaften import load_gewerkschaften
from research_app.union_articles import UnionSource
from research_app.union_browser import UnionBrowserHTTP
from research_app.career_browser import CareerBrowserHTTP
from research_app.site_http import BudgetExceeded, CrawlError


URL = 'https://www.djv.de/news/article'


class BrowserTests(unittest.TestCase):
    def test_career_browser_uses_current_employer_scope_and_cache_only(self):
        with TemporaryDirectory() as folder:
            with CareerBrowserHTTP(output=folder, cache_only=True) as http, patch.object(http, '_start_browser') as browser:
                with self.assertRaises(CrawlError):
                    http.get('https://acme.example/careers', {'acme.example'})
                self.assertEqual(http.fallback_hosts, {'acme.example'})
                self.assertFalse(http.javascript_enabled)
                with self.assertRaises(CrawlError):
                    http.get(URL, {'djv.de'})
                self.assertEqual(http.fallback_hosts, {'djv.de'})
                browser.assert_not_called()

    def transport(self, folder, *, robots='User-agent: *\nAllow: /', status=500, **kwargs):
        def respond(request):
            if request.url.path == '/robots.txt':
                return httpx.Response(200, text=robots)
            return httpx.Response(status, text='Unavailable')
        return UnionBrowserHTTP(output=folder, delay=0,
                                client=httpx.Client(transport=httpx.MockTransport(respond)), **kwargs)

    def result(self, *, status=200, text='<html><title>Article</title></html>'):
        return dict(url=URL, final_url=URL, redirects=[], status=status, text=text,
                    content_type='text/html', backend='browser',
                    fetched_at=datetime.now(timezone.utc).isoformat())

    def test_success_is_cached_and_cache_only_never_opens_browser(self):
        with TemporaryDirectory() as folder:
            with self.transport(folder) as http, patch.object(http, '_browser_raw', return_value=self.result()) as browser:
                self.assertEqual(http.get(URL, {'djv.de'})['backend'], 'browser')
                browser.assert_called_once()
            with self.transport(folder, cache_only=True) as http, patch.object(http, '_start_browser') as browser:
                self.assertEqual(http.get(URL, {'djv.de'})['backend'], 'browser')
                self.assertEqual(http.requests, 0)
                browser.assert_not_called()

    def test_challenge_is_not_saved_as_success(self):
        for result in (self.result(status=403), self.result(text='<title>Just a moment...</title>')):
            with self.subTest(status=result['status']), TemporaryDirectory() as folder:
                with self.transport(folder) as http, patch.object(http, '_browser_raw', return_value=result):
                    with self.assertRaises(CrawlError):
                        http.get(URL, {'djv.de'})
                    self.assertIn('error', http.browser_attempts[0])
                with self.transport(folder, cache_only=True) as http, patch.object(http, '_start_browser') as browser:
                    with self.assertRaises(CrawlError):
                        http.get(URL, {'djv.de'})
                    browser.assert_not_called()

    def test_robots_denial_and_unrelated_hosts_never_open_browser(self):
        with TemporaryDirectory() as folder:
            with self.transport(folder, robots='User-agent: *\nDisallow: /') as http, patch.object(http, '_start_browser') as browser:
                with self.assertRaisesRegex(CrawlError, 'robots.txt'):
                    http.get(URL, {'djv.de'})
                browser.assert_not_called()
        with TemporaryDirectory() as folder:
            with self.transport(folder) as http, patch.object(http, '_start_browser') as browser:
                with self.assertRaisesRegex(CrawlError, 'HTTP 500'):
                    http.get('https://example.org/article', {'example.org'})
                browser.assert_not_called()

    def test_exhausted_budget_prevents_browser_launch(self):
        with TemporaryDirectory() as folder:
            with self.transport(folder, max_requests=2) as http, patch.object(http, '_start_browser') as browser:
                with self.assertRaises(BudgetExceeded):
                    http.get(URL, {'djv.de'})
                self.assertEqual(http.requests, 2)  # robots.txt and failed HTTP request
                browser.assert_not_called()

    def test_redirect_is_checked_before_any_request_to_target(self):
        for target in ('https://outside.example/article', 'https://www.djv.de/private'):
            with self.subTest(target=target), TemporaryDirectory() as folder:
                with self.transport(folder) as http:
                    fetched = Mock(status=302, headers={'location': target})
                    page = Mock()
                    routes = []

                    def go(url, **kwargs):
                        route = Mock(request=SimpleNamespace(method='GET', resource_type='document', url=url,
                                                             is_navigation_request=lambda: True))
                        route.fetch.return_value = fetched
                        routes.append(route)
                        page.route.call_args.args[1](route)
                        return Mock(status=200, headers={'content-type': 'text/html'})

                    page.goto.side_effect = go
                    context = Mock()
                    context.new_page.return_value = page

                    def check(url):
                        if url.endswith('/private'):
                            raise CrawlError('robots.txt denied')

                    with patch.object(http, '_start_browser', return_value=context):
                        with self.assertRaises(CrawlError):
                            http._browser_raw(URL, {'djv.de'}, check, None)
                    self.assertEqual(len(routes), 1)
                    routes[0].fetch.assert_called_once()
                    self.assertEqual(routes[0].fetch.call_args.kwargs['max_redirects'], 0)
                    page.close.assert_called_once()


class SourceConfigurationTests(unittest.TestCase):
    def test_removed_source_and_flags_are_rejected(self):
        parser = make_parser()
        for args in (['--source', 'verbaende'], ['--list-verbaende'], ['--verband', 'BDU'],
                     ['--verbaende-max-pages', '10']):
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(args)
        self.assertEqual(SOURCE_GROUPS['secondary'], ('implisense',))
        self.assertFalse(any('verbaende' in sources for sources in SOURCE_GROUPS.values()))
        args = parser.parse_args(['--gewerkschaften-max-pages', '75', '--gewerkschaften-browser-fallback'])
        self.assertEqual(args.gewerkschaften_max_pages, 75)
        self.assertTrue(args.gewerkschaften_browser_fallback)

    def test_press_seeds_preserve_explicit_news_urls(self):
        for host in ('gew.de', 'djv.de'):
            for custom in ([], [f'https://www.{host}/custom']):
                source = UnionSource(host, urls=[f'https://www.{host}/'], news_urls=custom[:])
                with patch('research_app.gewerkschaften.load_union_sources', return_value=[source]):
                    result = load_gewerkschaften(Path('unused.xlsx'))[0]
                self.assertEqual(result.news_urls, custom or [
                    'https://www.gew.de/presse' if host == 'gew.de' else 'https://www.djv.de/news/pressemitteilungen/'])


if __name__ == '__main__':
    unittest.main()
