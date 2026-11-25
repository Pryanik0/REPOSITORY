"""Lokaler Chromium-Test mit abgefangenen URLs; keine Live- oder Zyte-Anfragen."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from research_app.kununu_browser import KununuBrowser, collection_page_after_login
from research_app.kununu_parser import parse_reviews
from research_app.kununu_zyte import expand_factor_actions
from research_app.browser import browser_channel
from research_app.research_inputs import CompanyQuery
from test_collection_regressions import BUTTON, NOW, ROOT, card, kununu_html, review


class BrowserCollectionTests(unittest.TestCase):
    def test_collapsed_scores_are_read_for_both_browser_backends(self):
        row = review('one')
        row['ratings'] = [dict(id='workLife', score=4, text='Balance')]
        visible = card('one').replace('<span data-score="4"></span>', '')
        visible = visible.replace('</article>', '''
            <button data-testid="review-collapsible-factors-toggle">Alle anzeigen</button>
            </article>''')
        html = kununu_html([row], visible) + '''<script>
            document.querySelector('button').onclick = event => {
                const block = [...document.querySelectorAll('h4')]
                    .find(h => h.textContent === 'Arbeitsatmosphäre').parentElement;
                block.insertAdjacentHTML('beforeend', '<span data-score="5"></span>');
                event.target.textContent = 'Weniger anzeigen';
            };
        </script>'''
        with sync_playwright() as pw, TemporaryDirectory() as folder:
            browser = pw.chromium.launch(headless=True, channel=browser_channel(pw, None))
            try:
                page = browser.new_page()
                for backend in ('browser', 'zyte'):
                    with self.subTest(backend=backend):
                        page.set_content(html)
                        self.assertNotIn('atmosphere', parse_reviews(page.content(), ROOT, 15, until=NOW)[0]['factor_scores'])
                        if backend == 'browser':
                            metadata = {}
                            collector = KununuBrowser(page, output=Path(folder))
                            collector.expand_review_factors(metadata)
                            self.assertEqual(metadata['factor_cards_expanded'], 1)
                            collector.expand_review_factors(metadata)  # Не свернуть повторно.
                        else:
                            page.evaluate(expand_factor_actions(15)[0]['source'])
                        scores = parse_reviews(page.content(), ROOT, 15, until=NOW)[0]['factor_scores']
                        self.assertEqual(scores, {'workLife': 4.0, 'atmosphere': 5.0})
            finally:
                browser.close()

    def test_session_loads_two_batches_without_reloading_or_prompting(self):
        html = kununu_html([review('one')], card('one'), BUTTON, pages=5)
        # Ein aktives Feature-Flag darf eine funktionierende Sitzung nicht abbrechen.
        html = html.replace('"initialReduxState":',
                            '"pageProps":{"query":{"featureToggles":"reviewFencing=true"}},"initialReduxState":')
        html += '''<script>
            let number = 1;
            document.getElementById('reviews-read-more-cta').onclick = () => {
                let copy = document.querySelector('article').cloneNode(true);
                copy.removeAttribute('data-research-seen');
                copy.setAttribute('data-testid', 'review-new' + (++number));
                document.querySelector('main').appendChild(copy);
            };
        </script>'''
        html += '''<aside id="usercentrics-cmp-ui"></aside><script>
            // Usercentrics rendert seine Buttons im offenen Shadow DOM.
            const host = document.getElementById('usercentrics-cmp-ui');
            host.style = 'position:fixed;inset:0;z-index:100000;background:white';
            const shadow = host.attachShadow({mode:'open'});
            shadow.innerHTML = '<button>Alle ablehnen</button><button>Alle akzeptieren</button>';
            shadow.querySelectorAll('button')[1].onclick = () => { window.acceptedAll = true; };
            shadow.querySelector('button').onclick = () => { host.remove(); };
            const overlay = document.createElement('div');
            overlay.style = 'position:fixed;inset:0;z-index:90000;background:white';
            overlay.innerHTML = '<dialog open aria-labelledby="pulse-launch-promotion-headline">'
                + '<h2 id="pulse-launch-promotion-headline">Entdecke die aktuelle Stimmung!</h2>'
                + '<button>Alles klar!</button></dialog>';
            overlay.querySelector('button').onclick = () => { overlay.remove(); };
            // Spätes Overlay muss ebenfalls ohne Benutzereingabe verschwinden.
            setTimeout(() => document.body.appendChild(overlay), 100);
        </script>'''
        navigations = []
        with sync_playwright() as pw, TemporaryDirectory() as folder:
            browser = pw.chromium.launch(headless=True, channel=browser_channel(pw, None))
            try:
                context = browser.new_context()
                page = context.new_page()
                def respond(route):
                    navigations.append(route.request.url)
                    route.fulfill(status=200, content_type='text/html', body=html)
                page.route('**/*', respond)
                collector = KununuBrowser(page, output=Path(folder), max_reviews=3)
                with patch('research_app.kununu_zyte.review_window', return_value=(NOW.replace(year=2024), NOW)), \
                        patch('builtins.input', side_effect=AssertionError('No per-company prompts')):
                    result = collector.research(CompanyQuery('Acme', 'Acme', kununu_url=ROOT))
                self.assertEqual(result['status'], 'ok')
                self.assertEqual([r['id'] for r in result['reviews']], ['one', 'new2', 'new3'])
                self.assertEqual(navigations, [ROOT+'/kommentare'])
                self.assertEqual(result['request_limits']['zyte_requests'], 0)
                self.assertTrue((Path(folder)/'kununu').exists())
                dismissed = [name for p in result['diagnostics']['pages'] for name in p.get('dismissed_overlays', [])]
                self.assertIn('cookies_rejected', dismissed)
                self.assertIn('pulse_promotion_closed', dismissed)
                self.assertFalse(page.evaluate('Boolean(window.acceptedAll)'))

                # Ein blockierter Abruf hält den nächsten Firmenabruf nicht auf.
                page.unroute('**/*', respond)
                page.route('**/*', lambda route: route.fulfill(
                    status=405, content_type='text/html', body='<title>Captcha</title><h1>Captcha</h1>'))
                with patch('builtins.input', side_effect=AssertionError('No CAPTCHA prompts')):
                    blocked = collector.research(CompanyQuery('Blocked', 'Blocked', kununu_url=ROOT))
                self.assertEqual(blocked['reason'], 'blocked')
                self.assertEqual(blocked['reviews'], [])
                self.assertTrue(collector.stop_requested)

                # Eine manuell gelöste CAPTCHA wird auf derselben Seite weiterverarbeitet.
                interactive = KununuBrowser(page, output=Path(folder)/'interactive',
                                             max_reviews=1, interactive=True)
                ready_html = kununu_html([review('one')], card('one'))
                with patch('research_app.kununu_zyte.review_window', return_value=(NOW.replace(year=2024), NOW)), \
                        patch('builtins.input', side_effect=lambda: page.set_content(ready_html)) as prompt:
                    recovered = interactive.research(CompanyQuery('Acme', 'Acme', kununu_url=ROOT))
                self.assertEqual(recovered['status'], 'ok')
                self.assertEqual(len(recovered['reviews']), 1)
                self.assertEqual(prompt.call_count, 1)
                self.assertFalse(interactive.stop_requested)
                self.assertTrue(recovered['diagnostics']['pages'][0]['challenge_resolved_manually'])
                # Die Route liefert wieder CAPTCHA: kein zweiter Prompt, globaler Stop.
                with patch('builtins.input', side_effect=AssertionError('Only one challenge pause')):
                    repeated = interactive.research(CompanyQuery('Again', 'Again', kununu_url=ROOT))
                self.assertEqual(repeated['reason'], 'blocked')
                self.assertTrue(interactive.stop_requested)

                unresolved = KununuBrowser(page, output=Path(folder)/'unresolved', interactive=True)
                with patch('builtins.input', return_value='') as prompt:
                    failed = unresolved.research(CompanyQuery('Acme', 'Acme', kununu_url=ROOT))
                self.assertEqual(failed['reason'], 'blocked')
                self.assertEqual(prompt.call_count, 1)
                self.assertTrue(unresolved.stop_requested)

                page.unroute('**/*')
                gate_html = kununu_html([review('one')], card('one'), BUTTON, pages=5)
                gate_html += '''<script>
                    document.getElementById('reviews-read-more-cta').onclick = () => {
                        let gate = document.createElement('div');
                        gate.setAttribute('data-testid', 'reviews-gate');
                        gate.textContent = 'Bitte anmelden';
                        document.body.appendChild(gate);
                    };
                </script>'''
                page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=gate_html))
                with patch('research_app.kununu_zyte.review_window', return_value=(NOW.replace(year=2024), NOW)), \
                        patch('builtins.input', side_effect=AssertionError('No login prompts')):
                    gated = collector.research(CompanyQuery('Gate', 'Gate', kununu_url=ROOT))
                self.assertEqual(gated['reason'], 'reviews_login_required')
                self.assertEqual(len(gated['reviews']), 1)

                page.unroute('**/*')
                def login_response(route):
                    body = ('<script>setTimeout(() => location.href="/de/my-home", 100)</script>'
                            if route.request.url.endswith('/login') else '<h1>Home</h1>')
                    route.fulfill(status=200, content_type='text/html', body=body)
                page.route('**/*', login_response)
                page.goto('https://www.kununu.com/de/login')
                collection_page = collection_page_after_login(page, page.context)
                self.assertTrue(page.url.endswith('/de/my-home'))
                self.assertIsNot(collection_page, page)
                self.assertEqual(collection_page.url, 'about:blank')
                collection_page.close()
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
