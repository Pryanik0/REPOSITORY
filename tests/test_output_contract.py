"""Liefervertrag: fehlende Fakten, Datei-Verknüpfungen und verlustfreie Migration."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research_app.kununu_storage import restore_factor_scores, save_snapshot
from research_app.output_contract import (ContractError, FACTORS, SECTIONS, check_links,
                                         migrate, prepare, validate, write_contract_json)
from research_app.research_inputs import CompanyQuery
from research_app.register_jobs import run_company
from research_app.unternehmensregister import FinancialReport
from types import SimpleNamespace


STAMP = '2026-11-10T12:00:00+00:00'
UID = '8035b05b-8baa-46ae-a603-6cd3408a60f7'
URL = 'https://www.kununu.com/de/acme'


def profile():
    return dict(url=URL, name='Acme', overall_score=3.5, review_count=20,
                recommendation_percent=50, recommendation_period='last_2_years', observed_at=STAMP)


def review():
    return dict(id=UID, url=URL + '/bewertung/' + UID, published_at='2026-09-01', rating=3,
                recommended=False, review_type='employee', factor_scores={'leadership': 2}, sections={})


def snapshot():
    return dict(status='ok', profile=profile(), reviews=[review()], review_period=None,
                coverage='bounded_employee_review_sample_last_2_years_not_all_reviews')


def publications():
    return dict(company=dict(key='Acme', name='Acme'), date_from='2024-01-01', date_to='2026-11-10',
                searched_at=STAMP, truncated=False,
                publications=[dict(category='finanzberichte', index=1, title='Jahresabschluss',
                                   company='Acme', publication_date='2026-09-01',
                                   url='https://example.org/report', language='Deutsch')])


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


class KununuContractTests(unittest.TestCase):
    def test_sparse_data_fills_nulls_without_changing_facts_or_input(self):
        original = snapshot()
        before = deepcopy(original)
        actual = prepare('kununu', original)
        validate('kununu', actual)
        self.assertEqual(original, before)
        self.assertIsNone(actual['reason'])
        row = actual['reviews'][0]
        self.assertIs(row['recommended'], False)
        self.assertEqual(row['id'], UID)
        self.assertEqual(row['rating'], 3)
        self.assertEqual(set(row['factor_scores']), set(FACTORS))
        self.assertEqual(row['factor_scores']['leadership'], 2)
        self.assertIsNone(row['factor_scores']['atmosphere'])
        self.assertEqual(row['sections'], dict.fromkeys(SECTIONS))
        self.assertEqual(actual, prepare('kununu', actual))

    def test_missing_recommendation_is_not_replaced_with_false(self):
        original = snapshot()
        original['reviews'][0]['recommended'] = None
        actual = prepare('kununu', original)
        self.assertEqual(actual['reviews'], [])
        self.assertEqual(actual['status'], 'partial')
        self.assertEqual(actual['reason'], 'output_contract_incomplete')
        self.assertEqual(actual['contract_rejections'][0]['value'], original['reviews'][0])
        self.assertEqual(actual['factor_scores_aggregated'], {})
        self.assertEqual(actual['factor_score_coverage']['status'], 'no_reviews')
        self.assertEqual(actual, prepare('kununu', actual))

    def test_invalid_fact_rejects_review_but_preserves_original(self):
        for key, value in [('id', ''), ('rating', 0), ('rating', True),
                           ('recommended', 'false'), ('review_type', None),
                           ('published_at', '2026-02-30'), ('factor_scores', {'leadership': 6})]:
            with self.subTest(key=key, value=value):
                original = snapshot()
                original['reviews'][0][key] = value
                result = prepare('kununu', original)
                self.assertEqual(result['reviews'], [])
                self.assertEqual(result['contract_rejections'][0]['value'], original['reviews'][0])

    def test_invalid_profile_is_preserved_and_valid_reviews_stay(self):
        original = snapshot()
        original['profile']['overall_score'] = None
        actual = prepare('kununu', original)
        self.assertIsNone(actual['profile'])
        self.assertEqual(len(actual['reviews']), 1)
        self.assertEqual(actual['status'], 'partial')
        self.assertEqual(actual['contract_rejections'][0]['value'], original['profile'])

    def test_no_usable_content_is_error_not_successful_empty_result(self):
        original = snapshot()
        original['profile'] = None
        del original['reviews'][0]['recommended']
        actual = prepare('kununu', original)
        self.assertEqual(actual['status'], 'error')
        self.assertEqual(len(actual['contract_rejections']), 1)

    def test_open_factor_section_id_and_type_are_preserved_as_schema_requires(self):
        original = snapshot()
        original['reviews'][0].update(id='future-id', review_type='future-type')
        original['reviews'][0]['factor_scores']['new_factor'] = 4
        original['reviews'][0]['sections']['new_section'] = 'Original text'
        actual = prepare('kununu', original)['reviews'][0]
        self.assertEqual(actual['factor_scores']['new_factor'], 4)
        self.assertEqual(actual['sections']['new_section'], 'Original text')
        self.assertEqual(actual['id'], 'future-id')
        self.assertEqual(actual['review_type'], 'future-type')

    def test_duplicate_ids_are_not_double_counted(self):
        original = snapshot()
        original['reviews'].append(deepcopy(original['reviews'][0]))
        actual = prepare('kununu', original)
        self.assertEqual(len(actual['reviews']), 1)
        self.assertEqual(len(actual['contract_rejections']), 1)
        self.assertEqual(actual['factor_scores_aggregated']['leadership']['count'], 1)

    def test_null_factors_do_not_count_as_scores(self):
        original = snapshot()
        original['reviews'][0]['factor_scores'] = {}
        actual = prepare('kununu', original)
        self.assertEqual(actual['factor_score_coverage']['reviews_with_scores'], 0)
        self.assertEqual(actual['factor_score_coverage']['reviews_without_scores'], 1)

    def test_error_branch_reads_only_status_and_reason(self):
        actual = prepare('kununu', dict(status='error', error='Transport failed', reviews='raw diagnostic'))
        self.assertEqual(actual['reason'], 'scrape_error')
        self.assertEqual(actual['reviews'], 'raw diagnostic')
        validate('kununu', actual)
        validate('kununu', dict(status='error', reason=None))
        with self.assertRaises(ContractError):
            validate('kununu', dict(status='error'))

    def test_storage_returns_delivered_shape_and_preserves_previous_bytes(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'snapshot.json'
            put(path, snapshot())
            old = path.read_bytes()
            result = save_snapshot(path, snapshot())
            self.assertEqual(result, json.loads(path.read_text(encoding='utf-8')))
            self.assertEqual(Path(result['previous_snapshot_path']).read_bytes(), old)
            validate('kununu', result)

    def test_restore_missing_scores_only_for_unchanged_review_with_provenance(self):
        old = snapshot()
        old['reviews'][0].update(title='Original title', content='Original text')
        old['reviews'][0]['factor_scores'] = {'leadership': 5, 'workLife': 4}
        current = deepcopy(old)
        current['reviews'][0]['factor_scores'] = {'leadership': None, 'workLife': 4}
        result = restore_factor_scores(current, old, 'history/original.json')
        self.assertEqual(result['reviews'][0]['factor_scores']['leadership'], 5)
        self.assertEqual(result['reviews'][0]['factor_score_sources']['leadership'],
                         dict(snapshot='history/original.json', observed_at=STAMP))
        self.assertIsNone(current['reviews'][0]['factor_scores']['leadership'])
        for key, value in [('content', 'Edited'), ('rating', 2), ('id', 'another-review'),
                           ('factor_scores', {'workLife': 2})]:
            edited = deepcopy(current)
            edited['reviews'][0][key] = value
            self.assertEqual(restore_factor_scores(edited, old, 'history.json'), edited)
        other_company = deepcopy(current)
        other_company['profile']['url'] = 'https://www.kununu.com/de/another'
        self.assertEqual(restore_factor_scores(other_company, old, 'history.json'), other_company)
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'snapshot.json'
            put(path, old)
            delivered = save_snapshot(path, current)
            self.assertEqual(delivered['reviews'][0]['factor_scores']['leadership'], 5)
            source = delivered['reviews'][0]['factor_score_sources']['leadership']['snapshot']
            self.assertEqual(json.loads(Path(source).read_text()), old)

    def test_two_of_thirteen_scores_are_not_complete_coverage(self):
        result = prepare('kununu', snapshot())
        self.assertEqual(result['factor_score_coverage']['status'], 'partial')
        self.assertEqual(result['factor_score_coverage']['reviews_with_all_known_scores'], 0)


class OtherContractTests(unittest.TestCase):
    def test_received_schema_preserves_fractional_timestamp_precision(self):
        original = publications()
        original['searched_at'] = '2026-11-19T12:11:19.655931+00:00'
        validate('register_publications', original)
        actual = prepare('register_publications', original)
        self.assertEqual(actual['searched_at'], original['searched_at'])
        self.assertEqual(prepare('register_publications', actual), actual)
        data = snapshot()
        data['profile']['observed_at'] = original['searched_at']
        self.assertEqual(prepare('kununu', data)['profile']['observed_at'], actual['searched_at'])

    def test_union_name_required_by_received_schema(self):
        data = dict(status='partial', date_from='2026-01-01', date_to='2026-11-01', truncated=True,
                    articles=[dict(id='a', title='News', url='https://example.org', published_at=None,
                                   match=dict(matched_name='Acme', matched_in='text'))])
        with self.assertRaisesRegex(ContractError, 'gewerkschaft'):
            prepare('artikel_snapshot', data)

    def test_pdf_pages_are_joined_without_fabricated_tables(self):
        original = [dict(format='pdf', name='report.pdf', pages=[dict(number=1, text='First page'),
                                                                dict(number=2, text='Second page')])]
        actual = prepare('register_documents', original)
        self.assertEqual(actual[0]['text'], 'First page\n\nSecond page')
        self.assertEqual(actual[0]['tables'], [])
        self.assertEqual(actual[0]['pages'], original[0]['pages'])
        self.assertNotIn('text', original[0])
        self.assertEqual(actual, prepare('register_documents', actual))

    def test_download_status_retains_diagnostic_source_status(self):
        for status in ['downloaded_no_text', 'unsupported']:
            result = prepare('register_downloads', [dict(category='finanzberichte', index=1, status=status)])
            self.assertEqual(result[0]['status'], 'error')
            self.assertEqual(result[0]['source_status'], status)
            self.assertTrue(result[0]['error'])

    def test_open_annotations_and_unique_composite_key(self):
        for rows in [
                [dict(index=1, status='downloaded')],
                [dict(category='finanzberichte', index=1, status=None)],
                [dict(category='finanzberichte', index=1, status='downloaded')] * 2]:
            with self.assertRaises(ContractError):
                prepare('register_downloads', rows)
        prepare('register_downloads', [dict(category=c, index=1, status='downloaded')
                                       for c in ['finanzberichte', 'bekanntmachungen']])
        prepare('register_downloads', [dict(category='future-category', index=1, status='future-status')])

    def test_unread_search_pages_are_visible_in_truncated(self):
        original = publications()
        original['coverage'] = dict(search_complete=False, more_pages=True, publication_limit_reached=False)
        original['publications'][0]['language'] = 'de'
        actual = prepare('register_publications', original)
        self.assertIs(actual['truncated'], True)
        self.assertIs(actual['coverage']['publication_limit_reached'], False)
        self.assertEqual(actual['publications'][0]['language'], 'Deutsch')
        self.assertIs(original['truncated'], False)

    def test_articles_have_reason_and_keep_full_text_in_article_file(self):
        article = dict(id='article-1', title='News', url='https://example.org/news',
                       published_at='2026-10-01', gewerkschaft='IG Metall',
                       match=dict(matched_name='Acme', matched_in='text'))
        data = dict(status='partial', date_from='2026-05-01', date_to='2026-11-01',
                    truncated=True, articles=[article])
        actual = prepare('artikel_snapshot', data)
        self.assertIsNone(actual['reason'])
        self.assertNotIn('text', actual['articles'][0])
        prepare('artikel_article', dict(article, text='Acme source text'))
        data['articles'].append(article)
        with self.assertRaises(ContractError):
            prepare('artikel_snapshot', data)

    def test_implisense_missing_facts_block_replacement_of_previous_export(self):
        data = dict(fetched_at=STAMP, period_days=90, articles=[], register_events=[])
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'news.json'
            write_contract_json(path, data, 'implisense_news')
            before = path.read_bytes()
            data['articles'] = [dict(title='Incomplete news')]
            with self.assertRaises(ContractError):
                write_contract_json(path, data, 'implisense_news')
            self.assertEqual(path.read_bytes(), before)

    def test_implisense_open_signal_and_category_are_allowed(self):
        common = dict(companyId='DE123', companyName='Acme', title='News', publishedAt='2026-10-01')
        data = dict(fetched_at=STAMP, period_days=90,
                    articles=[dict(common, sourceUrl='https://example.org/news', signal='future_signal')],
                    register_events=[dict(common, category='future_category')])
        self.assertEqual(prepare('implisense_news', data), data)

    def test_register_writer_delivers_pdf_and_error_status_without_altering_internal_coverage(self):
        class Client:
            page = SimpleNamespace(url='https://example.org/search')
            pages_read, has_more = 1, False

            def search(self, *args, **kwargs):
                return [FinancialReport('Acme', 'Jahresabschluss', '2026-09-01', 'de', 'https://example.org/report', '')]

            def download(self, report, output):
                return dict(status='downloaded', files=[], documents=[dict(format='pdf', pages=[dict(number=1, text='Report')])])

        args = SimpleNamespace(category='both', years=2, max_pages=1, max_publications=30,
                               open=None, download=True, report_index=None, max_reports=3)
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'unternehmensregister'
            company = root / 'Acme'
            result = run_company(Client(), CompanyQuery('Acme', 'Acme'), company, args)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(migrate([root])['checked'], 3)
            args.download = False
            run_company(Client(), CompanyQuery('Acme', 'Acme'), company, args)
            self.assertEqual(json.loads((company / 'downloads.json').read_text()), [])


class MigrationTests(unittest.TestCase):
    def test_flat_delivery_repairs_register_and_restores_scores_with_backups(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / 'delivery'
            company = root / 'Acme'
            pub = publications()
            pub['company'] = 'Acme AG'
            del pub['truncated']
            pub['coverage'] = dict(search_complete=False, more_pages=True)
            put(company / 'register' / 'publications.json', pub)
            downloads = [dict(index=1, status='downloaded', source_url='https://example.org/report',
                              documents_path='publication-1/documents.json')]
            put(company / 'register' / 'downloads.json', downloads)
            put(company / 'register' / 'publication-1' / 'documents.json',
                [dict(format='html', text='Report', tables=[])])
            data = snapshot()
            data['reviews'][0].update(title='Review', content='Text')
            put(company / 'kununu.json', data)
            previous = deepcopy(data)
            previous['reviews'][0]['factor_scores']['atmosphere'] = 4
            history = Path(folder) / 'prior'
            put(history / 'kununu' / 'Acme' / 'snapshot.json', previous)
            result = migrate([root], apply=True, kununu_history=[history])
            self.assertEqual(result['checked'], 4)
            actual = json.loads((company / 'register' / 'publications.json').read_text())
            self.assertEqual(actual['company'], dict(key='Acme', name='Acme AG'))
            self.assertIs(actual['truncated'], True)
            actual = json.loads((company / 'register' / 'downloads.json').read_text())
            self.assertEqual(actual[0]['category'], 'finanzberichte')
            actual = json.loads((company / 'kununu.json').read_text())
            self.assertEqual(actual['reviews'][0]['factor_scores']['atmosphere'], 4)
            self.assertEqual(migrate([root])['checked'], 4)
            self.assertEqual(migrate([root], apply=True, kununu_history=[history])['changed'], 0)
            backups = json.loads((Path(result['backup']) / 'manifest.json').read_text())
            saved = next(b for b in backups if b['path'].endswith('downloads.json'))
            self.assertEqual(json.loads(Path(saved['backup']).read_text()), downloads)

    def test_ambiguous_category_and_escaping_document_path_block_all_writes(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            company = root / 'Acme' / 'register'
            pub = publications()
            pub['publications'].append(dict(pub['publications'][0], category='bekanntmachungen'))
            put(company / 'publications.json', pub)
            path = company / 'downloads.json'
            for row in [dict(index=1, status='error'), dict(index=1, category='finanzberichte',
                        status='downloaded', documents_path='../../outside.json')]:
                put(path, [row])
                before = path.read_bytes()
                with self.assertRaises(ContractError):
                    migrate([root], apply=True)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((root / 'contract-backups').exists())

    def test_apply_is_backed_up_idempotent_and_skips_history_and_careers(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'kununu' / 'Acme' / 'snapshot.json'
            put(path, snapshot())
            original = path.read_bytes()
            history = path.parent / 'history' / 'old.json'
            career = root / 'karriere' / 'Acme' / 'snapshot.json'
            put(history, {'old': True})
            put(career, {'untouched': True})
            self.assertEqual(migrate([root])['checked'], 1)
            self.assertEqual(path.read_bytes(), original)
            result = migrate([root], apply=True)
            self.assertEqual((result['checked'], result['changed']), (1, 1))
            manifest = json.loads((Path(result['backup']) / 'manifest.json').read_text())
            self.assertEqual(Path(manifest[0]['backup']).read_bytes(), original)
            self.assertEqual(json.loads(history.read_text()), {'old': True})
            self.assertEqual(json.loads(career.read_text()), {'untouched': True})
            self.assertEqual(migrate([root], apply=True)['changed'], 0)
            self.assertEqual(migrate([root])['checked'], 1)

    def test_all_files_are_validated_before_any_write(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            a, b = [root / 'kununu' / name / 'snapshot.json' for name in ['A', 'B']]
            put(a, snapshot())
            invalid = snapshot()
            invalid['review_period'] = dict(since='not-a-date', until=STAMP)
            put(b, invalid)
            before = a.read_bytes(), b.read_bytes()
            with self.assertRaises(ContractError):
                migrate([root], apply=True)
            self.assertEqual((a.read_bytes(), b.read_bytes()), before)
            self.assertFalse((root / 'contract-backups').exists())

    def test_missing_article_and_mismatching_download_are_not_accepted(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            article = dict(id='a1', title='News', url='https://example.org/news', published_at=None,
                           match=dict(matched_name='Acme', matched_in='text'))
            put(root / 'gewerkschaften' / 'Acme' / 'snapshot.json',
                dict(status='partial', date_from='2026-05-01', date_to='2026-11-01', truncated=True, articles=[article]))
            with self.assertRaises(ContractError):
                migrate([root], apply=True)
            path = root / 'unternehmensregister' / 'Acme' / 'publications.json'
            records = {path: ('register_publications', publications()),
                       path.with_name('downloads.json'): ('register_downloads', [dict(category='finanzberichte', index=99, status='error')])}
            with self.assertRaises(ContractError):
                check_links(records)

    def test_empty_input_is_not_a_successful_check(self):
        with TemporaryDirectory() as folder:
            with self.assertRaises(ContractError):
                migrate([Path(folder)])


if __name__ == '__main__':
    unittest.main()
