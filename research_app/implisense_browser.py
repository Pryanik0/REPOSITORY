"""Implisense-Anmeldung, Firmenlisten und Nachrichten; neue Recherche nur mit Kostenlimit."""
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .research_inputs import load_companies, normalized, write_json
from .cli import ArgumentParser
from .browser import browser_channel
from .output_contract import write_contract_json


ORIGIN = 'https://app.implisense.com'
DEFAULT_OUTPUT = Path('output/implisense/browser')
LIST_ID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'


def free_operation(method, path):
    """Nur die vorgesehenen Import- und Nachrichtenabrufe zulassen."""
    return ((method == 'POST' and path in ('/api/lists/upload/resolve', '/api/lists/upload/commit'))
            or (method == 'GET' and bool(re.fullmatch('/api/lists/' + LIST_ID + '/news', path))))


def prepare_sources(source, output, company_key=None):
    """Firmen und Adressmerkmale für den kostenlosen Listenimport vorbereiten."""
    companies = load_companies(source)
    if company_key:
        companies = [company for company in companies if company.key == company_key]
    if not companies:
        raise ValueError('Mindestens ein aktives Unternehmen mit ausgefülltem Namen ist erforderlich.')
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'sources-import.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['Firmenname', 'Ort', 'PLZ', 'Website', 'Branche',
                                                   'company_key', 'implisense_id'])
        writer.writeheader()
        writer.writerows({'Firmenname': company.name, 'Ort': company.location, 'PLZ': company.postal_code,
                          'Website': company.urls[0] if company.urls else '',
                          'Branche': '; '.join(company.branches), 'company_key': company.key,
                          'implisense_id': company.implisense_id} for company in companies)
    return path, len(companies)


class RequestGuard:
    """Direkte API-Zugriffe und nicht freigegebene kostenpflichtige Aufrufe sperren."""
    def __init__(self, journal=None):
        self.allowed = set()
        self.events = []
        self.journal = journal
        self.approved_refresh = None
        self.refresh_used = False

    def approve_refresh(self, list_id, quoted_cost, budget):
        if not re.fullmatch(LIST_ID, list_id) or quoted_cost <= 0 or budget < quoted_cost:
            raise ValueError('Der angezeigte Nachrichtenpreis überschreitet das freigegebene Kostenlimit.')
        self.approved_refresh = '/api/lists/' + list_id + '/news'

    def handle(self, route):
        request = route.request
        parts = urlsplit(request.url)
        method, host, path = request.method.upper(), parts.hostname, parts.path
        blocked = host == 'api.implisense.com'
        if host == 'app.implisense.com':
            operation = (method, path)
            authentication = bool(re.fullmatch(r'/(?:de|en)/sign-in(?:/[^?#]*)?', path))
            blocked = ((path == '/api' or path.startswith('/api/') or method not in ('GET', 'HEAD', 'OPTIONS'))
                       and not (operation in self.allowed and free_operation(method, path))
                       and not authentication)
            if method == 'POST' and path == self.approved_refresh and not self.refresh_used:
                payload = request.post_data_json
                if (isinstance(payload, dict) and set(payload) == {'period'}
                        and payload['period'] in (7, 30, 90)):
                    self.refresh_used = True  # Eine Wiederholung darf keine erneuten Kosten auslösen.
                    blocked = False
        if host in ('api.implisense.com', 'app.implisense.com') and (
                path == '/api' or path.startswith('/api/') or blocked or method not in ('GET', 'HEAD', 'OPTIONS')):
            # Keine Abfrageparameter, Anfragetexte, Cookies oder Header protokollieren.
            self.events.append({'method': method, 'host': host, 'path': path, 'blocked': blocked})
            if self.journal:
                write_json(self.journal, self.events)
        if blocked:
            route.abort('blockedbyclient')
        else:
            route.continue_()


def site_json(page, guard, method, path, payload=None):
    plain_path = urlsplit(path).path
    if not path.startswith('/api/') or not free_operation(method, plain_path):
        raise ValueError('Dieser Aufruf gehört nicht zu den freigegebenen kostenlosen Vorgängen.')
    operation = (method, plain_path)
    guard.allowed.add(operation)
    try:
        result = page.evaluate('''async ({method, path, payload}) => {
            const response = await fetch(path, {
                method, cache: 'no-store', signal: AbortSignal.timeout(90000),
                ...(payload === null ? {} : {
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
                })
            });
            return {status: response.status, data: await response.json()};
        }''', {'method': method, 'path': path, 'payload': payload})
    finally:
        guard.allowed.discard(operation)
    if result['status'] != 200:
        raise RuntimeError(f'Kostenloser Website-Aufruf {plain_path}: HTTP {result["status"]}; '
                           f'{result["data"].get("error", "request_failed")}')
    return result['data']


def get_lists(page):
    links = page.locator('a[href]').evaluate_all('''nodes => nodes.map(a => ({
        name: a.innerText.trim(), path: new URL(a.href, location.href).pathname
    }))''')
    lists = {}
    for link in links:
        match = re.fullmatch('/de/lists/(' + LIST_ID + ')', link['path'])
        if match and match[1] not in lists:
            lists[match[1]] = {'id': match[1], 'name': link['name'], 'path': link['path']}
    return list(lists.values())


def candidate_reason(row, candidate):
    if not candidate or not candidate.get('id'):
        return 'no_candidate'
    if not candidate.get('active', True):
        return 'inactive_candidate'
    words = r'[^\W_]+'
    original = re.findall(words, normalized(row['Firmenname']))
    matched = re.findall(words, normalized(candidate.get('name')))
    if not original or (matched != original and (
            matched[:len(original)] != original or
            ' '.join(matched[len(original):]) not in ('gmbh', 'gmbh co kg', 'ag', 'se', 'kg', 'mbh', 'ug', 'eg', 'e v'))):
        return 'different_company_name'
    if row['implisense_id']:
        if original == matched and candidate['id'] == row['implisense_id']:
            return 'verified_implisense_id'
        return 'different_implisense_id'
    city = re.sub(r'\bi\.\s*', 'im ', normalized(candidate.get('city')))
    expected_city = normalized(row['Ort'])
    same_city = bool(city and (city == expected_city or expected_city.startswith(city + ' bei ')))
    if city and expected_city and not same_city:
        return 'different_city'
    postal = normalized(candidate.get('zip') or candidate.get('postalCode'))
    expected_postal = normalized(row['PLZ'])
    if postal and expected_postal and postal != expected_postal:
        return 'different_postal_code'
    score = candidate.get('score')
    if isinstance(score, (int, float)):
        if original == matched and same_city and postal and postal == expected_postal and score >= 0.8:
            return 'verified_name_city_and_postal_code'
        if score >= 0.9:
            return 'verified_name_and_score'
    return 'low_score'


def import_sources(page, guard, args, lists, *, resolve_only=False):
    path, count = prepare_sources(args.sources, args.output, args.company_key)
    with path.open(encoding='utf-8-sig', newline='') as stream:
        originals = list(csv.DictReader(stream))
    columns = {'name': 'Firmenname', 'url': 'Website', 'city': 'Ort', 'zip': 'PLZ'}
    items = [{key: row[column] for key, column in columns.items() if row[column]} for row in originals]
    fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
    source_hash = hashlib.sha256(json.dumps(originals, sort_keys=True).encode()).hexdigest()
    state_path = args.output / 'import-state.json'
    previous = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    if (not resolve_only and previous.get('source_hash') == source_hash and previous.get('name') == args.list_name
            and any(row['id'] == previous.get('list_id') for row in lists)):
        print(f'Liste bereits importiert: {previous["list_id"]}. Keine doppelte Liste angelegt.', flush=True)
        return previous
    if not resolve_only and any(row['name'] == args.list_name for row in lists):
        raise ValueError('Eine Liste mit diesem Namen existiert bereits. Anderen --list-name wählen; nichts wurde überschrieben.')
    cache = args.output / 'resolutions' / (fingerprint + '.json')
    if cache.exists():
        resolved = json.loads(cache.read_text(encoding='utf-8'))
    else:
        resolved = []
        # Der kostenlose Listenimport erlaubt höchstens 20 Einträge je Anfrage.
        for offset in range(0, len(items), 20):
            chunk = items[offset:offset + 20]
            data = site_json(page, guard, 'POST', '/api/lists/upload/resolve', {'items': chunk})
            results = data.get('results', [])
            if len(results) != len(chunk) or data.get('failedChunks') or data.get('failedItems'):
                raise RuntimeError('Die Firmenzuordnung ist unvollständig; es wurde keine Liste angelegt.')
            resolved.extend(results)
        write_json(cache, resolved)
    if len(resolved) != len(items):
        raise RuntimeError('Die gespeicherte Firmenzuordnung passt nicht zur Anzahl der Eingabezeilen.')
    accepted, review, seen = [], [], set()
    for row, candidate in zip(originals, resolved):
        reason = candidate_reason(row, candidate)
        selected = reason.startswith('verified_')
        if selected and candidate['id'] in seen:
            selected, reason = False, 'duplicate_company_id'
        review.append({'company_key': row['company_key'], 'company_name': row['Firmenname'],
                       'city': row['Ort'], 'postal_code': row['PLZ'], 'website': row['Website'],
                       'expected_implisense_id': row['implisense_id'],
                       'matched_name': (candidate or {}).get('name'),
                       'matched_city': (candidate or {}).get('city'),
                       'matched_postal_code': (candidate or {}).get('zip') or (candidate or {}).get('postalCode'),
                       'company_id': (candidate or {}).get('id'),
                       'score': (candidate or {}).get('score'), 'selected': selected,
                       'reason': reason})
        if selected:
            seen.add(candidate['id'])
            accepted.append({'companyId': candidate['id'], 'companyName': candidate['name'],
                             'city': candidate.get('city')})
    write_json(args.output / 'match-review.json', review)
    write_csv(args.output / 'match-review.csv', review,
              ['company_key', 'company_name', 'city', 'postal_code', 'website', 'matched_name',
               'matched_city', 'matched_postal_code', 'expected_implisense_id', 'company_id', 'score', 'selected', 'reason'])
    if resolve_only:
        print(f'{len(accepted)} von {count} Unternehmen zugeordnet: {args.output / "match-review.csv"}', flush=True)
        return review
    if not accepted:
        raise RuntimeError('Keine ausreichend sicheren Zuordnungen. Siehe match-review.csv; keine Liste angelegt.')
    data = site_json(page, guard, 'POST', '/api/lists/upload/commit', {
        'name': args.list_name, 'sourceFilename': args.sources.name, 'rows': accepted})
    identity = data.get('list', {}).get('id')
    if not identity or not re.fullmatch(LIST_ID, identity):
        raise RuntimeError('Die Plattform hat keine neue Listen-ID bestätigt.')
    state = {'source': str(args.sources), 'source_hash': source_hash, 'name': args.list_name,
             'list_id': identity, 'source_rows': count, 'imported_companies': len(accepted),
             'rows_needing_review': sum(not row['selected'] for row in review),
             'url': ORIGIN + '/de/lists/' + identity, 'spent_credits': 0}
    write_json(state_path, state)
    print(f'Liste angelegt: {identity}: {len(accepted)} Unternehmen; '
          f'{state["rows_needing_review"]} Zeilen müssen geprüft werden. Keine Implicents verbraucht.', flush=True)
    return state


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def collect_news(page, guard, args, lists):
    targets = ([{'id': args.list_id, 'name': args.list_id}] if args.list_id else lists)
    collected, events, records = [], [], []
    for target in targets:
        identity = target['id']
        data = site_json(page, guard, 'GET', f'/api/lists/{identity}/news?period={args.period}')
        write_json(args.output / 'news' / identity / f'{args.period}-days.json', data)
        record = {'list_id': identity, 'list_name': target['name'], 'cached': bool(data.get('cached')),
                  'generated_at': data.get('generatedAt'), 'flags': data.get('flags'),
                  'article_count': len(data.get('items') or [])}
        records.append(record)
        collected.extend(dict(item, list_id=identity, generated_at=data.get('generatedAt'),
                              cache_flags=data.get('flags')) for item in (data.get('items') or []))
        events.extend(dict(item, list_id=identity) for item in (data.get('registerEvents') or []))
        print(f'{target["name"]}: {record["article_count"]} gespeicherte Nachrichten; '
              f'im Cache={record["cached"]}. Keine neue Recherche gestartet.', flush=True)
    snapshot = {'provider': 'implisense_browser', 'period_days': args.period,
                'fetched_at': datetime.now(timezone.utc).isoformat(), 'spent_credits': 0,
                'lists': records, 'article_count': len(collected), 'articles': collected,
                'register_events': events}
    write_contract_json(args.output / 'news.json', snapshot, 'implisense_news')
    write_csv(args.output / 'news.csv', collected,
              ['companyName', 'companyId', 'publishedAt', 'title', 'sourceName', 'sourceUrl',
               'signal', 'summaryDe', 'summaryEn', 'list_id', 'generated_at'])
    write_csv(args.output / 'register-events.csv', events,
              ['companyName', 'companyId', 'publishedAt', 'title', 'category', 'list_id'])
    return snapshot


def quote_research(page, args):
    """Den angezeigten Preis vor dem Start einer neuen Recherche lesen."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    def check_login():
        if re.match(r'^/(?:de|en)/sign-in(?:/|$)', urlsplit(page.url).path):
            raise RuntimeError('Die Listenseite leitet zur Anmeldung weiter. '
                               'Zuerst main.py implisense login ausführen, danach quote erneut starten. '
                               'Keine kostenpflichtige Recherche gestartet.')

    page.goto(ORIGIN + '/de/lists/' + args.list_id, wait_until='domcontentloaded')
    check_login()
    panel = page.locator('#list-news-panel')
    button = panel.get_by_role('button', name=re.compile(r'Neuigkeiten(?: neu)? recherchieren'))
    try:
        if not panel.is_visible():
            # Die Plattform hält die Recherche in einem zunächst versteckten Panel.
            observe = page.get_by_role('button', name=re.compile(r'^Beobachten\b'))
            observe.wait_for()
            if observe.get_attribute('aria-expanded') != 'true':
                observe.click()
            page.get_by_role('button', name=re.compile(r'^Neuigkeiten\s+Websuche')).click()
        button.wait_for()
    except PlaywrightTimeoutError as error:
        check_login()
        diagnostic_path = args.output / 'research-page-status.json'
        write_json(diagnostic_path, {
            'path': urlsplit(page.url).path,
            'news_panel_count': panel.count(),
            'visible_buttons': page.locator('button:visible').all_inner_texts(),
            'research_started': False,
        })
        raise RuntimeError('Die Schaltfläche für die Nachrichtenrecherche fehlt oder ist nicht sichtbar. '
                           f'Seitendiagnose: {diagnostic_path}. Keine kostenpflichtige Recherche gestartet.') from error
    label = button.inner_text()
    prices = re.findall(r'\b\d+\b', label)
    if len(prices) != 1:
        raise RuntimeError('Kein eindeutiger Nachrichtenpreis auf der Website erkennbar; nichts gestartet.')
    quote = {'list_id': args.list_id, 'period_days': args.period, 'maximum_implicents': int(prices[0]),
             'website_label': label, 'research_started': False}
    write_json(args.output / 'research-quote.json', quote)
    print(f'Angezeigter Höchstpreis: {quote["maximum_implicents"]} Implicents. Recherche noch nicht gestartet.', flush=True)
    return quote


def refresh_news(page, guard, args):
    quote = quote_research(page, args)
    guard.approve_refresh(args.list_id, quote['maximum_implicents'], args.max_implicents)
    # Einziger kostenpflichtiger Aufruf: ein POST ohne Wiederholung.
    result = page.evaluate('''async ({path, period}) => {
        const response = await fetch(path, {method: 'POST',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify({period})});
        return {status: response.status, data: await response.json()};
    }''', {'path': '/api/lists/' + args.list_id + '/news', 'period': args.period})
    write_json(args.output / 'research-result.json', result)
    if result['status'] != 200:
        data = result.get('data')
        reason = data.get('error') if isinstance(data, dict) else None
        detail = (f' ({reason})' if reason else '')
        if reason == 'ai_timeout':
            detail += (' Die KI-Recherche bei Implisense hat ihr Zeitlimit überschritten. '
                       'Gespeicherte Ergebnisse mit news prüfen; eine neue Recherche später erneut starten.')
        raise RuntimeError(f'Nachrichtenrecherche HTTP {result["status"]}{detail}; '
                           'keine automatische Wiederholung. Siehe research-result.json.')
    print('Recherche abgeschlossen. Berechnet: ' + str(result['data'].get('creditsCharged', 'siehe Beleg')), flush=True)
    return collect_news(page, guard, args, [])


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'login', 'lists', 'resolve', 'import', 'news', 'quote', 'research'])
    parser.add_argument('--sources', type=Path, default=Path('data/sources.csv'))
    parser.add_argument('--company-key', help='Ein Unternehmen für prepare, resolve oder import auswählen.')
    parser.add_argument('--list-name', default='sources.csv')
    parser.add_argument('--list-id', help='Nur diese Liste lesen; andernfalls alle gespeicherten Listen.')
    parser.add_argument('--period', type=int, choices=[7, 30, 90], default=90)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--max-implicents', type=int, default=0,
                        help='Kostenlimit für eine neue Nachrichtenrecherche (Standard: keine Ausgaben).')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--browser-channel', choices=['chrome', 'msedge'])
    parser.add_argument('--login-timeout', type=int, default=600)
    args = parser.parse_args(argv)
    if args.login_timeout < 1:
        parser.error('--login-timeout muss größer als null sein.')
    if args.company_key and args.action not in ('prepare', 'resolve', 'import'):
        parser.error('--company-key ist nur mit prepare, resolve oder import zulässig.')
    if args.list_id and not re.fullmatch(LIST_ID, args.list_id):
        parser.error('--list-id muss die UUID aus der Implisense-Listenadresse sein.')
    if args.headless and args.action == 'login':
        parser.error('Die Anmeldung benötigt ein sichtbares Browserfenster.')
    if args.action == 'research' and args.max_implicents <= 0:
        parser.error('Kostenpflichtige Recherche ist deaktiviert. Zuerst quote verwenden; Kosten mit --max-implicents freigeben.')
    if args.action != 'research' and args.max_implicents:
        parser.error('--max-implicents ist nur mit der Aktion research zulässig.')
    if args.action in ('quote', 'research') and not args.list_id:
        state_path = args.output / 'import-state.json'
        if state_path.exists():
            args.list_id = json.loads(state_path.read_text(encoding='utf-8')).get('list_id')
        if not args.list_id or not re.fullmatch(LIST_ID, args.list_id):
            parser.error('--list-id angeben oder zuerst eine Firmenliste importieren.')
    if not args.list_name.strip():
        parser.error('--list-name darf nicht leer sein.')
    if args.action == 'prepare':
        path, count = prepare_sources(args.sources, args.output, args.company_key)
        print(f'{count} Unternehmen vorbereitet: {path}. Keine Netzwerkanfragen.', flush=True)
        return 0
    from playwright.sync_api import sync_playwright
    args.output.mkdir(parents=True, exist_ok=True)
    guard = RequestGuard(args.output / 'requests.json')
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str((args.output / 'profile').resolve()), headless=args.headless,
            channel=browser_channel(playwright, args.browser_channel), service_workers='block', accept_downloads=True,
            locale='de-DE', viewport={'width': 1350, 'height': 950})
        try:
            context.route('**/*', guard.handle)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            page.goto(ORIGIN + '/de/lists', wait_until='domcontentloaded')
            print('Browsersitzung geöffnet. Neue kostenpflichtige Vorgänge sind gesperrt.', flush=True)
            deadline = time.monotonic() + (args.login_timeout if args.action == 'login' else 15)
            last_status = 0
            while time.monotonic() < deadline:
                if page.is_closed():
                    raise RuntimeError('Die Browserseite wurde vor Abschluss der Anmeldung geschlossen.')
                signed_in = page.evaluate('Boolean(window.Clerk && window.Clerk.user)')
                if time.monotonic() - last_status > 5:
                    write_json(args.output / 'login-progress.json', {
                        'path': urlsplit(page.url).path, 'signed_in': signed_in,
                        'blocked_count': sum(event['blocked'] for event in guard.events)})
                    last_status = time.monotonic()
                if signed_in:
                    break
                page.wait_for_timeout(500)
            else:
                raise RuntimeError('Keine aktive Anmeldung. Den Befehl implisense login erneut ausführen.')
            page.goto(ORIGIN + '/de/lists', wait_until='domcontentloaded')
            page.wait_for_timeout(2500)
            write_json(args.output / 'session-status.json', {
                'signed_in': True, 'path': urlsplit(page.url).path, 'paid_operations_enabled': False,
                'saved_at': datetime.now(timezone.utc).isoformat()})
            lists = get_lists(page)
            if args.action in ('lists', 'import', 'news'):
                write_json(args.output / 'lists.json', lists)
            if args.action == 'lists':
                for entry in lists:
                    print(entry['id'] + '  ' + entry['name'], flush=True)
            elif args.action in ('resolve', 'import'):
                import_sources(page, guard, args, lists, resolve_only=args.action == 'resolve')
            elif args.action == 'news':
                collect_news(page, guard, args, lists)
            elif args.action == 'quote':
                quote_research(page, args)
            elif args.action == 'research':
                refresh_news(page, guard, args)
            else:
                print('Anmeldung gespeichert.', flush=True)
            print('Gesperrte API-Aufrufe: ' + str(sum(e['blocked'] for e in guard.events)), flush=True)
        finally:
            write_json(args.output / 'requests.json', guard.events)
            context.close()
    return 0
