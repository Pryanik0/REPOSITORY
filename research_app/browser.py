"""Playwright-Chromium oder einen installierten Windows-Browser auswählen."""
import os
from pathlib import Path


def browser_channel(playwright, requested):
    if requested or Path(playwright.chromium.executable_path).is_file():
        return requested
    for channel, relative in [('msedge', 'Microsoft/Edge/Application/msedge.exe'),
                              ('chrome', 'Google/Chrome/Application/chrome.exe')]:
        for name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            if os.environ.get(name) and (Path(os.environ[name]) / relative).is_file():
                return channel
    return None
