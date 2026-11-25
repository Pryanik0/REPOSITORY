"""Kununu-HTML über Zyte abrufen: ein POST je Seite, ohne automatische Wiederholung."""
import base64
from datetime import datetime, timezone
import os
from pathlib import Path

from bs4 import BeautifulSoup
import httpx

from .kununu_parser import (MAX_REVIEWS, ScrapeError, aggregate_factor_scores, block_kind,
                           has_review_button, missing_review_navigation, next_review_page, parse_profile, parse_reviews,
                           review_ids, review_window, reviews_require_login)
from .research_inputs import company_folder, profile_url

ENDPOINT = 'https://api.zyte.com/v1/extract'


def expand_factor_actions(limit):
    # React lädt die Sterne für eingeklappte Teilnoten erst beim Öffnen.
    return [{'action': 'evaluate', 'source': '''(async () => {
        const cards = [...document.querySelectorAll('article[data-testid^="review-"]')]
            .filter(card => card.querySelector('time[datetime]')).slice(0, LIMIT);
        for (const card of cards) {
            const button = card.querySelector('[data-testid="review-collapsible-factors-toggle"]');
            if (button && button.textContent.trim() === 'Alle anzeigen') {
                button.click();
                await new Promise(resolve => setTimeout(resolve, 200));
            }
        }
    })();'''.replace('LIMIT', str(limit))}]


def load_more_actions(clicks):
    """Jeder Zyte-Aufruf startet frisch; bestätigte Button-Klicks bis zum Ziel wiederholen."""
    actions = []
    for _ in range(clicks):
        actions.extend([
            {'action': 'waitForSelector', 'selector': {'type': 'css',
                'value': 'article[data-testid^="review-"] time[datetime]', 'state': 'attached'}, 'timeout': 10},
            {'action': 'evaluate', 'source': '''(() => {
                document.querySelectorAll('article[data-testid^="review-"]').forEach(card => {
                    card.setAttribute('data-research-seen', 'true');
                });
            })();'''},
            {'action': 'click', 'selector': {'type': 'css', 'value': '#reviews-read-more-cta'}},
            {'action': 'waitForSelector', 'selector': {'type': 'css',
                'value': 'article[data-testid^="review-"]:not([data-research-seen]) time[datetime]',
                'state': 'attached'}, 'timeout': 10},
        ])
    return actions


def configured_key():
    """Nur ZYTE_API_KEY aus der Umgebung oder .env lesen; keine Datei ausführen."""
    key = os.environ.get('ZYTE_API_KEY', '').strip()
    if key:
        return key
    path = Path('.env')
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            name, separator, value = line.strip().partition('=')
            if separator and name.strip() == 'ZYTE_API_KEY':
                return value.strip().strip('\"\'')
    return None


class KununuZyte:
    backend = 'zyte'

    def __init__(self, *, api_key=None, max_requests=None, output=Path('output'),
                 timeout=60, max_reviews=MAX_REVIEWS, ip_type='auto', country=None, client=None):
        self.api_key = api_key or configured_key()
        if not self.api_key:
            raise ValueError('Für Zyte ZYTE_API_KEY als Umgebungsvariable oder in .env eintragen.')
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests <= 0:
            raise ValueError('--zyte-max-requests ausdrücklich angeben: maximales Anfragelimit für diesen Aufruf.')
        self.max_requests = max_requests
        self.requests_made = 0
        self.account_error = None
        self.output = Path(output)
        self.timeout = timeout
        if isinstance(max_reviews, bool) or not isinstance(max_reviews, int) or not 0 <= max_reviews <= MAX_REVIEWS:
            raise ValueError(f'max_reviews muss zwischen 0 und {MAX_REVIEWS} liegen.')
        self.max_reviews = max_reviews
        if ip_type not in ('auto', 'datacenter', 'residential'):
            raise ValueError('Zyte-IP-Typ auto, datacenter oder residential verwenden.')
        if country is not None:
            country = country.upper()
            if len(country) != 2 or not country.isascii() or not country.isalpha():
                raise ValueError('Einen zweistelligen Zyte-Ländercode verwenden, z. B. DE.')
        self.ip_type = ip_type
        self.country = country
        self.client = client
        self.owns_client = client is None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.client is not None and self.owns_client:
            self.client.close()

    def redact(self, value):
        return str(value).replace(self.api_key, '[redacted]').replace(
            base64.b64encode((self.api_key + ':').encode()).decode(), '[redacted]')

    def fetch(self, url, root, path, metadata, *, actions=None):
        metadata.update(requested_url=url, transport='zyte', html_path=str(path))
        if profile_url(url) != root:
            raise ScrapeError('invalid_profile_url', 'Nur das eingetragene öffentliche Kununu-Profil darf abgerufen werden.')
        if self.account_error:
            raise ScrapeError('zyte_account_error', self.account_error)
        if self.requests_made >= self.max_requests:
            raise ScrapeError('request_limit_reached', f'Zyte-Anfragelimit erreicht ({self.max_requests} für diesen Aufruf). '
                              'Kein weiterer API-Aufruf ausgeführt.')
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout, follow_redirects=False)
        # Auch fehlgeschlagene Versuche zählen; POST nach Zeitüberschreitung nicht wiederholen.
        self.requests_made += 1
        metadata['api_request_number'] = self.requests_made
        request = {'url': url, 'browserHtml': True, 'javascript': True}
        if self.max_reviews and '/kommentare' in url:
            actions = [*(actions or []), *expand_factor_actions(self.max_reviews)]
        if actions:
            request['actions'] = actions
        if self.ip_type != 'auto':
            request['ipType'] = self.ip_type
        if self.country:
            request['geolocation'] = self.country
        metadata['request_options'] = {key: value for key, value in request.items() if key != 'url'}
        try:
            response = self.client.post(ENDPOINT, auth=(self.api_key, ''),
                                        json=request)
        except httpx.HTTPError as error:
            raise ScrapeError('zyte_request_failed', self.redact(error)) from error
        metadata['api_status'] = response.status_code
        try:
            payload = response.json()
        except ValueError as error:
            raise ScrapeError('zyte_invalid_response', f'Zyte lieferte HTTP {response.status_code} ohne JSON-Antwort.') from error
        if not isinstance(payload, dict):
            raise ScrapeError('zyte_invalid_response', 'Zyte lieferte ein unerwartetes Antwortformat.')
        if response.status_code != 200:
            detail = self.redact(payload.get('detail') or payload.get('title') or 'Anfrage fehlgeschlagen')[:2000]
            metadata['api_error_type'] = self.redact(payload.get('type') or '')
            message = f'Zyte API HTTP {response.status_code}: {detail}'
            if response.status_code in (401, 402, 403):
                self.account_error = message
            raise ScrapeError('zyte_api_error', message)
        html = payload.get('browserHtml')
        if not isinstance(html, str) or not html.strip():
            raise ScrapeError('zyte_empty_html', 'Zyte lieferte kein Browser-HTML; dies gilt nicht als leeres Arbeitgeberprofil.')
        final_url = payload.get('url')
        status = payload.get('statusCode')
        metadata.update(final_url=final_url, http_status=status,
                        observed_at=datetime.now(timezone.utc).isoformat())
        if actions:
            performed = payload.get('actions', [])
            metadata['actions'] = [{key: self.redact(item[key]) if key == 'error' else item[key]
                                    for key in ('action', 'status', 'error') if key in item}
                                   for item in performed if isinstance(item, dict)]
            metadata['actions_complete'] = (len(performed) == len(actions) and
                                            all(item.get('status') == 'success' for item in performed))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding='utf-8')
        soup = BeautifulSoup(html, 'html.parser')
        metadata.update(title=soup.title.get_text(' ', strip=True) if soup.title else None,
                        html_bytes=len(html.encode('utf-8')))
        if profile_url(final_url) != root:
            metadata['suggested_profile_url'] = profile_url(final_url)
            metadata['needs_profile_review'] = True
            raise ScrapeError('unexpected_redirect', 'Die Zyte-Zieladresse entspricht nicht dem eingetragenen Arbeitgeberprofil.')
        challenge = block_kind(soup)
        if challenge:
            metadata['block_kind'] = challenge
        if challenge or status in (401, 403, 405, 429):
            detail = {'aws_waf_javascript': 'AWS-WAF-JavaScript-Prüfung',
                      'aws_waf_captcha': 'AWS WAF CAPTCHA'}.get(challenge, 'gesperrte Seite')
            raise ScrapeError('blocked', f'Zyte lieferte {detail} (Zielseite HTTP {status}). '
                              'HTML wurde gespeichert. Automatischer Zugriff nicht möglich.')
        if not isinstance(status, int) or not 200 <= status < 300:
            raise ScrapeError('http_error', f'Unerwarteter HTTP-Status der Zielseite: {status}.')
        return html

    def research(self, company):
        since, until = review_window()
        result = dict(provider='kununu', backend=self.backend, status='error', query=company.name,
                      profile=None, reviews=[], review_limit=self.max_reviews,
                      review_period=dict(since=since.isoformat(), until=until.isoformat(),
                                         date_field='published_at', missing_dates='excluded'),
                      factor_scores_aggregated={},
                      coverage='company_profile_only', diagnostics=dict(pages=[]))
        try:
            if not company.kununu_url:
                result.update(status='skipped', reason='missing_kununu_url')
            else:
                root = profile_url(company.kununu_url)
                if not root:
                    raise ScrapeError('invalid_profile_url', 'Die Adresse eines öffentlichen Kununu-Arbeitgeberprofils verwenden.')
                result['source_url'] = root
                folder = self.output / 'kununu' / company_folder(company.key) / self.backend
                failures = []
                metadata = {}
                # Die Bewertungsseite enthält auch die Profilkennzahlen. Bei einer
                # Bewertungsstichprobe direkt dort beginnen: kein zweiter Aufruf
                # nur für dieselben Kennzahlen und keine unnötige WAF-Kontaktstelle.
                if not self.max_reviews:
                    metadata = dict(page='profile')
                    result['diagnostics']['pages'].append(metadata)
                    try:
                        html = self.fetch(root, root, folder / 'profile.html', metadata)
                        result['profile'] = parse_profile(html, root)
                        result['profile_source'] = root
                    except Exception as error:
                        metadata.update(status='error', reason=getattr(error, 'reason', 'request_failed'),
                                        error=self.redact(error))
                        failures.append(metadata)
                permanent_profile_failure = (metadata.get('reason') in ('unexpected_redirect', 'unexpected_profile', 'blocked')
                                             or metadata.get('http_status') in (404, 410)
                                             or self.account_error is not None)
                if permanent_profile_failure:
                    result['review_stop_reason'] = 'profile_unavailable'
                if self.max_reviews and not permanent_profile_failure:
                    url = root + '/kommentare'
                    visited, seen, observed_ids = set(), set(), set()
                    clicks, page = 0, 0
                    while url and len(result['reviews']) < self.max_reviews:
                        request_identity = (url, clicks)
                        if request_identity in visited:
                            raise ScrapeError('repeated_review_page', 'Bewertungsnavigation führt zu einer bereits besuchten Seite.')
                        visited.add(request_identity)
                        page += 1
                        metadata = dict(page='reviews', page_number=page, load_more_clicks=clicks)
                        result['diagnostics']['pages'].append(metadata)
                        try:
                            filename = 'reviews.html' if page == 1 else f'reviews-{page}.html'
                            html = self.fetch(url, root, folder / filename, metadata,
                                              actions=load_more_actions(clicks) if clicks else None)
                            final_url = metadata.get('final_url') or url
                            reviews = parse_reviews(html, root, MAX_REVIEWS, since=since, until=until)
                            login_required = reviews_require_login(html)
                            identities = review_ids(html, root)
                            identities.update(review.get('id') or review.get('url') for review in reviews
                                              if review.get('id') or review.get('url'))
                            if page > 1 and not identities.difference(observed_ids) and not login_required:
                                if clicks and not metadata.get('actions_complete'):
                                    raise ScrapeError('review_actions_incomplete',
                                                      'Nachladeaktion fehlgeschlagen; keine neuen Bewertungskarten erhalten.')
                                raise ScrapeError('review_navigation_no_progress',
                                                  'Keine neuen Bewertungskarten nach der Navigation; Abruf beendet.')
                            observed_ids.update(identities)
                            # Profilkennzahlen bei Bedarf aus der Bewertungsseite übernehmen.
                            if result['profile'] is None:
                                try:
                                    result['profile'] = parse_profile(html, root)
                                    result['profile_source'] = url
                                except ScrapeError as error:
                                    metadata['profile_parse_error'] = str(error)
                            for review in reviews:
                                identity = review.get('id') or review.get('url') or (review.get('title'), review['content'])
                                if identity not in seen and len(result['reviews']) < self.max_reviews:
                                    seen.add(identity)
                                    result['reviews'].append(review)
                            result['coverage'] = 'bounded_employee_review_sample_last_2_years_not_all_reviews'
                            metadata['reviews_collected'] = len(result['reviews'])
                            if len(result['reviews']) >= self.max_reviews:
                                result['review_stop_reason'] = 'sample_limit_reached'
                                break
                            if login_required:
                                raise ScrapeError('reviews_login_required',
                                                  'Weitere Bewertungen erfordern eine Anmeldung bei Kununu; '
                                                  'öffentlich lesbare Bewertungen wurden gespeichert.')
                            if clicks and not metadata.get('actions_complete'):
                                raise ScrapeError('review_actions_incomplete',
                                                  'Bewertungen teilweise gelesen; Browseraktionen nicht vollständig ausgeführt.')
                            count = (result['profile'] or {}).get('review_count')
                            if not reviews and count != 0 and not identities:
                                raise ScrapeError('unusable_reviews', 'Keine lesbaren datierten Mitarbeiterbewertungen gefunden. '
                                                  'Gespeichertes Bewertungs-HTML prüfen.')
                            next_url = next_review_page(html, root, current_url=final_url)
                            if has_review_button(html):
                                # Keine erfundenen /kommentare/N-URLs: den tatsächlichen Button bedienen.
                                clicks += 1
                            elif next_url:
                                url, clicks = next_url, 0
                            elif not clicks and missing_review_navigation(html, root):
                                raise ScrapeError('review_navigation_unavailable',
                                                  'Weitere Bewertungen gemeldet, aber keine bestätigte Navigation gefunden.')
                            else:
                                result['review_stop_reason'] = 'end_of_available_reviews'
                                url = None
                            if url and page >= 5:
                                raise ScrapeError('review_page_limit_reached',
                                                  'Maximal fünf Bewertungsabrufe je Firma erreicht; Stichprobe unvollständig.')
                        except Exception as error:
                            metadata.update(status='error', reason=getattr(error, 'reason', 'request_failed'),
                                            error=self.redact(error))
                            failures.append(metadata)
                            result['review_stop_reason'] = metadata['reason']
                            break
                if not failures and result['profile'] is None:
                    result.update(status='partial' if result['reviews'] else 'error',
                                  reason='unusable_profile',
                                  error='Keine lesbaren Profilkennzahlen; verfügbare Bewertungen wurden gespeichert.')
                elif failures:
                    result.update(status='partial' if result['profile'] or result['reviews'] else 'error',
                                  reason=failures[0]['reason'],
                                  error='; '.join(f'{page["page"]}: {page["error"]}' for page in failures))
                else:
                    result['status'] = 'ok'
        except Exception as error:
            result.update(status='partial' if result['profile'] else 'error',
                          reason=getattr(error, 'reason', 'request_failed'), error=self.redact(error))
        result['factor_scores_aggregated'] = aggregate_factor_scores(result['reviews'])
        scored = sum(bool(review.get('factor_scores')) for review in result['reviews'])
        result['factor_score_coverage'] = dict(
            reviews_with_scores=scored, reviews_without_scores=len(result['reviews']) - scored,
            status='complete' if scored == len(result['reviews']) and scored else
                   'partial' if scored else 'unavailable' if result['reviews'] else 'no_reviews')
        result['request_limits'] = dict(max_requests_per_command=self.max_requests,
                                        requests_attempted=self.requests_made, client_retries=0)
        return result
