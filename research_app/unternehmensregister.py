"""Öffentliche Registersuche und Dokumentabruf im Browser; zusätzliche CAPTCHAs können den Abruf stoppen."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .register_documents import extract_documents, publication_document
from .browser import browser_channel

BASE_URL = 'https://www.unternehmensregister.de'
SEARCH_URL = BASE_URL + '/de/suche/rechnungslegung'
ALL_PUBLICATIONS_URL = BASE_URL + '/de/suche/alle-bereiche'


def is_financial_document(report):
    """Registerbereich berücksichtigen; Befreiungen und Hinweise gesondert einordnen."""
    title = clean_text(report.title).casefold()
    # Diese Offenlegungsbefreiungen sind Bekanntmachungen, auch wenn das Register sie
    # als Jahresabschluss führt. Nur den Titel, keine Gesetzeszitate im Bericht prüfen.
    if re.search(r'\b264\s*b\b|\b264\s*(?:abs(?:atz)?\.?\s*3\b|\(\s*3\s*\))', title):
        return False
    if title.startswith(('hinweis', 'bekanntmachung', 'befreiung', '§')):
        return False
    return bool(re.match(r'rechnungslegung\s*/\s*finanzberichte\b',
                         clean_text(report.source_area), re.IGNORECASE))


@dataclass
class FinancialReport:
    company: str
    title: str
    publication_date: str | None
    language: str | None
    url: str
    metadata: str
    source_area: str = 'Rechnungslegung / Finanzberichte'

    @property
    def category(self):
        return 'finanzberichte' if is_financial_document(self) else 'bekanntmachungen'


def clean_text(value):
    return ' '.join(value.replace('\xad', '').split())


def parse_reports(html, page_url, *, accounting_only=True):
    """Veröffentlichungen mit ihrem ursprünglichen Registerbereich auslesen."""
    soup = BeautifulSoup(html, 'html.parser')
    reports, seen = [], set()
    for row in soup.select('tr'):
        metadata = clean_text(row.get_text(' ', strip=True))
        if accounting_only and 'Rechnungslegung / Finanzberichte' not in metadata:
            continue
        cells = row.find_all('td', recursive=False)
        if not cells:
            continue
        for link in row.select('a[href]'):
            url = urljoin(page_url, link['href'])
            parts = urlsplit(url)
            if (parts.hostname != 'www.unternehmensregister.de'
                    or parts.path != '/de/publication'
                    or 'payload' not in parse_qs(parts.query) or url in seen):
                continue
            date = re.search(r'Datum\s*:\s*(\d{2}\.\d{2}\.\d{4})', metadata)
            language = re.search(r'Sprache\s*:\s*(\S+)', metadata)
            reports.append(FinancialReport(
                company=clean_text(cells[0].get_text(' ', strip=True)),
                title=clean_text(link.get_text(' ', strip=True)),
                publication_date=datetime.strptime(date[1], '%d.%m.%Y').date().isoformat() if date else None,
                language=language[1] if language else None,
                url=url, metadata=metadata,
                source_area=clean_text(cells[1].get_text(' ', strip=True)) if len(cells) > 1 else '',
            ))
            seen.add(url)
    return reports


def next_page_url(html, page_url):
    current = int(parse_qs(urlsplit(page_url).query).get('from', ['0'])[0])
    candidates = []
    for link in BeautifulSoup(html, 'html.parser').select('a[href]'):
        url = urljoin(page_url, link['href'])
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if parts.hostname != 'www.unternehmensregister.de' or parts.path not in ('/de/search', '/de/suche'):
            continue
        offset = query.get('from', [''])[0]
        if offset.isdigit() and int(offset) > current:
            candidates.append((int(offset), url))
    return min(candidates)[1] if candidates else None


class UnternehmensregisterClient:
    """Browser während Suche und Dokumentabruf als Kontext geöffnet halten."""

    def __init__(self, *, headless=True, browser_channel=None, timeout=30000):
        self.headless = headless
        self.browser_channel = browser_channel
        self.timeout = timeout

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(
                channel=browser_channel(self.playwright, self.browser_channel), headless=self.headless)
            self.context = self.browser.new_context(accept_downloads=True)
            self.page = self.context.new_page()
            self.page.set_default_timeout(self.timeout)
            return self
        except BaseException:
            self.playwright.stop()
            raise

    def __exit__(self, *exc):
        self.browser.close()
        self.playwright.stop()

    def search(self, company, *, location='', max_pages=1, accounting_only=True,
               date_from=None, date_to=None):
        if not company.strip() or max_pages < 1:
            raise ValueError('Firmennamen und max_pages >= 1 angeben.')
        from playwright.sync_api import TimeoutError
        page = self.page
        page.goto(SEARCH_URL if accounting_only else ALL_PUBLICATIONS_URL)
        # Next.js kann Eingabefelder vor der React-Initialisierung anzeigen.
        # Zu frühes Ausfüllen kann einen leeren Firmennamen absenden.
        page.wait_for_function('''() => {
            const input = document.querySelector('input[name="companyName"]');
            return input && Object.keys(input).some(key => key.startsWith('__reactProps$'));
        }''')
        cookies = page.get_by_role('button', name='Nur technisch notwendige Cookies akzeptieren')
        try:
            cookies.click(timeout=2000)
        except TimeoutError:
            pass  # Die Cookie-Auswahl ist für diese Sitzung bereits gespeichert.
        page.wait_for_load_state('networkidle')
        page.locator('input[name=companyName]').fill(company)
        page.locator('input[name=companyName]').press('Tab')
        if location:
            page.locator('input[name=companyLocation]').fill(location)
        if date_from is not None and date_to is not None:
            for index, value in enumerate((date_from, date_to)):
                for field, number in (('day', value.day), ('month', value.month), ('year', value.year)):
                    control = page.get_by_test_id(f'date_input_{field}_text').nth(index)
                    control.fill(str(number))
                    control.press('Tab')
        page.get_by_role('button', name='Suchen', exact=True).click()
        page.get_by_text('Suchergebnis', exact=True).first.wait_for()
        submitted = parse_qs(urlsplit(page.url).query).get('companyName', [''])[0]
        if submitted != company:
            raise RuntimeError('Die Website hat den Firmenfilter nicht übernommen; ungefilterte Ergebnisse werden verworfen.')
        reports, seen = [], set()
        self.pages_read = 0
        self.has_more = False
        for _ in range(max_pages):

            html = page.content()
            for report in parse_reports(html, page.url, accounting_only=accounting_only):
                if report.url not in seen:
                    reports.append(report)
                    seen.add(report.url)
            self.pages_read += 1
            following = next_page_url(html, page.url)
            self.has_more = following is not None
            if not following or self.pages_read >= max_pages:
                break
            page.goto(following)
            page.get_by_text('Suchergebnis', exact=True).first.wait_for()
        if not reports:
            text = clean_text(page.locator('body').inner_text()).lower()
            if not any(word in text for word in ('keine', '0 treffer', '0 ergebnisse')):
                raise RuntimeError('Keine Veröffentlichungszeilen erkannt; Website auf Layoutänderung oder Zugangssperre prüfen.')
        return reports

    def access(self, report, destination):
        """Veröffentlichung sichtbar öffnen und manuelle CAPTCHA-Eingabe ermöglichen."""
        if self.headless:
            raise ValueError('Der manuelle Dokumentzugriff benötigt ein sichtbares Browserfenster (headless=False).')
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        downloads = []

        def save_download(download):
            name = Path(download.suggested_filename.replace('\\', '/')).name
            target = destination / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-' + name)
            download.save_as(target)
            downloads.append(str(target))

        def watch(page):
            page.on('download', save_download)

        watch(self.page)
        self.context.on('page', watch)
        self.page.goto(report.url)
        print('Veröffentlichung im Browser öffnen und gegebenenfalls CAPTCHA lösen.')
        print('Das Originaldokument dort herunterladen; Downloads werden automatisch gespeichert.')
        input('Sobald der Bericht sichtbar ist, hier Enter drücken, um HTML und Text zu speichern: ')
        # Während der Konsoleneingabe aufgelaufene Browserereignisse verarbeiten.
        self.page.wait_for_timeout(250)
        pages = [page for page in self.context.pages if not page.is_closed()]
        page = pages[-1]
        text = page.locator('body').inner_text()
        if any(marker in text for marker in ('Sicherheitsabfrage', 'Ich bin ein Mensch')):
            raise RuntimeError('CAPTCHA weiterhin vorhanden; Berichtsinhalt wurde nicht gespeichert.')
        if not downloads and not re.search(r'\b(?:Bilanz|Gewinn|Geschäftsjahr|Jahresabschluss|Finanzbericht)\b', text):
            raise RuntimeError('Berichtsinhalt konnte nicht bestätigt werden. Dokument im Browser herunterladen.')
        (destination / 'publication.html').write_text(page.content(), encoding='utf-8')
        (destination / 'publication.txt').write_text(text, encoding='utf-8')
        return {'url': page.url, 'downloads': downloads,
                'html': str(destination / 'publication.html'),
                'text': str(destination / 'publication.txt')}

    def download(self, report, destination):
        """Dokumente im Browser ohne Eingabeaufforderung oder kostenpflichtigen Dienst abrufen."""
        from playwright.sync_api import TimeoutError
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        result = {'publication_url': report.url, 'status': 'error', 'files': [],
                  'documents': [], 'warnings': []}
        page = self.page
        try:
            page.goto(report.url)
            page.wait_for_load_state('networkidle')
            if page.get_by_text('Ich bin ein Mensch', exact=True).count():
                page.get_by_role('checkbox').check()
                try:
                    page.wait_for_function('''() => !document.body.innerText.includes('Ich bin ein Mensch')''',
                                           timeout=10000)
                    page.wait_for_load_state('networkidle')
                except TimeoutError:
                    result.update(status='blocked_captcha', error='Zusätzliche menschliche Bestätigung erforderlich.')
                    return result
            if 'Sicherheitsabfrage' in page.locator('body').inner_text():
                result.update(status='blocked_captcha', error='Menschliche Bestätigung erforderlich.')
                return result
            # Nach erfolgreicher Prüfung kann kurz eine leere Seite erscheinen, bevor
            # der Inhalt geladen ist. Diesen Zwischenzustand nicht als unbrauchbar einstufen.
            try:
                page.locator('[data-testid="printview"], [data-testid="to-esef"]').first.wait_for(timeout=10000)
            except TimeoutError:
                pass
            viewer_button = page.get_by_role('button', name='Zu den amtlichen Offenlegungsdateien', exact=True)
            if viewer_button.count():
                with page.expect_popup(timeout=self.timeout) as popup:
                    viewer_button.click()
                viewer = popup.value
                viewer.set_default_timeout(self.timeout)
                viewer.wait_for_load_state('domcontentloaded')
                viewer.locator('a[href*="download~link"]').first.wait_for()
                links = viewer.locator('a[href*="download~link"]')
                count = links.count()
                if count > 10:
                    raise ValueError('Zu viele Downloadlinks im Dokumentbetrachter; Abruf abgebrochen.')
                for index in range(count):
                    with viewer.expect_download(timeout=self.timeout) as event:
                        links.nth(index).click()
                    download = event.value
                    name = Path(download.suggested_filename.replace('\\', '/')).name

                    suffix = Path(name).suffix.lower()
                    if suffix not in ('.zip', '.pdf', '.xhtml', '.html', '.xml'):
                        download.cancel()
                        result['warnings'].append(f'Nicht unterstütztes Downloadformat: {name}')
                        continue
                    target = destination / f'document-{index + 1}{suffix}'
                    download.save_as(target)
                    result['files'].append({'path': str(target), 'original_name': name})
                    if target.stat().st_size > 100_000_000:
                        raise ValueError('Das heruntergeladene Dokument überschreitet das Verarbeitungslimit von 100 MB.')
                    documents, warnings = extract_documents(target.read_bytes(), name)
                    result['documents'].extend(documents)
                    result['warnings'].extend(warnings)
                result['viewer_url'] = viewer.url
            elif page.locator('[data-testid="printview"]').count():
                # Die Druckansicht kennzeichnet den geladenen Veröffentlichungsinhalt.
                content = page.locator('main').inner_html()
                diagnostic = destination / 'diagnostic.html'
                diagnostic.write_text(content, encoding='utf-8')
                result['diagnostic_html_path'] = str(diagnostic)
                record = publication_document(content)
                target = destination / 'publication.html'
                target.write_text(content, encoding='utf-8')
                result['files'].append({'path': str(target), 'original_name': 'publication.html'})
                result['documents'].append(record)
            else:
                result.update(status='unsupported', error='Kein Veröffentlichungsinhalt und kein unterstützter Dokumentbetrachter gefunden.')
                return result
            if result['documents']:
                text_path = destination / 'publication.txt'
                text_path.write_text('\n\n'.join(
                    document['name'] + '\n' + (document.get('text') or '\n'.join(
                        p['text'] for p in document.get('pages', [])))
                    for document in result['documents']), encoding='utf-8')
                result.update(status='downloaded', text_path=str(text_path))
            elif result['files']:
                result.update(status='downloaded_no_text')
            else:
                result.update(status='unsupported', error='Keine unterstützten Dateien heruntergeladen.')
            return result
        except Exception as exc:
            result.update(status='error', error=f'{type(exc).__name__}: {exc}')
            if 'diagnostic_html_path' not in result:
                try:
                    diagnostic = destination / 'diagnostic.html'
                    diagnostic.write_text(page.content(), encoding='utf-8')
                    result['diagnostic_html_path'] = str(diagnostic)
                except Exception as diagnostic_error:
                    result['warnings'].append(f'Diagnose-HTML nicht verfügbar: {type(diagnostic_error).__name__}')
            return result
