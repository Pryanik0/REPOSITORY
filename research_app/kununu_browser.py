"""Kununu im lokalen Browser: einmalige Anmeldung, danach automatischer Firmenlauf."""
from datetime import datetime, timezone
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .browser import browser_channel
from .cli import ArgumentParser
from .kununu_parser import ScrapeError, block_kind, profile_url
from .kununu_storage import save_snapshot
from .kununu_zyte import KununuZyte
from .research_inputs import company_folder, select_companies, write_json


class KununuBrowser(KununuZyte):
    backend = 'browser'

    def __init__(self, page, *, output, max_reviews=15, max_pages=175, interactive=False):
        # Gemeinsame Parser und begrenzte Navigation; fetch verwendet niemals Zyte.
        super().__init__(api_key='unused-local-browser', max_requests=max_pages,
                         output=output, max_reviews=max_reviews)
        self.page = page
        self.loaded_url = None
        self.completed_clicks = 0
        self.interactive = interactive
        self.challenge_pause_used = False
        self.stop_requested = False

    def dismiss_overlays(self, metadata):
        """Nur bekannte UI-Dialoge bedienen, niemals Login/CAPTCHA wegklicken."""
        from playwright.sync_api import TimeoutError as BrowserTimeout
        candidates = [
            ('cookies_rejected', self.page.locator('#usercentrics-cmp-ui').get_by_role(
                'button', name=re.compile(r'^(Alle ablehnen|Alles ablehnen|Ablehnen|Nur notwendige Cookies|'
                                          r'Nur essenzielle Cookies|Reject all|Deny all)$', re.I))),
            ('pulse_promotion_closed', self.page.locator(
                'dialog[aria-labelledby="pulse-launch-promotion-headline"]').get_by_role(
                    'button', name='Alles klar!', exact=True)),
        ]
        for name, buttons in candidates:
            for button in buttons.all():
                if not button.is_visible():
                    continue
                try:
                    button.click(timeout=1500)
                    button.wait_for(state='hidden', timeout=1500)
                    metadata.setdefault('dismissed_overlays', []).append(name)
                except BrowserTimeout:
                    # Ein darüberliegender Dialog kann zuerst geschlossen werden müssen.
                    continue

    def click_load_more(self, metadata):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        deadline = time.monotonic() + 15
        while True:
            self.dismiss_overlays(metadata)
            try:
                self.page.locator('#reviews-read-more-cta').click(timeout=1000)
                return
            except BrowserTimeout:
                if time.monotonic() >= deadline:
                    raise
                # Auch nachträglich eingeblendete Overlays beim nächsten Versuch prüfen.

    def expand_review_factors(self, metadata):
        """Nachgeladene Karten rendern Teilnoten erst nach „Alle anzeigen“."""
        from playwright.sync_api import TimeoutError as BrowserTimeout
        cards = self.page.locator('article[data-testid^="review-"]').filter(
            has=self.page.locator('time[datetime]'))
        for card in cards.all()[:self.max_reviews]:
            button = card.locator('[data-testid="review-collapsible-factors-toggle"]').filter(
                has_text=re.compile(r'^\s*Alle anzeigen\s*$'))
            if not button.count() or not button.first.is_visible():
                continue
            try:
                self.dismiss_overlays(metadata)
                button.first.click(timeout=2000)
                button.first.wait_for(state='hidden', timeout=2000)
                metadata['factor_cards_expanded'] = metadata.get('factor_cards_expanded', 0) + 1
            except BrowserTimeout as error:
                metadata.setdefault('factor_expansion_errors', []).append(str(error)[:500])

    def fetch(self, url, root, path, metadata, *, actions=None):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        if profile_url(url) != root:
            raise ScrapeError('invalid_profile_url', 'Unerwartete Profiladresse.')
        if self.requests_made >= self.max_requests:
            raise ScrapeError('request_limit_reached', 'Browser-Seitenlimit erreicht.')
        self.requests_made += 1
        metadata.update(requested_url=url, transport='local_browser', html_path=str(path),
                        browser_step=self.requests_made)
        target_clicks = sum(action['action'] == 'click' for action in actions or [])
        response = None
        if self.loaded_url != url or not actions:
            response = self.page.goto(url, wait_until='domcontentloaded', timeout=45000)
            self.loaded_url, self.completed_clicks = url, 0
        try:
            self.page.locator('h1, article[data-testid^="review-"], #reviews-gate-cta-button').first.wait_for(timeout=10000)
        except BrowserTimeout:
            pass
        self.validate_page(root, path, metadata, response)
        if actions:
            metadata['actions_complete'] = False
            try:
                while self.completed_clicks < target_clicks:
                    self.click_load_more(metadata)
                    self.completed_clicks += 1
                    # Vorherige Karten sind markiert; nur neue Karten bedeuten Fortschritt.
                    self.page.wait_for_function('''() =>
                        document.querySelector('[data-testid="reviews-gate"], #reviews-gate-cta-button') ||
                        [...document.querySelectorAll('article[data-testid^="review-"]')]
                        .some(e => !e.hasAttribute('data-research-seen') && e.querySelector('time[datetime]'))
                    ''', timeout=15000)
                    if self.page.locator('[data-testid="reviews-gate"], #reviews-gate-cta-button').count():
                        break
                    self.mark_cards()
                metadata['actions_complete'] = self.completed_clicks == target_clicks
            except BrowserTimeout as error:
                metadata['action_error'] = str(error)[:1000]
        self.expand_review_factors(metadata)
        html = self.validate_page(root, path, metadata)
        self.mark_cards()
        return html

    def mark_cards(self):
        self.page.evaluate('''() => document.querySelectorAll('article[data-testid^="review-"]')
            .forEach(e => e.setAttribute('data-research-seen', 'true'))''')

    def validate_page(self, root, path, metadata, response=None):
        html = self.page.content()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding='utf-8')
        metadata.update(final_url=self.page.url, observed_at=datetime.now(timezone.utc).isoformat())
        if response is not None:
            metadata['http_status'] = response.status
        challenge = block_kind(BeautifulSoup(html, 'html.parser'))
        if challenge:
            metadata['block_kind'] = challenge
            if self.interactive and not self.challenge_pause_used:
                self.challenge_pause_used = True
                metadata['manual_challenge_pause'] = True
                print('CAPTCHA: Sammlung pausiert auf dieser Firma. Bitte im Browser lösen, '
                      'auf die Bewertungsseite warten und dann hier Enter drücken. '
                      'Bei erneuter Sperre wird der Lauf beendet.', flush=True)
                try:
                    input()
                except EOFError:
                    self.stop_requested = True
                    raise ScrapeError('blocked', 'Keine interaktive Eingabe möglich; Lauf beendet.')
                # Nicht neu laden: die vom Benutzer freigegebene Seite übernehmen.
                # Der frühere HTTP-Status gehört zur CAPTCHA, nicht zur neuen Seite.
                html = self.validate_page(root, path, metadata)
                metadata['challenge_resolved_manually'] = True
                return html
            self.stop_requested = True
            raise ScrapeError('blocked', 'Sicherheitsabfrage nicht gelöst oder erneut aufgetreten; '
                              'Lauf beendet, übrige Firmen nicht abgerufen.')
        if profile_url(self.page.url) != root:
            metadata['suggested_profile_url'] = profile_url(self.page.url)
            raise ScrapeError('unexpected_redirect', 'Browser auf einem anderen Profil oder einer Anmeldeseite.')
        if response is not None and response.status >= 400:
            raise ScrapeError('http_error', f'Browser HTTP {response.status}.')
        return html

    def research(self, company):
        result = super().research(company)
        result['request_limits'] = dict(max_browser_steps=self.max_requests,
                                        browser_steps=self.requests_made, zyte_requests=0)
        return result


def collection_page_after_login(page, context):
    """Login-Weiterleitung abschließen und die Sammlung in einer eigenen Seite starten."""
    def returned_to_site(url):
        parts = urlsplit(url)
        return (parts.hostname == 'www.kununu.com' and
                not re.search(r'/(login|signin|register|registration)(/|$)', parts.path, re.I))
    page.wait_for_url(returned_to_site, wait_until='domcontentloaded', timeout=45000)
    # Späte Weiterleitungen der Login-Seite können diese Seite nicht unterbrechen.
    return context.new_page()


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, default=Path('data/sources.csv'))
    parser.add_argument('--company-key')
    parser.add_argument('--output', type=Path, default=Path('output/kununu-local'))
    parser.add_argument('--profile-dir', type=Path, default=Path('data/browser-profiles/kununu'))
    parser.add_argument('--browser-channel', choices=['chrome', 'msedge'])
    parser.add_argument('--login', action='store_true', help='Einmalige manuelle Anmeldung, dann automatische Sammlung.')
    parser.add_argument('--headless', action='store_true', help='Gespeicherte Sitzung ohne sichtbares Fenster verwenden.')
    parser.add_argument('--kununu-reviews', type=int, default=15)
    args = parser.parse_args(argv)
    if not 0 <= args.kununu_reviews <= 15:
        parser.error('--kununu-reviews muss zwischen 0 und 15 liegen.')
    if args.login and args.headless:
        parser.error('--login benötigt ein sichtbares Browserfenster.')
    args.company, args.location = None, ''
    companies = select_companies(args)
    from playwright.sync_api import sync_playwright
    batch = dict(started_at=datetime.now(timezone.utc).isoformat(), backend='browser', companies=[])
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(args.profile_dir.resolve()), channel=browser_channel(playwright, args.browser_channel),
            headless=args.headless, locale='de-DE', viewport=dict(width=1350, height=950))
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if args.login:
                page.goto('https://www.kununu.com/de/login', wait_until='domcontentloaded', timeout=45000)
                print('Einmal im Browser anmelden und ggf. Cookie-Dialog schließen. Danach hier Enter drücken.', flush=True)
                input()
                page = collection_page_after_login(page, context)
            collector = KununuBrowser(page, output=args.output, max_reviews=args.kununu_reviews,
                                       max_pages=max(1, len(companies)*5), interactive=not args.headless)
            for index, company in enumerate(companies):
                print(f'Kununu Browser: {company.key}', flush=True)
                result = collector.research(company)
                path = args.output/'kununu'/company_folder(company.key)/'snapshot.json'
                result = save_snapshot(path, result)
                batch['companies'].append(dict(company=company.key, status=result['status'],
                                                reason=result.get('reason'), reviews=len(result['reviews']), path=str(path)))
                write_json(args.output/'batch.json', batch)
                print(f'  {result["status"]}: {len(result["reviews"])} Bewertungen; {result.get("reason", "ok")}', flush=True)
                if collector.stop_requested:
                    batch['stop_reason'] = 'blocked'
                    batch['not_attempted_companies'] = [c.key for c in companies[index+1:]]
                    write_json(args.output/'batch.json', batch)
                    print('Lauf wegen CAPTCHA beendet. Übrige Firmen und ihre bisherigen Ergebnisse bleiben unverändert.', flush=True)
                    break
        finally:
            context.close()
    return 0 if all(c['status'] in ('ok', 'skipped') for c in batch['companies']) else 1
