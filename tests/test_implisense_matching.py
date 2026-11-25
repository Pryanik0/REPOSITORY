"""Implisense-Listenabgleich mit Adressdaten und lokaler Prüfliste."""
from pathlib import Path
import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from research_app.implisense_browser import candidate_reason, import_sources, quote_research, refresh_news


class ImplisenseMatchingTests(unittest.TestCase):
    def test_preverified_id_distinguishes_the_same_company_at_another_office(self):
        row = {'Firmenname': 'Acme GmbH', 'Ort': 'Berlin', 'PLZ': '10117', 'implisense_id': 'DE123'}
        candidate = dict(id='DE123', name='Acme GmbH', city='Dresden', zip='01099', score=0.82)
        self.assertEqual(candidate_reason(row, candidate), 'verified_implisense_id')
        self.assertEqual(candidate_reason(row, dict(candidate, id='DE456')), 'different_implisense_id')

    def test_resolve_sends_address_and_rejects_conflicting_identity(self):
        with TemporaryDirectory() as folder:
            output = Path(folder)
            source = output / 'sources.csv'
            source.write_text(
                'company_key,company_name,company_location,company_postal_code,url\n'
                'Acme,Acme GmbH,Dresden,01099,https://acme.example/\n', encoding='utf-8')
            args = SimpleNamespace(sources=source, output=output, company_key='Acme', list_name='Example')
            candidate = dict(id='123', name='Acme GmbH', city='Dresden', zip='01097', score=0.99)
            with patch('research_app.implisense_browser.site_json', return_value={'results': [candidate]}) as request:
                review = import_sources(None, None, args, [], resolve_only=True)
            request.assert_called_once_with(None, None, 'POST', '/api/lists/upload/resolve', {'items': [
                {'name': 'Acme GmbH', 'city': 'Dresden', 'zip': '01099', 'url': 'https://acme.example/'},
            ]})
            self.assertEqual(review[0]['company_key'], 'Acme')
            self.assertFalse(review[0]['selected'])
            self.assertEqual(review[0]['reason'], 'different_postal_code')


class ImplisenseResearchTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.args = SimpleNamespace(output=Path(self.folder.name), period=90, max_implicents=3,
                                    list_id='c9e4dedf-afcb-4e10-957f-0afa22e94eb8')
        self.page = Mock()
        self.page.url = 'https://app.implisense.com/de/lists/' + self.args.list_id
        self.button = self.page.locator.return_value.get_by_role.return_value
        self.page.locator.return_value.is_visible.return_value = True
        self.guard = Mock()

    def test_login_redirect_stops_before_price_or_paid_request(self):
        self.page.url = 'https://app.implisense.com/de/sign-in?redirect_url=private'
        with self.assertRaisesRegex(RuntimeError, 'implisense login'):
            refresh_news(self.page, self.guard, self.args)
        self.guard.approve_refresh.assert_not_called()
        self.page.evaluate.assert_not_called()
        self.button.wait_for.assert_not_called()

    def test_redirect_while_waiting_also_reports_login(self):
        from playwright.sync_api import TimeoutError

        def redirect():
            self.page.url = 'https://app.implisense.com/en/sign-in'
            raise TimeoutError('button missing')

        self.button.wait_for.side_effect = redirect
        with self.assertRaisesRegex(RuntimeError, 'implisense login'):
            refresh_news(self.page, self.guard, self.args)
        self.guard.approve_refresh.assert_not_called()
        self.page.evaluate.assert_not_called()

    def test_missing_button_records_diagnostics_without_starting_research(self):
        from playwright.sync_api import TimeoutError
        self.button.wait_for.side_effect = TimeoutError('button missing')
        self.page.locator.return_value.count.return_value = 0
        self.page.locator.return_value.all_inner_texts.return_value = ['Neuigkeiten']
        with self.assertRaisesRegex(RuntimeError, 'research-page-status.json'):
            refresh_news(self.page, self.guard, self.args)
        data = json.loads((self.args.output / 'research-page-status.json').read_text(encoding='utf-8'))
        self.assertEqual(data['news_panel_count'], 0)
        self.assertFalse(data['research_started'])
        self.guard.approve_refresh.assert_not_called()
        self.page.evaluate.assert_not_called()

    def test_quote_reads_price_without_paid_request(self):
        self.button.inner_text.return_value = 'Neuigkeiten recherchieren 3 Implicents'
        quote = quote_research(self.page, self.args)
        self.assertEqual(quote['maximum_implicents'], 3)
        self.page.evaluate.assert_not_called()

    def test_hidden_news_panel_is_opened_without_clicking_paid_button(self):
        self.page.locator.return_value.is_visible.return_value = False
        observe, news = Mock(), Mock()
        observe.get_attribute.return_value = 'false'
        self.page.get_by_role.side_effect = [observe, news]
        self.button.inner_text.return_value = 'Neuigkeiten recherchieren\n3'
        quote = quote_research(self.page, self.args)
        observe.click.assert_called_once_with()
        news.click.assert_called_once_with()
        self.button.click.assert_not_called()
        self.assertEqual(quote['maximum_implicents'], 3)
        self.page.evaluate.assert_not_called()

    def test_open_menu_is_not_closed_before_selecting_news(self):
        self.page.locator.return_value.is_visible.return_value = False
        observe, news = Mock(), Mock()
        observe.get_attribute.return_value = 'true'
        self.page.get_by_role.side_effect = [observe, news]
        self.button.inner_text.return_value = 'Neuigkeiten recherchieren\n3'
        quote_research(self.page, self.args)
        observe.click.assert_not_called()
        news.click.assert_called_once_with()

    def test_unreadable_price_still_blocks_paid_request(self):
        self.button.inner_text.return_value = 'Neuigkeiten recherchieren'
        with self.assertRaisesRegex(RuntimeError, 'Kein eindeutiger Nachrichtenpreis'):
            refresh_news(self.page, self.guard, self.args)
        self.guard.approve_refresh.assert_not_called()
        self.page.evaluate.assert_not_called()

    def test_server_ai_timeout_preserves_response_and_never_retries(self):
        response = {'status': 502, 'data': {'error': 'ai_timeout'}}
        self.page.evaluate.return_value = response
        with patch('research_app.implisense_browser.quote_research',
                   return_value={'maximum_implicents': 3}), \
                patch('research_app.implisense_browser.collect_news') as collect:
            with self.assertRaisesRegex(RuntimeError, r'HTTP 502.*ai_timeout.*Implisense'):
                refresh_news(self.page, self.guard, self.args)
        self.page.evaluate.assert_called_once()
        collect.assert_not_called()
        saved = json.loads((self.args.output / 'research-result.json').read_text(encoding='utf-8'))
        self.assertEqual(saved, response)
