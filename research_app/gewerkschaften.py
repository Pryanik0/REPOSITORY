"""Unternehmensnennungen auf Gewerkschaftsseiten anhand der Branche finden."""
from dataclasses import replace

from .research_inputs import normalized, workbook_rows
from .site_http import clean_url, host_key
from .union_articles import FILE, collect_sites, load_union_sources, make_plan

IGBCE_NEWSROOM = 'https://www.presseportal.de/nr/56813'
IGBCE_FEED = 'https://www.presseportal.de/rss/pm_56813.rss2?langid=1'
IGMETALL_FEEDS = ['https://www.igmetall.de/igmetall.rss', 'https://www.igmetall.de/presse.rss']


def load_gewerkschaften(path):

    unions = load_union_sources(path, name_column='gewerkschaft')
    for union in unions:
        press_pages = {'gew.de': 'https://www.gew.de/presse',
                       'djv.de': 'https://www.djv.de/news/pressemitteilungen/'}
        if not union.news_urls:
            for url in union.urls:
                if host_key(url) in press_pages:
                    union.news_urls = [press_pages[host_key(url)]]
                    break
        if (normalized(union.name) == 'ig metall'
                and any(host_key(url) == 'igmetall.de' for url in union.urls)):
            if not union.news_urls:
                union.news_urls = [*IGMETALL_FEEDS, 'https://www.igmetall.de/presse/pressemitteilungen',
                                   'https://www.igmetall.de/tarif/tarifrunden']
            union.site_search = False
            union.source_note = ('Offizielle RSS-Feeds und öffentliche Rubriken; '
                                 'die durch robots.txt gesperrte interne Suche wird nicht verwendet.')
        if (normalized(union.name) in ('ig bce', 'igbce') and not union.news_urls
                and any(host_key(url) == 'igbce.de' for url in union.urls)):
            # igbce.de sperrt den Crawler; stattdessen den offiziellen IGBCE-Presseraum verwenden.
            # Eine ausdrücklich eingetragene News-URL hat weiterhin Vorrang.
            union.news_urls = [IGBCE_NEWSROOM, IGBCE_FEED]
            union.url_prefixes = [IGBCE_NEWSROOM, 'https://www.presseportal.de/pm/56813',
                                  IGBCE_FEED.split('?')[0]]
            union.query_values = {'langid': ['1']}
            union.source_note = 'Offizieller IGBCE-Presseraum auf Presseportal (igbce.de sperrt den Crawler).'
    return unions


def add_references(plan, path):
    """Quellenlinks nur für das passende Unternehmen und die passende Gewerkschaft ergänzen."""
    if path is None:
        return plan
    references = list(workbook_rows(path, required_headers=('unternehmen', 'quelle', 'url'), allow_missing=True))
    for item in plan:
        association = item['source']
        companies = {normalized(key) for key in item['company_keys']}
        roots = {host_key(url) for url in association.urls + association.news_urls}
        urls = []
        for _, _, row in references:
            if normalized(row['unternehmen']) not in companies or normalized(row['quelle']) != normalized(association.name):
                continue
            url = clean_url(row['url'])
            if (url and association.allows_url(url) and not FILE.search(url)
                    and any(host_key(url) == root or host_key(url).endswith('.' + root) for root in roots)
                    and url not in urls):
                urls.append(url)
        if urls:
            item['source'] = replace(association, news_urls=list(dict.fromkeys(urls + association.seeds)))
    return plan


def plan_for(companies, args):
    plan = make_plan(companies, load_gewerkschaften(args.gewerkschaften), source_filter=args.gewerkschaft, fallback=True)

    return add_references(plan, args.companies)


def collect_gewerkschaften(companies, args, plan):
    return collect_sites(companies, args, plan, provider='gewerkschaften')
