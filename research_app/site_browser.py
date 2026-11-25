"""Begrenzter Browserabruf öffentlicher Seiten nach HTTP-Fehlern."""
from datetime import datetime, timezone
import hashlib
import time
from urllib.parse import urljoin, urlsplit

from .browser import browser_channel
from .research_inputs import write_json
from .site_http import BudgetExceeded, CrawlError, MAX_BYTES, SiteHTTP, clean_url, host_key


class SiteBrowserHTTP(SiteHTTP):
    fallback_hosts = frozenset()

    def __init__(self, *, browser_channel=None, javascript_enabled=True, **kwargs):
        super().__init__(**kwargs)
        self.requested_channel = browser_channel
        self.javascript_enabled = javascript_enabled
        self._playwright = self._browser = self._context = None
        self.browser_attempts = []

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            try:
                if self._playwright:
                    self._playwright.stop()
            finally:
                super().__exit__(*exc)

    def _start_browser(self):
        if self._context is None:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True, channel=browser_channel(self._playwright, self.requested_channel))
            # Isoliertes Profil, keine Benutzer-Cookies. DJV rendert die Liste per JavaScript.
            self._context = self._browser.new_context(service_workers='block',
                                                       java_script_enabled=self.javascript_enabled)
        return self._context

    def _raw(self, url, allowed_hosts, redirect_check=None, *, allow_pdf=True, redirect_policy=None):
        result = super()._raw(url, allowed_hosts, redirect_check, allow_pdf=allow_pdf,
                              redirect_policy=redirect_policy)
        if (self.cache_only or result['status'] not in (403, 500)
                or host_key(url) not in self.fallback_hosts
                or urlsplit(url).path.endswith(('/robots.txt', '.pdf'))
                or 'pdf' in result.get('content_type', '').lower()):
            return result
        attempt = dict(url=url, http_status=result['status'])
        try:
            browser_result = self._browser_raw(url, allowed_hosts, redirect_check, redirect_policy)
            attempt.update(browser_status=browser_result['status'], final_url=browser_result['final_url'])
            if browser_result['status'] != 200:
                raise CrawlError(f'Browser HTTP {browser_result["status"]}: {url}; '
                                 'Seite nicht verfügbar oder Sicherheitsprüfung erforderlich.')
            text = browser_result.get('text', '').lower()
            if '<title>just a moment' in text or 'id="challenge-form"' in text:
                raise CrawlError(f'Browser-Sicherheitsprüfung erforderlich: {url}')
            cache = self.output / 'cache' / (hashlib.sha256(url.encode()).hexdigest() + '.json')
            write_json(cache, browser_result)
            return browser_result
        except Exception as error:
            attempt['error'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            self.browser_attempts.append(attempt)
            write_json(self.output / 'browser-attempts.json', self.browser_attempts)

    def wait_for_content(self, page, url):
        """Quellenspezifische Wartebedingung nach DOMContentLoaded."""

    def _browser_raw(self, url, allowed_hosts, redirect_check, redirect_policy):
        if self.requests >= self.max_requests:
            raise BudgetExceeded('HTTP-Anfragelimit vor Browserabruf erreicht.')
        context = self._start_browser()
        page = context.new_page()
        redirects, failures, pending = [], [], []
        current = url

        def route_request(route):
            nonlocal current
            request = route.request
            document = request.resource_type == 'document' and request.is_navigation_request()
            if (request.method != 'GET' or (not document and request.resource_type != 'script')
                    or (not document and host_key(request.url) not in allowed_hosts)):
                route.abort()
                return
            target = clean_url(request.url)
            try:
                if (not target or host_key(target) not in allowed_hosts
                        or host_key(target) not in self.fallback_hosts):
                    raise CrawlError('Browser-Weiterleitung außerhalb der Quelldomains.')
                if document and target != current:
                    if redirect_policy and not redirect_policy(current, target):
                        raise CrawlError('Browser-Weiterleitung außerhalb des erlaubten Bereichs.')
                    if redirect_check:
                        redirect_check(target)
                    if len(redirects) >= 5:
                        raise CrawlError('Zu viele Browser-Weiterleitungen.')
                # Automatische Redirects umgehen keine Domain-/robots-Prüfung.
                for hop in range(6):
                    if redirect_check:
                        redirect_check(target)
                    if self.requests >= self.max_requests:
                        raise BudgetExceeded('HTTP-Anfragelimit beim Browserabruf erreicht.')
                    key = host_key(target)
                    pause = self.host_delays.get(key, self.delay) - (time.monotonic() - self.last_request.get(key, 0))
                    if pause > 0:
                        time.sleep(pause)
                    self.requests += 1
                    self.last_request[key] = time.monotonic()
                    if document:
                        current = target
                    fetched = route.fetch(url=target, max_redirects=0, max_retries=0, timeout=30000)
                    try:
                        if 300 <= fetched.status < 400 and fetched.headers.get('location'):
                            following = clean_url(urljoin(target, fetched.headers['location']))
                            if document:
                                pending.append((target, following, fetched.status))
                                route.fulfill(status=200, content_type='text/html', body='')
                                return
                            if (not following or host_key(following) not in allowed_hosts
                                    or (redirect_policy and not redirect_policy(target, following))):
                                raise CrawlError('Browser-Skriptweiterleitung außerhalb der Quelldomains.')
                            target = following
                            continue
                        body = fetched.body()
                        if len(body) > MAX_BYTES:
                            raise CrawlError('Browserantwort überschreitet 3 MB.')
                        if not document and fetched.status != 200:
                            raise CrawlError(f'Browser-Skript HTTP {fetched.status}: {target}')
                        route.fulfill(response=fetched, body=body)
                        return
                    finally:
                        fetched.dispose()
                raise CrawlError('Zu viele Browser-Skriptweiterleitungen.')
            except Exception as error:
                failures.append(error)
                route.abort()

        page.route('**/*', route_request)
        try:
            target = url
            while True:
                response = page.goto(target, wait_until='domcontentloaded', timeout=30000)
                if failures:
                    raise failures[0]
                if not pending:
                    break
                source, target, status = pending.pop()
                if (not target or host_key(target) not in allowed_hosts
                        or host_key(target) not in self.fallback_hosts
                        or (redirect_policy and not redirect_policy(source, target))):
                    raise CrawlError('Browser-Weiterleitung außerhalb der Quelldomains.')
                if len(redirects) >= 5:
                    raise CrawlError('Zu viele Browser-Weiterleitungen.')
                # Auch Schleifen auf dieselbe URL benötigen eine erneute Prüfung.
                if redirect_check:
                    redirect_check(target)
                redirects.append(dict(source=source, target=target, status=status))
            if failures:
                raise failures[0]
            if response is None:
                raise CrawlError('Browser liefert keine Dokumentantwort.')
            content_type = response.headers.get('content-type', '')
            if 'html' not in content_type.lower():
                raise CrawlError('Browser-Fallback unterstützt nur HTML.')
            if response.status == 200:
                self.wait_for_content(page, url)
            if failures:
                raise failures[0]
            html = page.content()
            if len(html.encode('utf-8')) > MAX_BYTES:
                raise CrawlError('Browserantwort überschreitet 3 MB.')
            return dict(url=url, final_url=current, redirects=redirects, status=response.status,
                        content_type=content_type, text=html,
                        fetched_at=datetime.now(timezone.utc).isoformat(), backend='browser')
        except Exception:
            if failures:
                raise failures[0]
            raise
        finally:
            page.close()
