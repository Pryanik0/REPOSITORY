"""Stellen auf Firmenwebsites und deren verlinkten Arbeitgeberportalen sammeln."""
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import heapq
import json
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .career_feeds import GREENHOUSE_HOSTS, discover_feed, embedded_feed_urls, greenhouse_tenant, read_feed
from .research_inputs import company_folder, normalized, write_json
from .site_http import BudgetExceeded, CrawlError, SiteHTTP, clean_url, host_key


CAREER = re.compile(r'karriere|career|(?<![a-z])stellen(?:angebote?|ausschreibungen?|markt)?|jobs?|vacanc(?:y|ies)|offene.position|open.position|join.us|jobbörse', re.I)
DETAIL = re.compile(r'/(?:jobs?|stellenangebote?|stellenanzeigen?|vacancies|positions?)/[^/?]+|[?&](?:jobid|job_id|requisitionid|reqid|jh)=[^&]+|(?:-j\d+|job-\d+|_R\d+)(?:[./?]|$)', re.I)
JOB_LIST = re.compile(r'^(?:alle |aktuelle |offene )?stellen(?:angebote|anzeigen)?(?: suchen)?$|^(?:search |view all |open |all )?jobs$|^vacancies$', re.I)
SAP_JOB = re.compile(r'^/job/[^/]+/(\d+)-([a-z]{2}_[A-Z]{2})/?$')
GENDER = re.compile(r'\b[mwdfh]\s*[/|\-]\s*[mwdfh]\s*[/|\-]\s*[mwdfh]\b', re.I)
GENERIC = re.compile(r'^(?:karriere|careers?|jobs?|stellenangebote?|offene stellen|vacancies|job search|job openings|application process|bewerbungsprozess|work with us|stellen suchen|alle (?:jobs|stellen)|mehr erfahren|read more|apply(?: now)?|jetzt bewerben|bewerben|details)(?:\s*[»›→!])?$', re.I)
CLOSED = re.compile(r'(?:diese|die) (?:stelle|position|stellenausschreibung) ist (?:leider )?(?:bereits )?(?:besetzt|vergeben|nicht mehr verfügbar)|(?:position|job|vacancy) (?:has been filled|is no longer available|is closed)|job posting (?:has )?expired', re.I)
GENERAL_APPLICATION = re.compile(r'initiativbewerbung|speculative application|talent.?pool|talent community|talent network|job.?agent|job.?alert|expression of interest', re.I)
REXX_FORM = re.compile(r'-[a-z]{2,3}-f\d+\.html$', re.I)
EMPTY = re.compile(r'(?:aktuell|derzeit|momentan) (?:haben wir |sind )?keine (?:offenen |passenden )?(?:stellen|jobs|positionen)|keine (?:offenen stellen|stellenangebote|jobs gefunden)|no (?:current |open |matching )?(?:vacancies|jobs|positions)(?: available| found| at)', re.I)
EXCLUDE_PATH = re.compile(r'/(?:impressum|privacy|datenschutz|kontakt|contact|login|anmelden|newsletter|register|registration|editmysearch|apply|application|bewerbung)(?:/|\.|$)|/(?:header|footer)\.html?$|\.(?:pdf|jpg|png|svg|zip|mp4)(?:$|\?)', re.I)
AGGREGATORS = ('linkedin.com', 'xing.com', 'indeed.com', 'indeed.de', 'stepstone.de',
               'monster.de', 'glassdoor.com', 'kununu.com', 'arbeitsagentur.de',
               'facebook.com', 'instagram.com', 'twitter.com', 'x.com', 'youtube.com', 'youtube-nocookie.com', 'vimeo.com',
               'whatsapp.com', 'wa.me', 't.me', 'jobware.de', 'stellenanzeigen.de',
               'her-career.com', 'careerservice.kit.edu', 'threads.net', 'podcasts.apple.com', 'spotify.com',
               'safelinks.protection.outlook.com')
ATS = ('personio.de', 'personio.com', 'myworkdayjobs.com', 'successfactors.eu',
       'successfactors.com', 'successfactors.jobs', 'softgarden.io', 'softgarden.de',
       'dvinci.de', 'rexx-systems.com', 'onlyfy.jobs', 'onlyfy.io', 'smartrecruiters.com',
       'teamtailor.com', 'taleo.net', 'hr4you.org', 'hr4you.de', 'csod.com',
       'umantis.com', 'prescreen.io', 'join.com', 'jobbase.io', 'icims.com', 'greenhouse.io', 'ashbyhq.com')


def domain_matches(host, domains):
    return any(host == domain or host.endswith('.' + domain) for domain in domains)


def job_key(url):
    if domain_matches(host_key(url), ('rosenberger.com',)):
        identity = dict(parse_qsl(urlsplit(url).query)).get('tx_rossapjobs_job[job]')
        if identity and identity.isdigit():
            return host_key(url), identity
    sap = SAP_JOB.fullmatch(urlsplit(url).path)
    successfactors = re.fullmatch(r'/job/[^/]+/(\d+)/?', urlsplit(url).path)
    rexx = re.search(r'-[jf](\d+)\.html$', urlsplit(url).path)
    sea_chefs = (re.fullmatch(r'/(?:[a-z]{2}/)?job/detail/[^/]+--([a-z0-9]+)/?', urlsplit(url).path)
                 if domain_matches(host_key(url), ('seachefs.com',)) else None)
    identity = sap or successfactors or rexx or sea_chefs
    return (host_key(url), identity[1]) if identity else url


def german_job_url(url):
    if domain_matches(host_key(url), ('rosenberger.com',)):
        return urlsplit(url).path.startswith('/de/')
    if domain_matches(host_key(url), ('seachefs.com',)):
        return urlsplit(url).path.startswith('/job/detail/')
    return bool(re.search(r'(?:-de_DE/?$|-de-j\d+\.html$)', urlsplit(url).path, re.I))


def career_link_url(url):
    """Sprach-/Sortiervarianten einer Suchliste behalten keine eigene Warteschlange."""
    parts = urlsplit(url)
    if parts.path.rstrip('/') != '/search':
        return url
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.casefold() not in ('locale', 'sortcolumn', 'sortdirection')]
    return urlunsplit(parts._replace(query=urlencode(query)))


def text(value):
    if isinstance(value, list):
        return ', '.join(filter(None, (text(item) for item in value)))
    if isinstance(value, dict):
        return text(value.get('name') or value.get('value') or '')
    return BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def is_type(item, kind):
    types = item.get('@type', [])
    return any(str(t).rstrip('/').rsplit('/', 1)[-1] == kind
               for t in (types if isinstance(types, list) else [types]))


def expiry_passed(value, now):
    if not value:
        return False
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return date.fromisoformat(value) < now.astimezone(ZoneInfo('Europe/Berlin')).date()
        expiry = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=ZoneInfo('Europe/Berlin'))
        return expiry < now
    except (ValueError, TypeError):
        return False


def locations(value):
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        address = item.get('address', item) if isinstance(item, dict) else item
        if isinstance(address, dict):
            label = ', '.join(filter(None, (text(address.get(k)) for k in
                                           ('addressLocality', 'addressRegion', 'addressCountry'))))
        else:
            label = text(address)
        if label and label not in result:
            result.append(label)
    return result


def parse_postings(structured, url, now, page_text='', extraction='schema_org_jobposting'):
    rows, excluded = [], []
    for item in structured:
        title = text(item.get('title'))
        raw_url = item.get('url')
        if isinstance(raw_url, dict):
            raw_url = raw_url.get('@id')
        target = clean_url(raw_url, url) if isinstance(raw_url, str) else None
        # Eine Liste ohne individuelle URLs darf nicht zu einer einzigen Stelle verschmelzen.
        if not target and len(structured) == 1:
            target = url
        if not title or not target:
            continue
        reason = ('general_application' if item.get('_general_application') or GENERAL_APPLICATION.search(title) else
                  'expired' if expiry_passed(item.get('validThrough'), now) else None)
        if len(structured) == 1 and CLOSED.search(page_text):
            reason = 'closed'
        if reason:
            excluded.append(dict(url=target, title=title, reason=reason))
            continue
        rows.append(dict(title=title, url=target, description=text(item.get('description')),
                         locations=locations(item.get('jobLocation')),
                         employment_type=text(item.get('employmentType')),
                         remote=text(item.get('jobLocationType')),
                         employer=text(item.get('hiringOrganization')),
                         date_posted=text(item.get('datePosted')) or None,
                         valid_through=text(item.get('validThrough')) or None,
                         reference=text(item.get('identifier')) or None,
                         extraction=extraction, source_url=url,
                         availability='advertised', fetched_at=now.isoformat()))
    return rows, excluded


def parse_page(html, url, *, now=None, detail_hint=False):
    """JSON-LD und einzelne HTML-Stellenseiten; Navigationslinks sind keine Stellen."""
    now = now or datetime.now(timezone.utc)
    soup = BeautifulSoup(html, 'html.parser')
    data = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data.extend(objects(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError, RecursionError):
            continue
    structured = [item for item in data if is_type(item, 'JobPosting')]
    if not structured:
        for posting in soup.select('[itemscope][itemtype$="/JobPosting"]'):
            title = posting.select_one('[itemprop="title"]')
            description = posting.select('[itemprop="description"]')
            if title and description:
                structured.append(dict(title=title.get_text(' ', strip=True),
                                       description=' '.join(node.get_text(' ', strip=True) for node in description)))
    extraction = 'schema_org_jobposting' if any(is_type(item, 'JobPosting') for item in structured) else 'schema_org_microdata'
    main = soup.select_one('main, [role="main"], article') or soup.body or soup
    visible = BeautifulSoup(str(main), 'html.parser')
    for node in visible.select('script, style, nav, header, footer, noscript, template, '
                               '[hidden], [aria-hidden="true"], [style*="display:none"], [style*="display: none"]'):
        node.decompose()
    page_text = visible.get_text(' ', strip=True)
    rows, excluded = parse_postings(structured, url, now, page_text, extraction=extraction)
    sap = SAP_JOB.fullmatch(urlsplit(url).path)
    if sap:
        for row in rows:
            row['reference'] = row['reference'] or sap[1]
    headings = visible.find_all('h1')
    role_headings = [node for node in headings if GENDER.search(node.get_text(' ', strip=True))]
    heading = (visible.select_one('[itemprop="title"]') or
               (role_headings[0] if len(role_headings) == 1 else visible.find('h1')))
    title = heading.get_text(' ', strip=True) if heading else ''
    application_form = bool(REXX_FORM.search(urlsplit(url).path))
    if application_form:
        rows = []
        excluded.append(dict(url=url, title=title, reason='application_form'))
    if not structured and not application_form and (detail_hint or GENDER.search(title)):
        apply_link = any(re.search(r'bewerben|apply', a.get_text(' ', strip=True), re.I)
                         for a in soup.select('a[href], button'))
        if (title and not GENERIC.fullmatch(title) and not re.match(r'^(?:karriere|careers?)\b', title, re.I)
                and (GENDER.search(title) or apply_link)):
            if GENERAL_APPLICATION.search(title):
                excluded.append(dict(url=url, title=title, reason='general_application'))
            elif CLOSED.search(page_text):
                excluded.append(dict(url=url, title=title, reason='closed'))
            elif len(page_text) >= 120:
                rows.append(dict(title=title, url=url, description=page_text, locations=[],
                                 employment_type='', remote='', employer='', date_posted=None,
                                 valid_through=None, reference=None, extraction='html_job_detail',
                                 source_url=url, availability='advertised', fetched_at=now.isoformat()))
    links = []
    for node in soup.select('a[href], iframe[src], iframe[data-src], script[src]'):
        raw_url = node.get('href') or node.get('data-src') or node.get('src') or ''
        # Defekte externe Footerlinks nicht relativ an eine Stellenadresse hängen.
        if not raw_url.strip() or raw_url.strip().startswith(('#', 'www.')):
            continue
        target = clean_url(raw_url, url)
        if not target or EXCLUDE_PATH.search(urlsplit(target).path) or REXX_FORM.search(urlsplit(target).path):
            continue
        target = career_link_url(target)
        if node.name == 'a' and (node.has_attr('hreflang') or
                (host_key(target) == host_key(url) and
                 {key.casefold() for key, _ in parse_qsl(urlsplit(target).query)} == {'locale'})):
            continue
        if node.name == 'script' and not discover_feed(target):
            continue
        label = node.get('title', '') if node.name == 'iframe' else node.get_text(' ', strip=True) or node.get('title', '')
        rel = node.get('rel', [])
        pagination = ('next' in rel or bool(re.fullmatch(r'(?:next|weiter|nächste(?: seite)?|[»›→])', label, re.I)))
        detail = bool(DETAIL.search(unquote(target)) or GENDER.search(label))
        career = bool(discover_feed(target) or CAREER.search(label + ' ' + unquote(raw_url)))
        embedded = node.name in ('iframe', 'script') and domain_matches(host_key(target), ATS)
        if career or detail or pagination or embedded:
            links.append(dict(url=target, label=label, detail=detail, pagination=pagination,
                              embedded=embedded, career=career))
    for script in soup.select('script:not([src])'):
        for target in embedded_feed_urls(script.string or script.get_text()):
            links.append(dict(url=target, label='', detail=False, pagination=False, embedded=True,
                              career=True, mechanism='embedded_job_feed'))
    for node in soup.select('meta[http-equiv]'):
        if node.get('http-equiv', '').casefold() != 'refresh':
            continue
        match = re.search(r'(?:^|;)\s*url\s*=\s*(.+?)\s*$', node.get('content', ''), re.I)
        target = clean_url(match.group(1).strip('\'"'), url) if match else None
        if target:
            links.append(dict(url=target, label='', detail=False, pagination=False,
                              embedded=False, career=True, redirect='meta_refresh'))
    for item in data:
        if is_type(item, 'ListItem'):
            child = item.get('item', {})
            target = item.get('url') or (child.get('url') if isinstance(child, dict) else child)
            if (isinstance(target, str) and clean_url(target, url)
                    and (DETAIL.search(target) or GENDER.search(text(item.get('name'))))):
                links.append(dict(url=clean_url(target, url), label=text(item.get('name')),
                                  detail=True, pagination=False, embedded=False, career=False))
    dynamic_more = any(re.search(r'mehr (?:stellen|jobs)|load more|weitere (?:stellen|jobs)',
                                 b.get_text(' ', strip=True), re.I) for b in soup.select('button'))
    return dict(jobs=rows, excluded=excluded, links=links, text=page_text,
                explicit_empty=bool(EMPTY.search(page_text)), dynamic_more=dynamic_more)


def seed_urls(company):
    urls, rejected = [], []
    for value in [*company.career_urls, *company.urls]:
        target = clean_url(value)
        if not target or domain_matches(host_key(target), AGGREGATORS):
            rejected.append(dict(url=value, reason='invalid_or_job_aggregator'))
        elif target not in urls:
            urls.append(target)
    return urls, rejected


def job_sitemap(url, body, locations):
    """Die öffentliche Sitemap als Inventar der verlinkten Stellendetails lesen."""
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise CrawlError('Ungültige XML-Stellensitemap.') from error
    if root.tag.rsplit('}', 1)[-1] != 'urlset':
        raise CrawlError('Die Stellensitemap enthält keine URL-Liste.')
    prefixes = {re.sub(r'[^\w]+', '-', normalized(value)) + '-': value for value in locations}
    links = {}
    for node in root.findall('{*}url/{*}loc'):
        target = clean_url((node.text or '').strip())
        if not target or host_key(target) != host_key(url):
            continue
        path = unquote(urlsplit(target).path)
        if not DETAIL.search(path):
            continue
        segments = path.strip('/').split('/')
        if len(segments) < 2:
            continue
        location = next((value for prefix, value in prefixes.items() if segments[1].casefold().startswith(prefix)), '')
        if prefixes and not location:
            continue
        links[target] = dict(url=target, label='', detail=True, pagination=False, embedded=False, career=False,
                             location=location)
    if not links:
        raise CrawlError('Keine passenden Stellenlinks in der XML-Sitemap.')
    return dict(jobs=[], excluded=[], links=list(links.values()), text='', explicit_empty=False, dynamic_more=False)


class CareerScope:
    """Nur eingetragene Quellen und direkt verlinkte Karriereportale freigeben."""
    def __init__(self, seeds):
        self.hosts = {host_key(url) for url in seeds}
        self.paths = {}
        self.greenhouse_tenants = {}
        self.evidence = [dict(url=url, linked_from=None, mechanism='configured') for url in seeds]
        for url in seeds:
            self._tenant(url)

    def _tenant(self, url):
        host = host_key(url)
        if host in GREENHOUSE_HOSTS:
            self.greenhouse_tenants.setdefault(host, set()).add(greenhouse_tenant(url))
        # Anbieter mit Firmen im ersten Pfadsegment nicht übergreifend durchsuchen.
        if host in ('jobs.smartrecruiters.com', 'careers.smartrecruiters.com', 'join.com'):
            first = urlsplit(url).path.strip('/').split('/')[0]
            self.paths.setdefault(host, set()).add('/' + first if first else '/__no_tenant__')
        if host in ('jobs.ashbyhq.com', 'api.ashbyhq.com'):
            feed = discover_feed(url)
            prefix = ('/posting-api/job-board/' if host == 'api.ashbyhq.com' else '/')
            self.paths.setdefault(host, set()).add(prefix + feed.tenant if feed else '/__no_tenant__')

    def contains(self, url):
        host, path = host_key(url), urlsplit(url).path
        if host in self.greenhouse_tenants:
            tenant = greenhouse_tenant(url)
            return tenant is not None and tenant in self.greenhouse_tenants[host]
        return host in self.hosts and (host not in self.paths or any(
            path == prefix or path.startswith(prefix + '/') for prefix in self.paths[host]))

    def add_feed(self, feed, source):
        if not self.contains(source) or discover_feed(source) != feed:
            raise CrawlError('Stellenfeed ohne zugeordnetes Arbeitgeberportal.')
        host = host_key(feed.url)
        if feed.provider == 'greenhouse':
            prefix = f'/v1/boards/{feed.tenant}/jobs'
            self.paths.setdefault(host, set()).add(prefix)
            for portal_host in GREENHOUSE_HOSTS:
                self.hosts.add(portal_host)
                self.greenhouse_tenants.setdefault(portal_host, set()).add(feed.tenant)
        if feed.provider == 'ashby':
            self.hosts.add('jobs.ashbyhq.com')
            self.paths.setdefault('jobs.ashbyhq.com', set()).add('/' + feed.tenant)
            self.paths.setdefault(host, set()).add('/posting-api/job-board/' + feed.tenant)
        self.hosts.add(host)
        self.evidence.append(dict(url=feed.url, linked_from=source, mechanism='public_job_feed',
                                  adapter=feed.provider))

    def admit(self, link, source):
        target = link['url']
        host = host_key(target)
        if domain_matches(host, AGGREGATORS):
            return False
        if self.contains(target):
            return True
        if host in self.hosts or len(self.hosts) >= 10:
            return False
        # Neue Domains nur über Karriere-/Stellenlinks, niemals über Paginierung.
        if not (link['career'] or link['detail'] or link['embedded']):
            return False
        self.hosts.add(host)
        self._tenant(target)
        self.evidence.append(dict(url=target, linked_from=source, mechanism=link.get('mechanism', 'employer_link')))
        return self.contains(target)

    def redirect(self, source, target, mechanism='http_redirect'):
        robots = urlsplit(source).path.endswith('/robots.txt') and host_key(source) in self.hosts
        if not self.contains(source) and not robots:
            return False
        if robots:
            # robots.txt darf auf eine kanonische Domain zeigen, bleibt aber eine Textprüfung.
            mechanism = 'robots_redirect'
            if host_key(target) in self.hosts:
                return True
        was_known = self.contains(target)
        accepted = self.admit(dict(url=target, career=True, detail=False, embedded=False), source)
        if accepted and source != target:
            if not was_known:
                self.evidence[-1]['mechanism'] = mechanism
            elif not any(row['url'] == target and row['linked_from'] == source for row in self.evidence):
                self.evidence.append(dict(url=target, linked_from=source, mechanism=mechanism))
        return accepted


def fetch_career_page(url, detail_hint, http, scope, adapter_attempts, attempted_feeds, sitemap_locations=None):
    """Optionaler Feed zuerst; bei nicht aktiviertem Feed bleibt der HTML-Weg erhalten."""
    def attempt_feed(portal):
        feed = discover_feed(portal)
        if not feed or feed.url in attempted_feeds:
            return None
        attempted_feeds.add(feed.url)
        scope.add_feed(feed, portal)
        attempt = dict(provider=feed.provider, portal_url=portal, url=feed.url)
        adapter_attempts.append(attempt)
        try:
            # Ein Feed darf nicht auf einen anderen Mandanten oder eine Loginseite umleiten.
            options = {'public_job_api': True} if feed.provider == 'ashby' else {}
            response = http.get(feed.url, scope.hosts, allow_pdf=False,
                                redirect_policy=lambda source, target: scope.contains(target), **options)
            postings = read_feed(feed, response['text'])
            rows, excluded = parse_postings(postings, response['final_url'], datetime.now(timezone.utc),
                                            extraction=feed.provider + '_feed')
            attempt.update(status='ok', jobs_found=len(rows))
            parsed = dict(jobs=rows, excluded=excluded, links=[], text='', explicit_empty=not postings,
                          dynamic_more=False)
            return response, parsed
        except BudgetExceeded:
            attempt.update(status='request_limit')
            raise
        except Exception as error:
            attempt.update(status='fallback_html', error=str(error))
            return None

    result = attempt_feed(url)
    if result:
        return result
    response = http.get(url, scope.hosts, allow_pdf=False, redirect_policy=scope.redirect)
    final = response['final_url']
    if not scope.contains(final):
        raise CrawlError('Weiterleitung außerhalb des Arbeitgeberportals.')
    if sitemap_locations is not None:
        return response, job_sitemap(final, response['text'], sitemap_locations)
    if final != url:
        result = attempt_feed(final)
        if result:
            result[0]['portal_redirects'] = response.get('redirects', [])
            return result
    content_type = response.get('content_type', '').lower()
    if content_type and not any(kind in content_type for kind in ('html', 'xhtml')):
        raise CrawlError('Keine HTML-Karriereseite.')
    return response, parse_page(response['text'], final, detail_hint=detail_hint)


def covered_by_feed(url, adapter_attempts):
    """Ein vollständiger Feed ersetzt die einzelnen Portalabrufe."""
    feed = discover_feed(url)
    return bool(feed and feed.provider in ('personio', 'ashby') and any(
        attempt['url'] == feed.url and attempt['status'] == 'ok' for attempt in adapter_attempts))


def crawl_company(company, args, http):
    seeds, rejected = seed_urls(company)
    result = dict(provider='karriere', company=asdict(company), searched_at=datetime.now(timezone.utc).isoformat(),
                  status='skipped', jobs=[], pages=[], errors=[], excluded=rejected, sources=[], adapter_attempts=[],
                  coverage=dict(scope='configured_sites_and_linked_career_portals', complete=False,
                                pending_pages=0, page_limit_reached=False, job_limit_reached=False,
                                request_limit_reached=False))
    if not seeds:
        result['reason'] = 'invalid_company_urls' if rejected else 'missing_company_url'
        if rejected:
            result['status'] = 'error'
        return result
    scope = CareerScope(seeds)
    sitemaps = {url for url in seeds if urlsplit(url).path.endswith('/sitemap.xml')
                and url in {clean_url(value) for value in company.career_urls}}
    sitemap_hosts = set()
    sitemap_details = {}
    career_seeds = {clean_url(value) for value in company.career_urls}
    queue, enqueued, visited, jobs = [], set(), set(), {}
    detail_keys = {}
    unavailable, budget_pending = set(), []
    attempted_feeds = set()
    def enqueue(url, priority, detail=False):
        key = job_key(url)
        if detail and isinstance(key, tuple):
            old_url = detail_keys.get(key)
            if old_url:
                if not german_job_url(url) or german_job_url(old_url):
                    return
                queue[:] = [item for item in queue if item[2] != old_url]
                heapq.heapify(queue)
            detail_keys[key] = url
        if url not in enqueued:
            enqueued.add(url)
            heapq.heappush(queue, (priority, len(enqueued), url, detail))
    for url in seeds:
        enqueue(url, -2 if url in sitemaps else 1 if url in career_seeds else 5, bool(DETAIL.search(url)))
    explicit_empty = False
    unresolved = False
    attempts = 0
    while queue and attempts < args.career_max_pages and len(jobs) < args.career_max_jobs:
        _, _, url, detail_hint = heapq.heappop(queue)
        if url in visited or not scope.contains(url) or covered_by_feed(url, result['adapter_attempts']):
            continue
        if sitemap_hosts and url in seeds and url not in career_seeds:
            continue
        visited.add(url)
        attempts += 1
        try:
            locations = company.career_sitemap_locations if url in sitemaps else None
            response, parsed = fetch_career_page(url, detail_hint, http, scope, result['adapter_attempts'],
                                                 attempted_feeds, locations)
            final = response['final_url']
            if locations is not None:
                sitemap_hosts.add(host_key(final))
                sitemap_details.update((link['url'], link['location']) for link in parsed['links'])
            visited.add(final)
            result['pages'].append(dict(url=url, final_url=final, redirects=response.get('redirects', []), jobs_found=len(parsed['jobs']),
                                        portal_redirects=response.get('portal_redirects', []),
                                        explicit_empty=parsed['explicit_empty']))
            result['excluded'].extend(parsed['excluded'])
            for excluded in parsed['excluded']:
                key = job_key(excluded['url'])
                unavailable.add(key)
                jobs.pop(key, None)
                if detail_hint:
                    key = job_key(url)
                    unavailable.add(key)
                    jobs.pop(key, None)
            explicit_empty |= parsed['explicit_empty']
            unresolved |= parsed['dynamic_more']
            allowed = 0
            for link in parsed['links']:
                if locations is None and (url in sitemap_details or host_key(link['url']) in sitemap_hosts):
                    continue
                # Paginierung nur innerhalb eines erkannten Karrierebereichs.
                if link['pagination'] and not (CAREER.search(final) or parsed['jobs']):
                    continue
                accepted = (scope.redirect(final, link['url'], link['redirect']) if link.get('redirect') else
                            scope.admit(link, final))
                if accepted:
                    allowed += 1
                    enqueue(link['url'], -3 if link.get('redirect') or link['embedded'] else
                            0 if link['detail'] else 2 if link['pagination'] or JOB_LIST.fullmatch(link['label']) else 3,
                            link['detail'])
            for row in parsed['jobs']:
                if not row['locations'] and sitemap_details.get(url):
                    row['locations'] = [sitemap_details[url]]
                if not scope.contains(row['url']):
                    result['excluded'].append(dict(url=row['url'], reason='outside_employer_scope'))
                    unresolved = True
                    continue
                key = job_key(row['url'])
                if key in unavailable:
                    continue
                old = jobs.get(key)
                prefer_german = (old and isinstance(key, tuple)
                                 and german_job_url(row['url']) and not german_job_url(old['url']))
                if not old or prefer_german or (row['url'] == old['url'] and row['extraction'] == 'schema_org_jobposting'):
                    if len(jobs) < args.career_max_jobs or old:
                        jobs[key] = row
                    else:
                        result['coverage']['job_limit_reached'] = True
            if not parsed['jobs'] and not parsed['explicit_empty'] and not parsed['excluded'] and allowed == 0:
                unresolved = True
                result['errors'].append(dict(url=final, reason='no_extractable_jobs_or_career_links',
                                             error='Keine auslesbaren Stellen; JavaScript oder manuelle Prüfung erforderlich.'))
        except BudgetExceeded as error:
            budget_pending.append(url)
            result['coverage']['request_limit_reached'] = True
            result['errors'].append(dict(url=url, reason='request_limit', error=str(error)))
            break
        except Exception as error:
            result['errors'].append(dict(url=url, reason='fetch_failed', error=str(error)))
    pending = budget_pending + [url for _, _, url, _ in queue
                               if url not in visited and not covered_by_feed(url, result['adapter_attempts'])]
    coverage = result['coverage']
    coverage.update(pending_pages=len(pending), pages_attempted=attempts, pages_read=len(result['pages']),
                    page_limit_reached=bool(pending and attempts >= args.career_max_pages),
                    job_limit_reached=coverage['job_limit_reached'] or bool(pending and len(jobs) >= args.career_max_jobs),
                    dynamic_or_unparsed_pages=unresolved)
    if sitemap_hosts and company.career_sitemap_locations:
        coverage['sitemap_locations'] = company.career_sitemap_locations
    result['sources'] = scope.evidence
    result['pending_urls'] = pending
    all_jobs = list(jobs.values())
    keywords = [normalized(k) for k in args.career_keyword]
    result['jobs'] = [row for row in all_jobs if not keywords or any(
        keyword in normalized(row['title'] + ' ' + row['description']) for keyword in keywords)]
    result['jobs_before_keyword_filter'] = len(all_jobs)
    incomplete = bool(pending or result['errors'] or unresolved or coverage['job_limit_reached'])
    terminal_evidence = bool(all_jobs or explicit_empty or any(
        row['reason'] in ('expired', 'closed', 'general_application') for row in result['excluded']))
    if not terminal_evidence and not incomplete:
        incomplete = True
        coverage['dynamic_or_unparsed_pages'] = True
        result['errors'].append(dict(url=result['pages'][-1]['final_url'], reason='no_extractable_job_data',
                                     error='Keine auslesbaren Stellen oder belegte Leermeldung; manuelle Prüfung erforderlich.'))
    coverage['complete'] = not incomplete and terminal_evidence
    result['status'] = ('partial' if incomplete and result['pages'] else 'error' if incomplete else
                        'ok' if result['jobs'] else 'no_results' if coverage['complete'] else 'partial')
    if not result['jobs']:
        result['reason'] = ('keyword_filter_no_match' if all_jobs else 'explicit_no_open_jobs' if explicit_empty
                            else 'no_current_jobs_in_checked_pages' if coverage['complete'] else 'search_incomplete')
    return result


def add_arguments(parser):
    parser.add_argument('--career-url', help='Offizielle Karriereadresse; genau eine Firma auswählen.')
    parser.add_argument('--list-career-sites', action='store_true', help='Karriere-Startlinks ohne Netzwerk anzeigen.')
    parser.add_argument('--career-max-pages', type=int, default=150, help='Maximale Seiten je Firma (Standard: 150).')
    parser.add_argument('--career-max-jobs', type=int, default=300, help='Maximale Stellen je Firma (Standard: 300).')
    parser.add_argument('--career-max-requests', type=int, help='HTTP-Limit für den gesamten Lauf; Standard je Firma: zweimal das Seitenlimit plus zehn.')
    parser.add_argument('--career-cache-only', action='store_true', help='Nur aktuelle Cacheantworten auswerten; keine Netzwerkanfragen.')
    parser.add_argument('--career-browser-fallback', action='store_true',
                        help='Bei HTTP 403/500 zusätzlich Browser-HTML laden; keine CAPTCHA-Lösung.')
    parser.add_argument('--career-keyword', action='append', default=[], help='Optionaler ODER-Filter auf Stellentitel und Text; wiederholbar.')


def validate_arguments(parser, args):
    for option in ('career_max_pages', 'career_max_jobs', 'career_max_requests'):
        value = getattr(args, option)
        if value is not None and value < 1:
            parser.error('--' + option.replace('_', '-') + ' muss größer als null sein.')
    if args.career_url and (not clean_url(args.career_url) or domain_matches(host_key(args.career_url), AGGREGATORS)):
        parser.error('--career-url muss eine offizielle Firmen-/Karriereadresse sein, keine Jobbörse.')


def collect_karriere(companies, args):
    root = args.output / 'karriere'
    batch = dict(provider='karriere', started_at=datetime.now(timezone.utc).isoformat(), companies={})
    index = ['# Unternehmenskarriereseiten', '', '| Unternehmen | Status | Stellen | Weitere Karrieredomains |', '| --- | --- | --- | --- |']
    max_requests = args.career_max_requests or len(companies) * (2 * args.career_max_pages + 10)
    transport_class, transport_options = SiteHTTP, {}
    if getattr(args, 'career_browser_fallback', False):
        from .career_browser import CareerBrowserHTTP
        transport_class = CareerBrowserHTTP
        transport_options['browser_channel'] = getattr(args, 'browser_channel', None)
    with transport_class(output=root, max_requests=max_requests,
                         cache_only=getattr(args, 'career_cache_only', False), **transport_options) as http:
        for position, company in enumerate(companies):
            print(f'Karriereseiten: {company.name}', flush=True)
            # Spätere Firmen behalten ihren Anteil, auch wenn die erste Seite
            # sehr viele Links oder Weiterleitungen liefert. Nicht verbrauchte
            # Anfragen stehen den folgenden Firmen zur Verfügung.
            before, cache_before = http.requests, http.cache_hits
            allowance = max(0, max_requests - before) // (len(companies) - position)
            http.max_requests = before + allowance
            try:
                result = crawl_company(company, args, http)
            finally:
                http.max_requests = max_requests
            result['coverage']['http'] = dict(requests_attempted=http.requests - before,
                                             allocated_requests=allowance,
                                             cache_hits=http.cache_hits - cache_before)
            folder = root / company_folder(company.key)
            for row in result['jobs']:
                row['id'] = hashlib.sha256(row['url'].encode()).hexdigest()[:20]
                row['company_key'] = company.key
                job_folder = folder / 'jobs' / row['id']
                write_json(job_folder / 'job.json', row)
                (job_folder / 'job.txt').write_text(row['title'] + '\n\n' + row['description'] + '\n', encoding='utf-8')
            write_json(folder / 'snapshot.json', result)
            lines = [f'# {company.name}', '', f'Status: {result["status"]}; Stellen: {len(result["jobs"])}', '',
                     'Erfasst werden veröffentlichte Angebote auf den geprüften Seiten. Die vollständige Zahl aller offenen Stellen ist nicht garantiert.', '']
            if result.get('reason'):
                lines += ['Grund: ' + result['reason'], '']
            lines += ['## Karrierequellen', '']
            for source in result['sources']:
                lines.append(f'- [{md_label(source["url"])}]({source["url"]}) ({source["mechanism"]})' +
                             (f' — von {source["linked_from"]}' if source['linked_from'] else ''))
            lines += ['', '## Stellen', '']
            for row in result['jobs']:
                lines += [f'- [{md_label(row["title"])}]({row["url"]})' +
                          (f' — {md_label(row["employer"])}' if row['employer'] else '') +
                          (f' — {", ".join(row["locations"])}' if row['locations'] else '')]
            if result['errors']:
                lines += ['', '## Suchgrenzen', ''] + [f'- {r["url"]}: {r["error"]}' for r in result['errors']]
            (folder / 'README.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
            overview = dict(status=result['status'], reason=result.get('reason'), count=len(result['jobs']),
                            path=str(folder / 'snapshot.json'), coverage=result['coverage'], sources=result['sources'])
            batch['companies'][company.key] = overview
            batch['http'] = dict(requests_attempted=http.requests, max_requests=max_requests,
                                 cache_hits=http.cache_hits, allocation='remaining_budget_per_remaining_company')
            write_json(root / 'batch.json', batch)
            original_hosts = {host_key(url) for url in company.urls}
            portals = {}
            for source in result['sources']:
                host = host_key(source['url'])
                if (host not in original_hosts and source['mechanism'] != 'robots_redirect'
                        and not domain_matches(host, AGGREGATORS)):
                    portals.setdefault(host, source['url'])
            portal_links = ', '.join(f'[{host}]({url})' for host, url in portals.items()) or '—'
            index.append(f'| [{md_label(company.name)}]({folder.name}/README.md) | {result["status"]} | {len(result["jobs"])} | {portal_links} |')
            print(f'  {len(result["jobs"])} Stellen; {result["status"]}; {folder / "snapshot.json"}', flush=True)
        batch['http'] = dict(requests_attempted=http.requests, max_requests=max_requests, cache_hits=http.cache_hits,
                             allocation='remaining_budget_per_remaining_company')
    write_json(root / 'batch.json', batch)
    (root / 'README.md').write_text('\n'.join(index) + '\n', encoding='utf-8')
    return batch['companies']


def md_label(value):
    return value.replace('|', '\\|').replace('[', '\\[').replace(']', '\\]').replace('\n', ' ')
