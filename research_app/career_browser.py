"""Browser-HTML für blockierte, bereits zugeordnete Karrierequellen."""
from .site_browser import SiteBrowserHTTP


class CareerBrowserHTTP(SiteBrowserHTTP):
    def __init__(self, **kwargs):
        # Statisches HTML reicht z. B. für Recor; dynamische Portale benötigen Feeds.
        super().__init__(javascript_enabled=False, **kwargs)

    def get(self, url, allowed_hosts, **kwargs):
        # CareerScope hält diese Menge aktuell, auch bei belegten Weiterleitungen.
        self.fallback_hosts = allowed_hosts
        return super().get(url, allowed_hosts, **kwargs)
