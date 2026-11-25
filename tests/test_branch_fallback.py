"""Branchenübergreifende Reserve nach Fachsuche, ohne Netzwerk."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research_app.research_inputs import CompanyQuery, company_folder, write_json
from research_app.union_articles import UnionSource, collect_sites, make_plan


class Transport:
    def __init__(self, *, max_requests, **kwargs):
        self.requests = self.cache_hits = 0
        self.max_requests = max_requests

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FallbackTests(unittest.TestCase):
    def companies(self):
        return [CompanyQuery('hit', 'hit', branches=['Maschinenbau']),
                CompanyQuery('empty', 'empty', branches=['Maschinenbau']),
                CompanyQuery('unmatched', 'unmatched', branches=['Polizei']),
                CompanyQuery('missing', 'missing')]

    def sources(self):
        return [UnionSource('Broad', ['Branchenübergreifend'], urls=['https://broad.example']),
                UnionSource('Industry', ['Maschinenbau'], urls=['https://industry.example'])]

    def test_plan_keeps_reserve_after_primary_and_respects_name_filter(self):
        plan = make_plan(self.companies(), self.sources(), fallback=True)
        self.assertEqual([p['source'].name for p in plan], ['Industry', 'Broad'])
        self.assertEqual(plan[0]['company_keys'], ['hit', 'empty'])
        self.assertEqual(plan[1]['company_keys'], ['hit', 'empty', 'unmatched', 'missing'])
        self.assertEqual(plan[1]['phase'], 'fallback')
        filtered = make_plan(self.companies(), self.sources(), fallback=True, source_filter='Industry')
        self.assertEqual(len(filtered), 1)

    def test_mixed_source_does_not_search_same_company_twice(self):
        sources = [UnionSource('Mixed', ['Maschinenbau', 'Branchenübergreifend'])]
        plan = make_plan(self.companies(), sources, fallback=True)
        self.assertEqual(plan[0]['company_keys'], ['hit', 'empty'])
        self.assertEqual(plan[1]['company_keys'], ['unmatched', 'missing'])

    def test_collection_falls_back_only_without_primary_articles_and_records_why(self):
        for provider in ['gewerkschaften']:
            with self.subTest(provider=provider), TemporaryDirectory() as folder:
                args = SimpleNamespace(output=Path(folder), article_months=6,
                                       gewerkschaften_max_requests=12, gewerkschaften_max_articles=10)
                companies = self.companies()
                calls = []

                def crawl(transport, association, output, args, *, companies, today, **kwargs):
                    calls.append((association.name, [c.key for c in companies], transport.max_requests))
                    # Consume the allocation: the reserve must still have budget.
                    self.assertGreater(transport.max_requests, transport.requests)
                    transport.requests = transport.max_requests
                    selected = [c for c in companies if c.key == 'hit' or association.name == 'Broad']
                    articles = [dict(id=c.key, title=c.name, text=c.name, url=f'https://example.org/{c.key}',
                                     published_at=today.isoformat(), content_hash=c.key, gewerkschaft=association.name,
                                     company_matches={c.key: dict(matched_name=c.key, matched_in='text')})
                                for c in selected]
                    for article in articles:
                        # A valid, fixed-length digest as produced by the real crawler.
                        import hashlib
                        article['content_hash'] = hashlib.sha256(article['text'].encode()).hexdigest()
                    result = dict(status='complete', articles=articles, truncated=False, documents=[])
                    write_json(output / 'manifest.json', result)
                    return result

                with patch('research_app.union_articles.SiteHTTP', Transport), patch('research_app.union_articles.crawl_union', crawl):
                    collect_sites(companies, args, make_plan(companies, self.sources(), fallback=True), provider=provider)
                self.assertEqual([c[:2] for c in calls], [('Industry', ['hit', 'empty']),
                                                       ('Broad', ['empty', 'unmatched', 'missing'])])
                batch = json.loads((Path(folder) / provider / 'batch.json').read_text())
                self.assertEqual(batch['requests_attempted'], 12)
                for company, reason in [('empty', 'no_primary_articles'), ('unmatched', 'no_matching_primary_source'),
                                        ('missing', 'no_matching_primary_source')]:
                    snapshot = json.loads((Path(folder) / provider / company_folder(company) / 'snapshot.json').read_text())
                    self.assertNotEqual(snapshot['status'], 'skipped')
                    reserve = next(a for a in snapshot['associations'] if a['phase'] == 'fallback')
                    self.assertEqual(reserve['fallback_reason'], reason)
                    self.assertEqual(snapshot['article_count'], 1)

    def test_all_reserves_use_primary_result_not_first_reserve_result(self):
        with TemporaryDirectory() as folder:
            args = SimpleNamespace(output=Path(folder), article_months=6, gewerkschaften_max_requests=20,
                                   gewerkschaften_max_articles=10)
            companies = [CompanyQuery('a', 'a', branches=['Unbekannt'])]
            sources = [UnionSource(name, ['Branchenübergreifend']) for name in ['One', 'Two']]
            calls = []

            def crawl(transport, association, output, args, **kwargs):
                calls.append(association.name)
                return dict(status='complete', articles=[dict(company_matches={'a': {}})], truncated=False)

            with patch('research_app.union_articles.SiteHTTP', Transport), patch('research_app.union_articles.crawl_union', crawl), \
                    patch('research_app.union_articles.write_company_results', return_value={}):
                collect_sites(companies, args, make_plan(companies, sources, fallback=True), provider='gewerkschaften')
            self.assertEqual(calls, ['One', 'Two'])


if __name__ == '__main__':
    unittest.main()
