"""Browser-Fallback für GEW und DJV."""
from urllib.parse import urlsplit

from .site_browser import SiteBrowserHTTP
from .site_http import host_key


class UnionBrowserHTTP(SiteBrowserHTTP):
    fallback_hosts = frozenset({'gew.de', 'djv.de'})

    def wait_for_content(self, page, url):
        if host_key(url) == 'djv.de' and urlsplit(url).path.rstrip('/') == '/news/pressemitteilungen':
            page.locator('a[href*="/press-detail/"]').first.wait_for(state='attached', timeout=10000)
