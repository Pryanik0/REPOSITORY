"""Artikel mit Unternehmensnennungen auf passenden Gewerkschaftsseiten finden."""
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from calendar import monthrange
from email.utils import parsedate_to_datetime
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import shutil
import unicodedata
from urllib.parse import parse_qsl, quote_plus, unquote, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import trafilatura

from .research_inputs import company_folder, normalized, workbook_rows, write_json
from .branches import branch_key, normalize_branches
from .cli import status_text
from .site_http import BudgetExceeded, PDFSkipped, SiteHTTP, clean_url, host_key
from .pdf_reader import extract_pdf
from .output_contract import write_contract_json


def publication_range(args, today):
    if getattr(args, 'article_days', None) is not None:
        return today - timedelta(days=args.article_days), today
    months = getattr(args, 'article_months', 6)
    year, month = divmod(today.year * 12 + today.month - 1 - months, 12)
    month += 1
    return date(year, month, min(today.day, monthrange(year, month)[1])), today

@dataclass
class UnionSource:
    name: str
    branches: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    news_urls: list[str] = field(default_factory=list)
    url_prefixes: list[str] = field(default_factory=list)
    query_values: dict[str, list[str]] = field(default_factory=dict)
    source_note: str = ''
    source_name: str = ''
    site_search: bool = True

    @property
    def seeds(self):
        return self.news_urls or self.urls

    def allows_url(self, url):
        """Gehostete Presseräume auf den gewählten Herausgeber begrenzen."""
        target = urlsplit(url)
        if any(key in self.query_values and value not in self.query_values[key]
               for key, value in parse_qsl(target.query, keep_blank_values=True)):
            return False
        if not self.url_prefixes:
            return True
        for prefix in self.url_prefixes:
            scope = urlsplit(prefix)
            path = scope.path.rstrip('/')
            if (host_key(url) == host_key(prefix) and target.scheme == scope.scheme
                    and (target.path.rstrip('/') == path or target.path.startswith(path + '/'))):
                return True
        return False


def load_union_sources(path, *, name_column='gewerkschaft'):
    associations = {}
    for sheet, number, row in workbook_rows(path, required_headers=(name_column, 'branche')):
        name, branch = row[name_column], row['branche']
        if not name or not branch:
            raise ValueError(f'{path}/{sheet}:{number}: {name_column} oder Branche ist leer.')
        association = associations.setdefault(normalized(name), UnionSource(name))
        association.branches = normalize_branches([*association.branches, branch])
        for column, target in [('website', association.urls), ('news-url', association.news_urls)]:
            value = row.get(column, '')
            if not value:
                continue
            parts = urlsplit(value)
            if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
                raise ValueError(f'{path}/{sheet}:{number}: ungültiger Wert in {column}.')
            if value not in target:
                target.append(value)
    return list(associations.values())


def make_plan(companies, associations, *, cross_sector=False, source_filter=None, fallback=False):
    plan = []
    reserves = []
    for association in associations:
        if source_filter and normalized(source_filter) not in normalized(association.name):
            continue
        branches = {branch_key(branch) for branch in normalize_branches(association.branches)}
        broad = branch_key('Branchenübergreifend') in branches
        matched = [company.key for company in companies
                   if branches.intersection(branch_key(b) for b in normalize_branches(company.branches))
                   or (broad and cross_sector)]
        if matched:
            plan.append(dict(source=association, company_keys=matched))
        if fallback and broad:
            candidates = [company.key for company in companies if company.key not in matched]
            if candidates:
                reserves.append(dict(source=association, company_keys=candidates, phase='fallback'))
    return plan + reserves

SEARCH_PATH = re.compile(r'/(?:suche|suchergebnis(?:se)?|search|sitesearch)(?:[-/]|$)', re.I)
QUERY_NAME = re.compile(r'^(?:q|s|query|search|searchterm|searchword|searchtext|term|keywords|tx_\w+\[(?:q|sword|query|searchword)\])$', re.I)


def company_terms(company):
    terms, seen = [], set()
    for term in [company.key, company.name, *company.search_terms]:
        if normalized(term) and normalized(term) not in seen:
            terms.append(term.strip())
            seen.add(normalized(term))
    return terms


def mention(text, terms, *, strict_acronyms=False):
    text = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text)).strip()
    for term in terms:
        pattern = r'(?<!\w)' + r'\s+'.join(re.escape(p) for p in unicodedata.normalize('NFKC', term).split()) + r'(?!\w)'
        # Kurze Marken in Großbuchstaben nur in der passenden Schreibweise erkennen:
        # Ein gewöhnlicher „Link“ ist kein Nachweis für das Unternehmen „LINK“.
        acronym = strict_acronyms and term.isalpha() and term.isupper() and 2 <= len(term) <= 5
        found = re.search(pattern, text, 0 if acronym else re.I)
        if found:
            return dict(matched_name=term, excerpt=text[max(0, found.start() - 90):found.end() + 130])
    return None


def company_matches(article, companies):
    matches = {}
    for company in companies:
        for field in ('title', 'text'):
            evidence = mention(article.get(field, ''), company_terms(company), strict_acronyms=True)
            if evidence:
                matches[company.key] = dict(evidence, matched_in=field)
                break
    return matches


def pdf_title_matches(url, title, companies, *, description=''):
    """Firmenbezug vor dem PDF-Abruf anhand von Linktext und Dateiname prüfen."""
    description = description if len(description) <= 1500 else ''
    filename = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
    filename = re.sub(r'[-_]+', ' ', filename)
    return any(mention(title or '', company_terms(company), strict_acronyms=True)
               or mention(description or '', company_terms(company), strict_acronyms=True)
               or mention(re.sub(r'[-_]+', ' ', title or ''),
                          [re.sub(r'[-_]+', ' ', term) for term in company_terms(company)], strict_acronyms=True)
               or mention(filename, [re.sub(r'[-_]+', ' ', term) for term in company_terms(company)])
               for company in companies)


def pdf_link_description(node):
    """Eine lokale Ergebniskarte statt der gesamten Seite auswerten."""
    for parent in node.parents:
        if parent.name in ('main', 'body', 'html'):
            break
        classes = ' '.join(parent.get('class', []))
        if parent.name not in ('p', 'li', 'article') and not re.search(r'card|teaser|result', classes, re.I):
            continue
        pdf_links = {link.get('href') for link in parent.select('a[href]')
                     if PDF.search(link['href']) or 'application/pdf' in link.get('type', '').lower()}
        if len(pdf_links) > 1:
            continue
        text = parent.get_text(' ', strip=True)
        if len(text) <= 1500:
            # Ein allgemeiner Downloadabsatz darf die umgebende Ergebniskarte nicht verdecken.
            if text != node.get_text(' ', strip=True):
                return text
    return ''


def article_url(url):
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=re.sub(r'/drucken/?$', '/', parts.path, flags=re.I)))


def same_article(left, right):
    if article_url(left['url']) == article_url(right['url']) or left['content_hash'] == right['content_hash']:
        return True
    if left['published_at'] != right['published_at'] or (left.get('format') == 'pdf') == (right.get('format') == 'pdf'):
        return False
    a = re.findall(r'[^\W_]+', normalized(left['text']))
    b = re.findall(r'[^\W_]+', normalized(right['text']))
    if not a or not b or min(len(a), len(b)) / max(len(a), len(b)) < .85:
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= .9


def search_fingerprint(html):
    soup = BeautifulSoup(html, 'html.parser')
    scope = soup.find('main') or soup
    for node in scope.select('script, style, nav, header, footer, form'):
        node.decompose()
    return hashlib.sha256(normalized(scope.get_text(' ', strip=True)).encode()).hexdigest()


def discover_searches(html, url, hosts, companies):
    """Nur erkennbare GET-Suchformulare oder veröffentlichte SearchAction-Vorlagen verwenden."""
    soup = BeautifulSoup(html, 'html.parser')
    results, seen = [], set()

    def add(target, company, term, mechanism):
        target = clean_url(target, url)
        if target and host_key(target) in hosts and target not in seen:
            seen.add(target)
            results.append(dict(url=target, company_key=company.key, query=term, mechanism=mechanism))

    for form in soup.find_all('form'):
        if form.get('method', 'get').lower() != 'get':
            continue
        inputs = [node for node in form.find_all('input') if node.get('name') and not node.has_attr('disabled')]
        query = next((node for node in inputs if node.get('type', 'text').lower() in ('text', 'search')
                      and (QUERY_NAME.fullmatch(node['name']) or node.get('type', '').lower() == 'search')), None)
        if query is None:
            continue
        action = clean_url(form.get('action') or url, url)
        if not action or host_key(action) not in hosts:
            continue
        # Standort-/Mitglieder-/Geschäftsstellensuchen liefern keine Publikationssuche.
        if re.search(r'geschaeftsstellen|geschäftsstellen|standort|postcode|postal|plz|newsletter|login',
                     ' '.join((action, query['name'], query.get('placeholder', ''), form.get('id', ''))), re.I):
            continue
        query_name = query['name']
        mechanism = 'get_form'
        if host_key(url) == 'verdi.de' and not form.get('action') and query_name == 'search':
            # Von ver.di veröffentlichte Suchroute: /suche?q=Suchbegriff.
            action = clean_url('/suche', url)
            query_name, mechanism = 'q', 'verdi_site_search'
        parts = urlsplit(action)
        hidden = [(node['name'], node.get('value', '')) for node in inputs
                  if node.get('type', '').lower() == 'hidden' and node['name'] != query['name']]
        values = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                  if k not in {query_name, *(name for name, _ in hidden)}] + hidden
        for company in companies:
            for term in company_terms(company):
                target = urlunsplit(parts._replace(query=urlencode(values + [(query_name, term)])))
                add(target, company, term, mechanism)

    def walk(value):
        if isinstance(value, dict):
            if value.get('@type') == 'SearchAction':
                target = value.get('target')
                if isinstance(target, dict):
                    target = target.get('urlTemplate')
                if isinstance(target, str):
                    placeholders = re.findall(r'\{([^{}]+)\}', target)
                    if len(placeholders) == 1:
                        for company in companies:
                            for term in company_terms(company):
                                add(target.replace('{' + placeholders[0] + '}', quote_plus(term)), company, term, 'search_action')
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
    return results


def search_landing_pages(html, url, hosts):
    soup = BeautifulSoup(html, 'html.parser')
    targets = set()
    for node in soup.select('a[href]'):
        target = clean_url(node['href'], url)
        if target and host_key(target) in hosts and SEARCH_PATH.search(urlsplit(target).path):
            targets.add(urlunsplit(urlsplit(target)._replace(query='')))
    return sorted(targets)[:2]


NEWS = re.compile(r'presse|press|news|aktuelles|aktuell|meldungen|mitteilungen|nachrichten|artikel|article|detail', re.I)
UNWANTED = re.compile(r'(?:^|/)(?:impressum|datenschutz|privacy|kontakt|contact|login|anmelden|newsletter|veranstaltungen?|events?|karriere|jobs|mitglied-werden|mediathek|videos?|podcasts?)(?:/|$|\.(?:html?|php|jsp)$)', re.I)
FILE = re.compile(r'\.(?:zip|jpg|jpeg|png|gif|svg|css|js|mp4|docx?|xlsx?)(?:$|\?)', re.I)
PDF = re.compile(r'\.pdf(?:$|\?)', re.I)
MONTHS = dict(januar=1, februar=2, märz=3, maerz=3, april=4, mai=5, juni=6,
              juli=7, august=8, september=9, oktober=10, november=11, dezember=12)


def parse_date(value):
    value = str(value or '').strip()
    match = re.search(r'\b(20\d{2})-(\d{2})-(\d{2})(?!\d)', value)
    if match:
        parts = tuple(map(int, match.groups()))
    else:
        match = re.search(r'\b(\d{1,2})\.\s*(\d{1,2})(?:\.\s*|\s+)(20\d{2}|\d{2})(?!\d)', value)
        if match:
            day, month, year = map(int, match.groups())
            if year < 100:
                year += 2000
            parts = year, month, day
        else:
            match = re.search(r'\b(\d{1,2})\.?\s+(' + '|'.join(MONTHS) + r')\s+(20\d{2})\b', value, re.I)
            if not match:
                try:
                    return parsedate_to_datetime(value).date()
                except (ValueError, TypeError, OverflowError):
                    return None
            parts = int(match[3]), MONTHS[match[2].lower()], int(match[1])
    try:
        return date(*parts)
    except ValueError:
        return None


def structured_articles(soup):
    def walk(value):
        if isinstance(value, dict):
            types = value.get('@type', [])
            types = [types] if isinstance(types, str) else types
            if any(str(t).rsplit('/', 1)[-1].lower() in ('article', 'newsarticle', 'blogposting', 'reportagenewsarticle') for t in types or []):
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)
    articles = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            articles.extend(walk(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError):
            continue
    return articles


def extract_article(html, url, *, start, end, keywords=(), feed_date=None):
    if UNWANTED.search(urlsplit(url).path) or SEARCH_PATH.search(urlsplit(url).path):
        return None, 'excluded_page_type'
    soup = BeautifulSoup(html, 'html.parser')
    headings = soup.select('main h1, article h1') or soup.find_all('h1')
    h1 = next((node for node in headings if node.get_text(' ', strip=True)
               and not node.find_parent('nav')
               and normalized(node.get_text(' ', strip=True)) not in ('navigationsmenü', 'navigation')), None)
    title = h1.get_text(' ', strip=True) if h1 else ''
    if not title:
        meta_title = soup.find('meta', property='og:title')
        title = meta_title.get('content', '').strip() if meta_title else ''
    if not title:
        return None, 'missing_article_heading'
    if (normalized(title) in ('presse', 'news', 'newsroom', 'aktuelles', 'pressemitteilungen', 'nachrichten', 'pressemeldungen')
            or re.match(r'^(?:newsarchiv|nachrichtenarchiv|archiv|archive)(?:\s|$)', normalized(title))
            or re.search(r'/(?:newsarchiv|nachrichtenarchiv|archiv|archive)(?:/|$)', urlsplit(url).path, re.I)):
        return None, 'listing_page'
    schemas = structured_articles(soup)
    og = soup.find('meta', property='og:type')
    is_article = (bool(schemas and len(schemas) == 1) or bool(og and og.get('content', '').lower() == 'article')
                  or bool(h1 and soup.select_one('.pageIntro__author')))
    visible_publication = None
    if host_key(url) == 'verdi.de':
        # Die Seite kennzeichnet Artikel ausdrücklich, nutzt aber og:type=website
        # und section/div-Elemente statt eines article-Elements.
        scope = soup.find('main') or soup
        visible_publication = re.search(r'Veröffentlicht am\s+(\d{1,2}\.\d{1,2}\.20\d{2})',
                                        scope.get_text(' ', strip=True))
        is_article = is_article or bool(h1 and visible_publication)
    if not is_article and not (h1 and h1.find_parent('article')) and (len(soup.find_all('article')) > 2 or len(soup.find_all('time')) > 3):
        return None, 'listing_page'
    candidates = [item.get('datePublished') for item in schemas] if len(schemas) == 1 else []
    if visible_publication:
        candidates.append(visible_publication[1])
    for node in soup.select('meta[property="article:published_time"], meta[name="date"], '
                            'meta[name="publicationdate"], meta[name="DC.date.issued"], '
                            'meta[name="dcterms.date"], meta[itemprop="datePublished"], '
                            '[itemprop="datePublished"]'):
        candidates.append(node.get('content') or node.get('datetime') or node.get_text(' ', strip=True))
    for node in soup.select('.pageIntro__author'):
        # IG Metall wiederholt Datumsangaben für Mobil-/Desktopansicht und hängt Aktualisierungen an.
        # Das erste sichtbare Datum bezeichnet die ursprüngliche Veröffentlichung.
        candidates.append(node.get_text(' ', strip=True))
    # Eine kurze Datumszeile an der Überschrift zählt ebenfalls als Nachweis.
    near_heading = h1
    for _ in range(3):
        near_heading = near_heading.parent if near_heading else None
        if not near_heading or near_heading.name in ('html', 'body'):
            break
        for node in near_heading.find_all(['span', 'p', 'dd', 'time', 'b', 'strong']):
            if node.find_parent(['aside', 'nav', 'footer']) or node.find_parent(class_=re.compile(r'related|teaser|news-list', re.I)):
                continue
            value = node.get_text(' ', strip=True)
            if len(value) <= 250 and re.match(r'^(?:[\w .-]{1,40},\s*)?(?:\d{1,2}[.\s]|20\d{2}-)', value):
                candidates.append(value)
    published = next((parsed for value in candidates if (parsed := parse_date(value))), None)
    date_source = 'article_metadata'
    if not published and feed_date:
        published, date_source = feed_date, 'feed'
    if not published:
        # Datumsangaben aus Empfehlungen und Seitenleisten ausschließen.
        scope = (h1.find_parent('article') if h1 else None) or soup.find('main') or soup
        date_nodes = scope.find_all('time') + scope.find_all(class_=re.compile(r'date|datum|timestamp', re.I))
        for node in date_nodes:
            if node.find_parent(['nav', 'footer', 'aside']) or node.find_parent(class_=re.compile(r'related|teaser|result|news-list', re.I)):
                continue
            value = node.get('datetime') or node.get_text(' ', strip=True)
            if len(value) <= 90 and (published := parse_date(value)):
                break
    if not published:
        return None, 'unknown_publication_date'
    if not start <= published <= end:
        return None, 'outside_publication_window'
    if not is_article and not (soup.find('article') or (NEWS.search(urlsplit(url).path) and len(urlsplit(url).path.strip('/').split('/')) > 1)):
        return None, 'not_an_article'
    # Wiederkehrende Bedienelemente vor der Textextraktion entfernen.
    for node in soup.select('nav, header, footer, aside, form, script, style, [role="navigation"], '
                            '[role="dialog"], [class*="cookie"], [id*="cookie"], [class*="newsletter"], '
                            '[class*="social-share"], [class*="related"], [class*="breadcrumb"]'):
        if node.name not in ('html', 'body', 'main', 'article'):
            node.decompose()
    text = trafilatura.extract(str(soup), url=url, include_comments=False, include_tables=False,
                               favor_precision=True, deduplicate=False)
    if host_key(url) == 'igmetall.de':
        # Der eigentliche Artikel liegt in contentBlock; innere teaserGrid-Klassen
        # führen bei generischer Extraktion zum Verlust ganzer Absätze. Kontakt-
        # und Cookie-Blöcke gehören nicht zum Artikel (z.B. Pressesprecher Siemens).
        blocks = soup.select('section.contentBlock')
        if blocks:
            body = '\n\n'.join(block.get_text('\n', strip=True) for block in blocks)
            if len(body.strip()) >= 300 and len(body.split()) >= 45:
                text = body
    if not text or len(text.strip()) < 300 or len(text.split()) < 45:
        return None, 'insufficient_article_text'
    text = text.strip()
    if keywords and not any(normalized(word) in normalized(title + '\n' + text) for word in keywords):
        return None, 'keyword_filter'
    canonical = soup.find('link', rel='canonical')
    canonical_url = clean_url(canonical.get('href', ''), url) if canonical else None
    if canonical_url and host_key(canonical_url) != host_key(url):
        canonical_url = None
    return dict(title=title, url=article_url(canonical_url or url), fetched_url=url,
                published_at=published.isoformat(), text=text,
                date_source=date_source,
                content_hash=hashlib.sha256(normalized(text).encode()).hexdigest()), None


def discover_links(html, url, hosts, *, companies=(), search_results=False, start=None, end=None,
                   publication_dates=None, pdf_titles=None, page_date=None, pdf_descriptions=None):
    soup = BeautifulSoup(html, 'html.parser')
    links, dates = {}, {}
    current_page = next((int(value) for key, value in parse_qsl(urlsplit(url).query)
                         if (key in ('page', 'p') or key.endswith('[page]')) and value.isdigit()), 1)
    for node in soup.select('a[href], link[rel="alternate"][href]'):
        target = clean_url(node.get('href', ''), url)
        if not target or host_key(target) not in hosts or FILE.search(target):
            continue
        target = article_url(target)
        path = urlsplit(target).path
        label = node.get_text(' ', strip=True)
        pdf_link = bool(PDF.search(target) or 'application/pdf' in node.get('type', '').lower())
        page = next((int(value) for key, value in parse_qsl(urlsplit(target).query)
                     if (key in ('page', 'p') or key.endswith('[page]')) and value.isdigit()), None)
        pagination = (search_results and ('next' in node.get('rel', []) or
                      re.search(r'^(?:weiter|nächste(?:r|s)?(?:\s+seite)?|next(?:\s+page)?)\b',
                                label or node.get('title', ''), re.I) or
                      (label.isdigit() and page == current_page + 1)))
        if pagination:
            current_terms = {(key, value) for key, value in parse_qsl(urlsplit(url).query)
                             if QUERY_NAME.fullmatch(key) and value.strip()}
            target_terms = {(key, value) for key, value in parse_qsl(urlsplit(target).query)
                            if QUERY_NAME.fullmatch(key) and value.strip()}
            # Die leere Suchstartseite darf keinen ungezielten Archivcrawl auslösen.
            # Auch eine Folgeseite, die den Firmensuchbegriff verliert, ist kein Trefferpfad.
            if not current_terms or not current_terms.issubset(target_terms):
                continue
        if (UNWANTED.search(path) or SEARCH_PATH.search(path)) and not (pagination and SEARCH_PATH.search(path)):
            continue
        if path.startswith(('/en/', '/fr/', '/es/')):
            continue
        feed = node.name == 'link' and any(t in node.get('type', '') for t in ('rss', 'atom'))
        if feed and 'comment' in target.lower():
            continue
        if search_results and not pagination and node.find_parent(['nav', 'header', 'footer', 'form']):
            continue
        if not pagination:
            card = node.find_parent(['article', 'li', 'p']) or node.find_parent(
                class_=lambda value: value and re.search(r'(?:search-result|results-entry)', str(value)))
            if card:
                # Überlagerte Links können leer sein; die Überschrift steht in der Ergebniskarte.
                label = label + ' ' + card.get_text(' ', strip=True)
                time = card.find('time')
                stamp = parse_date(time.get('datetime') or time.get_text(' ', strip=True)) if time else None
                stamp = stamp or parse_date(card.get_text(' ', strip=True)[:50])
                if stamp:
                    if (start and stamp < start) or (end and stamp > end):
                        continue
                    dates[target] = stamp
                    if publication_dates is not None:
                        publication_dates.setdefault(target, stamp)
            heading = card.find(['h1', 'h2', 'h3']) if card else None
            title = ' '.join(filter(None, [node.get_text(' ', strip=True), node.get('title'),
                node.get('aria-label'), heading.get_text(' ', strip=True) if heading else None]))
            if pdf_titles is not None and title:
                pdf_titles[target] = title
            description = pdf_link_description(node)
            if pdf_descriptions is not None and description:
                pdf_descriptions[target] = description
            if pdf_link:
                if not pdf_title_matches(target, title, companies, description=description):
                    continue
                if publication_dates is not None and page_date and target not in publication_dates:
                    publication_dates[target] = page_date
        targeted = any(mention(label + ' ' + target, company_terms(company)) for company in companies)
        if targeted and not SEARCH_PATH.search(path):
            links[target] = -4
        elif pagination:
            links[target] = 0
        elif feed:
            links[target] = -1
        elif pdf_link:
            links[target] = 0
        elif search_results and len(label) >= 12 and not node.find_parent(['nav', 'header', 'footer', 'form']):
            links[target] = -3
        elif NEWS.search(path) or NEWS.search(label):
            # Lange Artikeltitel und datierte oder tiefe Pfade vor Rubrikmenüs prüfen.
            detail = len(label) > 45 or bool(re.search(r'/20\d{2}/|/\d{4}-\d{2}', path))
            priority = 0 if detail else 2
            links[target] = min(links.get(target, 9), priority)
        elif len(label) > 45 and node.find_parent(['main', 'article']):
            links[target] = 1
    return sorted(((priority, target) for target, priority in links.items()),
                  key=lambda item: (item[0], -dates.get(item[1], date.min).toordinal()))


def discover_xml(text, url, hosts, start, feed_dates=None, companies=(), pdf_titles=None, pdf_descriptions=None):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    local = lambda tag: tag.rsplit('}', 1)[-1]
    links = []
    for item in root.iter():
        if local(item.tag) not in ('url', 'sitemap', 'item', 'entry'):
            continue
        target = stamp = None
        title, description = '', ''
        for child in item:
            tag = local(child.tag)
            if tag in ('loc', 'link') and (child.text or child.get('href')):
                target = clean_url(child.get('href') or child.text.strip(), url)
            if tag in ('pubDate', 'published', 'lastmod'):
                stamp = parse_date(child.text)
            if tag == 'title':
                title = ''.join(child.itertext())
            if tag in ('description', 'summary'):
                value = BeautifulSoup(' '.join(child.itertext()), 'html.parser').get_text(' ', strip=True)
                if len(value) <= 1500:
                    description = value
        if not target or host_key(target) not in hosts or FILE.search(target) or UNWANTED.search(urlsplit(target).path):
            continue
        if pdf_titles is not None and title:
            pdf_titles[target] = title
        if pdf_descriptions is not None and description:
            pdf_descriptions[target] = description
        if PDF.search(target):
            if not pdf_title_matches(target, title, companies, description=description):
                continue
        if stamp and local(item.tag) in ('item', 'entry') and feed_dates is not None:
            feed_dates[target] = stamp
        if stamp and stamp < start:
            continue
        if local(item.tag) == 'sitemap':
            if NEWS.search(target) or 'post' in target:
                links.append((1, target))
        else:
            targeted = any(mention(' '.join(item.itertext()) + ' ' + target, company_terms(company)) for company in companies)
            if targeted or local(item.tag) in ('item', 'entry') or NEWS.search(target) or PDF.search(target):
                links.append((-4 if targeted else 0, target))
    # Sitemaps beginnen oft mit alten Einträgen; neuere URLs zuerst prüfen.
    return sorted(dict.fromkeys(links), key=lambda item: (item[0], -(feed_dates or {}).get(item[1], date.min).toordinal()))[:300]


def crawl_union(transport, association, output, args, *, companies=(), today=None, memo=None,
                  provider='gewerkschaften'):
    today = today or datetime.now(ZoneInfo('Europe/Berlin')).date()
    start, today = publication_range(args, today)
    hosts = {host_key(url) for url in association.urls + association.news_urls}
    queue, queued, visited, articles, rejected, errors = [], set(), [], [], [], []
    owners, search_visits, content_visits = {}, {}, {}
    search_pages = content_pages = 0
    search_limit = getattr(args, 'gewerkschaften_max_search_pages', None) or max(15, 2 * len(companies))
    queue_capped = False
    memo = memo if memo is not None else {}
    sequence = 0
    pdf_titles, pdf_descriptions, documents = {}, {}, []

    def enqueue(priority, url, company_keys=()):
        nonlocal sequence, queue_capped
        url = clean_url(url)
        url = article_url(url) if url else None
        if url and PDF.search(url) and not pdf_title_matches(url, pdf_titles.get(url, ''), companies,
                                                            description=pdf_descriptions.get(url, '')):
            return
        if url and association.allows_url(url):
            owners.setdefault(url, set()).update(company_keys)
        if url and association.allows_url(url) and url not in queued and len(queued) < 1000:
            queued.add(url)
            queue.append((priority, sequence, url))
            sequence += 1
        elif url and url not in queued and len(queued) >= 1000:
            queue_capped = True

    for url in association.seeds:
        enqueue(-20, url)
    before = transport.requests
    feed_dates = {}
    searches, search_urls = [], set()
    landing_fingerprints = {}
    articles_examined = 0
    limited = False
    seen_urls, seen_text = set(), set()
    def needs_articles():
        return any(sum(company.key in article['company_matches'] for article in articles) < args.gewerkschaften_max_articles
                   for company in companies)

    def is_search_url(url):
        return url in search_urls or bool(SEARCH_PATH.search(urlsplit(url).path))

    def queue_order(item):
        priority, sequence, url = item
        counters = search_visits if is_search_url(url) else content_visits
        keys = owners.get(url, set())
        turns = min((counters.get(key, 0) for key in keys), default=max(counters.values(), default=0) + 1)
        # Startquellen zuerst, dann Firmensuchen; Artikel gleichmäßig auf Firmen verteilen.
        tier = (0 if priority == -20 else 1 if is_search_url(url) and priority < 0 else
                3 if is_search_url(url) else 2)
        return tier, turns, priority, sequence

    while queue and needs_articles():
        eligible = [item for item in queue if
                    (search_pages < search_limit if is_search_url(item[2]) else
                     content_pages < args.gewerkschaften_max_pages)]
        if not eligible:
            break
        item = min(eligible, key=queue_order)
        queue.remove(item)
        _, _, url = item
        planned_search = is_search_url(url)
        if planned_search:
            search_pages += 1
            counters = search_visits
        else:
            content_pages += 1
            counters = content_visits
        for key in owners.get(url, set()):
            counters[key] = counters.get(key, 0) + 1
        visited.append(url)
        try:
            if url not in memo:
                memo[url] = transport.get(url, hosts, allow_pdf=pdf_title_matches(url, pdf_titles.get(url, ''), companies,
                                                                               description=pdf_descriptions.get(url, '')))
            response = memo[url]
            content = response.get('text', '')
            final = response['final_url']
            if not association.allows_url(final):
                rejected.append(dict(url=url, reason='outside_source_scope'))
                continue
            is_pdf = 'body_b64' in response
            if is_pdf and not pdf_title_matches(url, pdf_titles.get(url, ''), companies,
                                               description=pdf_descriptions.get(url, '')):
                rejected.append(dict(url=url, reason='pdf_title_no_company_match'))
                continue
            if not is_pdf and ('xml' in response['content_type'] or content.lstrip().startswith(('<?xml', '<urlset', '<rss', '<feed', '<sitemapindex'))):
                for priority, target in discover_xml(content, final, hosts, start, feed_dates, companies,
                                                     pdf_titles, pdf_descriptions):
                    enqueue(priority, target, owners.get(url, ()))
                continue
            if not is_pdf and 'html' not in response['content_type'] and not re.search(r'<(?:html|!doctype)', content[:300], re.I):
                rejected.append(dict(url=url, reason='unsupported_content_type'))
                continue
            if not is_pdf and url not in search_urls and not SEARCH_PATH.search(urlsplit(final).path):
                landing_fingerprints.setdefault(host_key(final), search_fingerprint(content))
            for search in (discover_searches(content, final, hosts, companies)
                           if association.site_search and not searches else []):
                if association.allows_url(search['url']) and search['url'] not in search_urls:
                    search_urls.add(search['url'])
                    searches.append(search)
                    enqueue(-5, search['url'], [search['company_key']])
            if association.site_search and not searches:
                for target in search_landing_pages(content, final, hosts):
                    enqueue(-5, target)
            is_search = not is_pdf and (url in search_urls or bool(SEARCH_PATH.search(urlsplit(final).path)))
            if is_search and search_fingerprint(content) == landing_fingerprints.get(host_key(final)):
                for search in searches:
                    if search['url'] == url:
                        search['status'] = 'ignored_query'
                rejected.append(dict(url=url, reason='ignored_search_query'))
                errors.append(dict(url=url, error='Die Suchanfrage lieferte die unveränderte Startseite.'))
                continue
            if is_pdf:
                article, reason, document = extract_pdf(response, output / 'documents', start=start, end=today,
                    parse_date=parse_date, keywords=args.article_keyword,
                    published=feed_dates.get(url) or feed_dates.get(final), title=pdf_titles.get(url, ''))
                document['link_description'] = pdf_descriptions.get(url, '')
                documents.append(document)
            elif is_search:
                article, reason = None, 'search_results_page'
            else:
                article, reason = extract_article(content, final, start=start, end=today, keywords=args.article_keyword,
                                                  feed_date=feed_dates.get(url) or feed_dates.get(final))
            if article and not association.allows_url(article['url']):
                article, reason = None, 'outside_source_scope'
            if article:
                articles_examined += 1
                article['company_matches'] = company_matches(article, companies)
                if not article['company_matches']:
                    rejected.append(dict(url=url, reason='no_company_mention'))
                    if is_pdf:
                        document['reason'] = 'no_company_mention'
                elif article['url'] in seen_urls or article['content_hash'] in seen_text or any(same_article(article, old) for old in articles):
                    rejected.append(dict(url=url, reason='duplicate_article'))
                    if is_pdf:
                        document['reason'] = 'duplicate_article'
                else:
                    seen_urls.add(article['url'])
                    seen_text.add(article['content_hash'])
                    article['id'] = hashlib.sha256(article['url'].encode()).hexdigest()[:20]
                    article['gewerkschaft'] = association.name
                    if association.source_name:
                        article['publisher'] = association.source_name
                    article['branches'] = association.branches
                    article['fetched_at'] = response['fetched_at']
                    articles.append(article)
                    if is_pdf:
                        document['included'] = True
            else:
                rejected.append(dict(url=url, reason=reason))
            if is_pdf:
                write_json(Path(document['pdf_path']).parent / 'document.json', document)
                # Binärdaten nur im Dateicache halten.
                memo.pop(url, None)
                continue
            for priority, target in discover_links(content, final, hosts, companies=companies,
                                                   search_results=is_search, start=start, end=today,
                                                   publication_dates=feed_dates, pdf_titles=pdf_titles,
                                                   pdf_descriptions=pdf_descriptions,
                                                   page_date=parse_date(article['published_at']) if article else None):
                if is_search and (target in search_urls or SEARCH_PATH.search(urlsplit(target).path)):
                    search_urls.add(target)
                enqueue(priority, target, owners.get(url, ()) if is_search else ())
            if len(visited) == 1:
                # Nur veröffentlichte Sitemaps verwenden.
                for robot in transport.robots.values():
                    for target in robot.site_maps() or []:
                        if host_key(target) in hosts:
                            enqueue(3, target)
        except PDFSkipped:
            rejected.append(dict(url=url, reason='pdf_title_no_company_match'))
        except BudgetExceeded as error:
            errors.append(dict(url=url, error=str(error)))
            limited = True
            break
        except Exception as error:
            errors.append(dict(url=url, error=f'{type(error).__name__}: {error}'))
    limited = limited or bool(queue) or queue_capped
    failed_urls = {row['url'] for row in errors}
    for search in searches:
        if search.get('status') == 'ignored_query':
            continue
        search['status'] = ('not_attempted' if search['url'] not in visited else
                            'error' if search['url'] in failed_urls else 'fetched')
    articles.sort(key=lambda row: row['published_at'], reverse=True)
    records = [{k: v for k, v in article.items() if k != 'text'} for article in articles]
    status = 'partial' if errors or limited or any(d['status'] == 'downloaded_no_text' for d in documents) else 'complete'
    if not articles_examined and errors and len(errors) == len(visited):
        status = 'error'
    elif not articles_examined and not errors and not limited:
        status = 'partial'  # Keine erfolgreiche Artikelextraktion; verworfene Seiten zur Diagnose prüfen.
    result = dict(status=status, provider=provider, date_from=start.isoformat(),
                  pdf_download_policy='company_in_link_metadata_or_filename',
                  date_to=today.isoformat(), articles=records, visited=visited,
                  rejected=rejected, errors=errors, truncated=limited,
                  searches=searches, company_keys=[company.key for company in companies],
                  search_mode='site_search' if searches else 'link_discovery' if association.site_search else 'feeds_and_links',
                  articles_examined=articles_examined,
                  page_coverage=dict(search_pages=search_pages, content_pages=content_pages,
                                     search_page_limit=search_limit, content_page_limit=args.gewerkschaften_max_pages,
                                     queued_remaining=len(queue), queue_limit_reached=queue_capped),
                  documents=documents,
                  requests_attempted=transport.requests - before)
    result['gewerkschaft'] = asdict(association)
    write_json(output / 'manifest.json', result)
    result['articles'] = articles
    return result


def add_arguments(parser):
    parser.add_argument('--companies', type=Path, help='Excel-Firmenliste: Unternehmen, Branche; optional aus --sources ergänzen.')
    parser.add_argument('--gewerkschaften', type=Path, default=Path('data/Gewerkschaften.xlsx'))
    parser.add_argument('--list-gewerkschaften', action='store_true', help='Passende Gewerkschaften ohne Netzwerkzugriff oder Dateiausgabe anzeigen.')
    parser.add_argument('--gewerkschaft', help='Nur Gewerkschaften mit diesem Text im Namen auswählen.')
    parser.add_argument('--gewerkschaften-max-requests', type=int, default=2000,
                        help='HTTP-Anfragelimit je Quellenart einschließlich robots.txt und Weiterleitungen (Standard: 2000).')
    parser.add_argument('--gewerkschaften-max-pages', type=int, default=50,
                        help='Maximale Inhalts-/Einstiegsseiten je Website, ohne Suchseiten (Standard: 50).')
    parser.add_argument('--gewerkschaften-max-search-pages', type=int,
                        help='Suchseiten je Website; Standard: mindestens 15 oder zweimal die Firmenanzahl.')
    parser.add_argument('--gewerkschaften-max-articles', type=int, default=10,
                        help='Maximale passende Artikel je Unternehmen und Quellenart (Standard: 10).')
    parser.add_argument('--gewerkschaften-cache-only', action='store_true',
                        help='Gespeicherte Antworten ohne Netzwerkzugriff erneut auswerten.')
    parser.add_argument('--gewerkschaften-browser-fallback', action='store_true',
                        help='GEW/DJV bei HTTP 403/500 zusätzlich im versteckten Browser laden; keine CAPTCHA-Lösung.')
    period = parser.add_mutually_exclusive_group()
    period.add_argument('--article-months', type=int, default=6, help='Veröffentlichungszeitraum in Kalendermonaten (Standard: 6).')
    period.add_argument('--article-days', type=int, help='Statt Kalendermonaten eine Anzahl Tage verwenden.')
    parser.add_argument('--article-keyword', action='append', default=[], help='Optionaler ODER-Filter für Titel und Text; für weitere Suchwörter wiederholen.')


def validate_arguments(parser, args):
    for name in ('gewerkschaften_max_requests', 'gewerkschaften_max_pages', 'gewerkschaften_max_search_pages',
                 'gewerkschaften_max_articles', 'article_months', 'article_days'):
        if getattr(args, name) is None:
            continue
        if not 1 <= getattr(args, name) <= 10000:
            parser.error(f'--{name.replace("_", "-")} muss zwischen 1 und 10000 liegen.')
    if args.companies and args.company:
        parser.error('--companies und --company sind nicht kombinierbar.')


def write_pdf_index(company, folder, sites, articles):
    """Quelldownloads und tatsächliche Firmennennungen getrennt ausweisen."""
    rows = []
    retained_urls = {article['url'] for article in articles if article.get('format') == 'pdf'}
    for site in sites:
        manifest = Path(site['manifest'])
        if not manifest.exists():
            continue
        data = json.loads(manifest.read_text(encoding='utf-8'))
        for document in data.get('documents', []):
            text_path = Path(document['text_path']) if document.get('text_path') else None
            text = text_path.read_text(encoding='utf-8') if text_path and text_path.exists() else ''
            match = company_matches(dict(title=document.get('title', ''), text=text), [company]).get(company.key)
            retained = document['url'] in retained_urls
            reason = (None if retained else 'no_company_mention' if not match and document['status'] == 'extracted'
                      else document.get('reason') or 'duplicate_or_article_limit')
            rows.append(dict(source=site['name'], title=document.get('title'), url=document['url'],
                             pdf_path=document['pdf_path'], text_path=document.get('text_path'),
                             document_path=str(Path(document['pdf_path']).parent / 'document.json'),
                             status=document['status'], published_at=document.get('published_at'),
                             link_description=document.get('link_description', ''),
                             company_match=match, retained_as_article=retained, excluded_reason=reason))
    path = folder / 'checked-pdfs.json'
    if rows or articles:
        write_json(path, dict(company_key=company.key, scope='all_downloads_on_matching_source_sites', documents=rows))
    return dict(download_policy='company_in_link_metadata_or_filename',
                downloaded_on_sources=len(rows), mentioning_company=sum(bool(row['company_match']) for row in rows),
                retained_as_articles=sum(article.get('format') == 'pdf' for article in articles),
                index_path=str(path) if rows or articles else None)


def remove_empty_search_folder(folder):
    """Leere Metadatenergebnisse entfernen; vorhandene Artikel und sonstige Dateien erhalten."""
    if folder.is_symlink():
        return False
    if not folder.exists():
        return True
    entries = list(folder.rglob('*'))
    metadata = {folder / 'snapshot.json', folder / 'checked-pdfs.json'}
    if any(path.is_symlink() or (not path.is_dir() and path not in metadata) for path in entries):
        return False
    for path in sorted(entries, key=lambda path: len(path.parts), reverse=True):
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    folder.rmdir()
    return True


def collect_sites(companies, args, plan, *, provider):
    root = args.output / provider
    today = datetime.now(ZoneInfo('Europe/Berlin')).date()
    start, today = publication_range(args, today)
    print(f'{provider}: Veröffentlichungszeitraum {start} bis {today} (einschließlich)', flush=True)
    batch = dict(provider=provider, date_from=start.isoformat(), date_to=today.isoformat(),
                 companies={}, sites=[], requests_attempted=0, cache_hits=0,
                 max_requests=args.gewerkschaften_max_requests)
    memo = {}
    found = {company.key: [] for company in companies}
    # Alle Fachquellen zuerst; die Reserve wird anhand dieses Ergebnisses gewählt.
    plan = sorted(plan, key=lambda item: item.get('phase') == 'fallback')
    fallback_keys = None
    primary_keys = {key for item in plan if item.get('phase') != 'fallback' for key in item['company_keys']}
    transport_class = SiteHTTP
    transport_options = {}
    if getattr(args, 'gewerkschaften_browser_fallback', False):
        from .union_browser import UnionBrowserHTTP
        transport_class = UnionBrowserHTTP
        transport_options['browser_channel'] = getattr(args, 'browser_channel', None)
    with transport_class(output=root, max_requests=args.gewerkschaften_max_requests,
                         cache_only=getattr(args, 'gewerkschaften_cache_only', False),
                         **transport_options) as transport:
        for index, item in enumerate(plan):
            association = item['source']
            phase = item.get('phase', 'primary')
            if phase == 'fallback' and fallback_keys is None:
                fallback_keys = {key for key, articles in found.items() if not articles}
            selected = [key for key in item['company_keys'] if phase != 'fallback' or key in fallback_keys]
            if not selected:
                continue
            targets = [company for company in companies if company.key in selected]
            site_name = association.source_name or association.name
            folder = root / '_sites' / company_folder(site_name)
            if phase == 'fallback':
                folder = folder / 'fallback'
            print(f'Firmensuche ({phase}): {", ".join(selected)} @ {site_name}', flush=True)
            if association.source_note:
                print(f'  Quelle: {association.source_note}', flush=True)
            # Einen Anteil des Anfragelimits für die folgenden Quellen reservieren.
            remaining = args.gewerkschaften_max_requests - transport.requests
            allowance = max(1, remaining // (len(plan) - index)) if remaining else 0
            transport.max_requests = min(args.gewerkschaften_max_requests, transport.requests + allowance)
            try:
                result = crawl_union(transport, association, folder, args, memo=memo, companies=targets,
                                       provider=provider, today=today)
            except Exception as error:
                result = dict(status='error', articles=[], error=f'{type(error).__name__}: {error}')
                write_json(folder / 'manifest.json', result)
                write_json(folder / 'error.json', result)
            finally:
                transport.max_requests = args.gewerkschaften_max_requests
            record = dict(name=site_name, organization=association.name, status=result['status'], company_keys=selected,
                          phase=phase,
                          manifest=str(folder / 'manifest.json'), article_count=len(result['articles']),
                          searches=result.get('searches', []), truncated=result.get('truncated', False))
            if phase == 'fallback':
                record['fallback_reasons'] = {key: 'no_primary_articles' if key in primary_keys else 'no_matching_primary_source'
                                              for key in selected}
            record['pdf_count'] = len(result.get('documents', []))
            record['search_mode'] = result.get('search_mode', 'unavailable')
            record['page_coverage'] = result.get('page_coverage', {})
            for article in result['articles']:
                for key in article['company_matches']:
                    found[key].append(article)
            if result.get('error'):
                record['error'] = result['error']
            batch['sites'].append(record)
            batch.update(requests_attempted=transport.requests, cache_hits=transport.cache_hits)
            write_json(root / 'batch.json', batch)
            print(f'  {status_text(result["status"])}: {len(result["articles"])} passende Artikel; '
                  f'{record["pdf_count"]} PDF-Downloads aus der Quelle (für alle zugeordneten Unternehmen gemeinsam); '
                  f'HTTP {transport.requests}/{transport.max_requests}', flush=True)
    return write_company_results(companies, args, batch, found, provider=provider, start=start, today=today)


def write_company_results(companies, args, batch, found, *, provider, start, today):
    """Ergebnisse jeder Firma schreiben, auch ohne Treffer oder passende Quelle."""
    root = args.output / provider
    index = [f'# {provider}', '', f'Zeitraum: {start} – {today}', '',
             'Ergebnisse nach Unternehmen. `_sites` und `cache` enthalten gemeinsame Quelldiagnosen.', '',
             '| Unternehmen | Status | Artikel |', '|---|---|---:|']
    for company in companies:
        matching = [row for row in batch['sites'] if company.key in row['company_keys']]
        links, seen_urls, seen_text = [], set(), set()
        company_output = root / company_folder(company.key)
        capped = False
        retained = []
        for article in sorted(found[company.key], key=lambda row: row['published_at'], reverse=True):
            if article['url'] in seen_urls or article['content_hash'] in seen_text or any(same_article(article, old) for old in retained):
                continue
            if len(links) >= args.gewerkschaften_max_articles:
                capped = True
                break
            seen_urls.add(article['url'])
            seen_text.add(article['content_hash'])
            retained.append(article)
            evidence = article['company_matches'][company.key]
            saved = dict((key, value) for key, value in article.items() if key != 'company_matches')
            saved.update(company_key=company.key, company_name=company.name, relevance='company_mention', match=evidence)
            folder = company_output / 'articles' / article['id']
            if article.get('pdf_path'):
                folder.mkdir(parents=True, exist_ok=True)
                original = folder / 'original.pdf'
                shutil.copyfile(article['pdf_path'], original)
                saved.update(source_pdf_path=article['pdf_path'], pdf_path=str(original))
            write_contract_json(folder / 'article.json', saved, 'artikel_article')
            text_path = folder / 'article.txt'
            text_path.write_text(article['text'] + '\n', encoding='utf-8')
            links.append(dict((key, value) for key, value in saved.items() if key != 'text') |
                         dict(text_path=str(text_path), path=str(folder / 'article.json')))
        status = ('skipped' if not matching else 'ok' if all(row['status'] == 'complete' for row in matching)
                  else 'error' if all(row['status'] == 'error' for row in matching) and not links else 'partial')
        if capped and status == 'ok':
            status = 'partial'
        snapshot = dict(provider=provider, status=status, company=asdict(company), relevance='company_mention',
                        date_from=start.isoformat(), date_to=today.isoformat(),
                        search_terms=company_terms(company), truncated=capped or any(row['truncated'] for row in matching),
                        associations=[dict(name=row['name'], status=row['status'], manifest=row['manifest'],
                                           phase=row.get('phase', 'primary'),
                                           fallback_reason=row.get('fallback_reasons', {}).get(company.key),
                                           search_mode=row['search_mode'],
                                           searches=[s for s in row['searches'] if s['company_key'] == company.key]) for row in matching],
                        article_count=len(links), articles=links)
        snapshot['pdfs'] = write_pdf_index(company, company_output, matching, links)
        company_searches = [s for row in matching for s in row['searches'] if s['company_key'] == company.key]
        search_coverage = {state: sum(s.get('status') == state for s in company_searches)
                           for state in ('fetched', 'error', 'not_attempted', 'ignored_query')}
        search_coverage['sites_without_search'] = sum(not row['searches'] for row in matching)
        snapshot['search_coverage'] = search_coverage
        if not matching:
            missing_source = 'no_matching_gewerkschaft'
            snapshot['reason'] = missing_source if company.branches else 'missing_branche'
        elif not links:
            snapshot['reason'] = ('all_matching_sites_failed' if status == 'error' else
                                  'search_not_attempted' if company_searches and
                                  search_coverage['not_attempted'] == len(company_searches) else
                                  'search_incomplete' if status == 'partial' else
                                  'no_company_mentions_in_checked_articles')
        path = company_output / 'snapshot.json'
        snapshot = write_contract_json(path, snapshot, 'artikel_snapshot')
        overview = [f'# {company.name}', '', f'Status: {status}',
                    f'Zeitraum: {start} – {today}', f'Artikel in diesem Lauf: {len(links)}', '',
                    f'Grund: {snapshot.get("reason") or "—"}', '',
                    '[Details und Suchabdeckung](snapshot.json)', '', '## Artikel', '']
        overview.extend(f'- [{article["title"]}](articles/{article["id"]}/article.txt) — '
                        f'{article["published_at"]} · [Quelle]({article["url"]})' for article in links)
        if not links:
            overview.append('In diesem Lauf keine passenden Artikel gespeichert. Status und Suchabdeckung beachten.')
        overview.extend(['', '## Geprüfte Quellen', ''])
        overview.extend(f'- {row["name"]}: {row["status"]}' for row in matching)
        if not matching:
            overview.append('Keine passende Quelle in den Eingabetabellen zugeordnet.')
        (company_output / 'README.md').write_text('\n'.join(overview) + '\n', encoding='utf-8')
        label = company.name.replace('|', '\\|').replace('[', '\\[').replace(']', '\\]')
        index.append(f'| [{label}]({company_output.name}/README.md) | {status} | {len(links)} |')
        batch['companies'][company.key] = dict(status=status, reason=snapshot.get('reason'),
                                                path=str(path),
                                                search_coverage=search_coverage,
                                                article_count=len(links))
    write_json(root / 'batch.json', batch)
    (root / 'README.md').write_text('\n'.join(index) + '\n', encoding='utf-8')
    return batch['companies']
