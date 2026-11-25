"""HTTP-Abrufe mit Cache, robots.txt, Weiterleitungsprüfung und gemeinsamem Anfragelimit."""
from datetime import datetime, timezone
import base64
import hashlib
import ipaddress
import json
import re
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import httpx

from .research_inputs import write_json

USER_AGENT = 'CompanyResearchBot/1.0'
MAX_BYTES = 3_000_000
MAX_PDF_BYTES = 20_000_000


class CrawlError(Exception):
    pass


class BudgetExceeded(CrawlError):
    pass


class PDFSkipped(CrawlError):
    """Binärantwort vor dem vollständigen Abruf wegen fehlendem Firmenbezug ablehnen."""


def clean_url(value, base=None):
    try:
        parts = urlsplit(urljoin(base or '', value))
        if (parts.scheme not in ('https', 'http') or not parts.hostname or parts.username
                or parts.password or parts.port not in (None, 80, 443) or '\\' in value):
            return None
        hostname = parts.hostname.lower().rstrip('.')
        if hostname == 'localhost' or '.' not in hostname:
            return None
        try:
            if not ipaddress.ip_address(hostname).is_global:
                return None
        except ValueError:
            pass
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or '/', urlencode(query), ''))
    except (TypeError, ValueError):
        return None


def host_key(url):
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')


class SiteHTTP:
    def __init__(self, *, output, max_requests=2000, cache_hours=24, delay=0.5, timeout=20, client=None,
                 cache_only=False):
        self.output = Path(output)
        self.max_requests, self.cache_hours, self.delay = max_requests, cache_hours, delay
        self.requests = self.cache_hits = 0
        self.cache_only = cache_only
        self.last_request = {}
        self.host_delays = {}
        self.robots = {}
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False,
                                            headers={'User-Agent': USER_AGENT})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.client.close()

    def _raw(self, url, allowed_hosts, redirect_check=None, *, allow_pdf=True, redirect_policy=None):
        def authorize(source, target):
            if (not clean_url(target) or (redirect_policy is not None and not redirect_policy(source, target))
                    or host_key(target) not in allowed_hosts):
                raise CrawlError('Weiterleitung außerhalb der eingetragenen Quelldomains.')
            if redirect_check:
                redirect_check(target)

        cache = self.output / 'cache' / (hashlib.sha256(url.encode()).hexdigest() + '.json')
        if cache.exists() and self.cache_hours:
            try:
                saved = json.loads(cache.read_text(encoding='utf-8'))
                old_pdf = 'pdf' in saved.get('content_type', '').lower() or saved.get('text', '').startswith('%PDF-')
                if old_pdf and 'body_b64' not in saved:
                    raise ValueError('Alter PDF-Cache enthält keine vollständigen Binärdaten.')
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(saved['fetched_at'])).total_seconds()
                if 0 <= age < self.cache_hours * 3600 and saved['url'] == url:
                    # Jede Weiterleitung auch im Cache erneut dem aktuellen Firmenscope zuordnen.
                    current = url
                    redirects = saved.get('redirects', [])
                    if not redirects and saved['final_url'] != url:
                        redirects = [dict(source=url, target=saved['final_url'])]
                    for hop in redirects:
                        if hop['source'] != current:
                            raise ValueError('Ungültige Weiterleitungskette im Cache.')
                        authorize(current, hop['target'])
                        current = hop['target']
                    if current != saved['final_url'] or host_key(current) not in allowed_hosts:
                        raise ValueError('Cacheantwort liegt außerhalb der Quelldomains.')
                    if 'body_b64' in saved and not allow_pdf:
                        raise PDFSkipped('PDF-Linktitel oder Dateiname nennt kein ausgewähltes Unternehmen.')
                    self.cache_hits += 1
                    return saved
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if self.cache_only:
            raise CrawlError('Keine aktuelle Cacheantwort; im Cachemodus ist der Netzwerkzugriff gesperrt.')
        current = url
        redirects = []
        for _ in range(6):
            if self.requests >= self.max_requests:
                raise BudgetExceeded('HTTP-Anfragelimit erreicht (Anteil der Website oder Gesamtlimit).')
            key = host_key(current)
            pause = self.host_delays.get(key, self.delay) - (time.monotonic() - self.last_request.get(key, 0))
            if pause > 0:
                time.sleep(pause)
            self.requests += 1  # Fehlversuche und Weiterleitungen zählen mit.
            self.last_request[key] = time.monotonic()
            with self.client.stream('GET', current) as response:
                if response.is_redirect:
                    target = clean_url(response.headers.get('location', ''), current)
                    if not target:
                        raise CrawlError('Weiterleitung außerhalb der eingetragenen Quelldomains.')
                    authorize(current, target)
                    redirects.append(dict(source=current, target=target, status=response.status_code))
                    current = target
                    continue
                body = bytearray()
                content_type = response.headers.get('content-type', '')
                if 'pdf' in content_type.lower() and not allow_pdf:
                    raise PDFSkipped('PDF-Linktitel oder Dateiname nennt kein ausgewähltes Unternehmen.')
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if body.startswith(b'%PDF-') and not allow_pdf:
                        raise PDFSkipped('PDF-Linktitel oder Dateiname nennt kein ausgewähltes Unternehmen.')
                    limit = MAX_PDF_BYTES if 'pdf' in content_type.lower() or body.startswith(b'%PDF-') else MAX_BYTES
                    if len(body) > limit:
                        raise CrawlError(f'Antwort überschreitet {limit // 1_000_000} MB.')
                response.encoding = response.encoding or 'utf-8'
                result = dict(url=url, final_url=current, redirects=redirects, status=response.status_code,
                              content_type=content_type,
                              fetched_at=datetime.now(timezone.utc).isoformat())
                if 'pdf' in content_type.lower() or body.startswith(b'%PDF-'):
                    result['body_b64'] = base64.b64encode(body).decode('ascii')
                else:
                    result['text'] = bytes(body).decode(response.encoding, errors='replace')
            # Nur erfolgreiche Antworten zwischenspeichern; Fehler beim nächsten Lauf erneut prüfen.
            if result['status'] == 200:
                write_json(cache, result)
            return result
        raise CrawlError('Zu viele Weiterleitungen.')

    def _robots(self, url, allowed_hosts, redirect_policy=None):
        parts = urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
        if origin not in self.robots:
            result = self._raw(origin + '/robots.txt', allowed_hosts, redirect_policy=redirect_policy)
            status = result['status']
            parser = RobotFileParser()
            if status in (404, 410):
                parser.parse([])
            elif status == 200:
                if 'body_b64' in result or '<html' in result.get('text', '')[:300].lower():
                    raise CrawlError('robots.txt liefert HTML; Zugriffsberechtigung unklar.')
                parser.parse(result['text'].splitlines())
            else:
                raise CrawlError(f'robots.txt HTTP {status}; Zugriffsberechtigung unklar.')
            self.robots[origin] = parser
        return self.robots[origin]

    def get(self, url, allowed_hosts, *, allow_pdf=True, redirect_policy=None, public_job_api=False):
        url = clean_url(url)
        if not url or host_key(url) not in allowed_hosts:
            raise CrawlError('Ungültige URL oder Domain außerhalb der eingetragenen Quelle.')

        def check(target):
            if public_job_api:
                # Ashby veröffentlicht diesen GET-Endpunkt ausdrücklich ohne Login.
                # Die allgemeine API-Domain verlangt für /robots.txt dagegen Auth.
                # https://developers.ashbyhq.com/docs/public-job-posting-api
                parts = urlsplit(target)
                if (parts.scheme != 'https' or parts.hostname != 'api.ashbyhq.com'
                        or not re.fullmatch(r'/posting-api/job-board/[A-Za-z0-9_-]+/?', parts.path)
                        or parts.query):
                    raise CrawlError('Außerhalb der dokumentierten öffentlichen Stellen-API.')
                return
            robots = self._robots(target, allowed_hosts, redirect_policy)
            if not robots.can_fetch(USER_AGENT, target):
                raise CrawlError('Durch robots.txt gesperrt.')
            crawl_delay = robots.crawl_delay(USER_AGENT) or robots.crawl_delay('*') or 0
            if crawl_delay > 60:
                raise CrawlError('robots.txt verlangt eine Wartezeit von mehr als 60 Sekunden.')
            self.host_delays[host_key(target)] = max(self.delay, crawl_delay)

        check(url)
        result = self._raw(url, allowed_hosts, check, allow_pdf=allow_pdf, redirect_policy=redirect_policy)
        if result['status'] != 200:
            raise CrawlError(f'HTTP {result["status"]}: {result["final_url"]}')
        return result
