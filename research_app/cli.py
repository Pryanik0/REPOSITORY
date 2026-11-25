"""Gemeinsame deutsche Darstellung der Kommandozeilenhilfe."""
import argparse
import re
import sys


def status_text(status):
    return {
        'ok': 'erfolgreich', 'searched': 'Suche abgeschlossen', 'complete': 'vollständig',
        'partial': 'teilweise', 'incomplete': 'unvollständig', 'error': 'Fehler',
        'skipped': 'übersprungen', 'downloaded': 'heruntergeladen',
        'downloaded_no_text': 'heruntergeladen, ohne auslesbaren Text',
        'blocked_captcha': 'CAPTCHA erforderlich', 'unsupported': 'nicht unterstützt',
        'unavailable': 'nicht verfügbar', 'no_reviews': 'keine Bewertungen',
        'no_results': 'keine Treffer im Suchzeitraum',
    }.get(status, status)


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, add_help=False, **kwargs)
        self._positionals.title = 'Befehle und Argumente'
        self._optionals.title = 'Optionen'
        self.add_argument('-h', '--help', action='help', help='Diese Hilfe anzeigen und beenden.')

    def format_help(self):
        return super().format_help().replace('usage:', 'Aufruf:', 1)

    def format_usage(self):
        return super().format_usage().replace('usage:', 'Aufruf:', 1)

    def error(self, message):
        replacements = {
            'the following arguments are required:': 'Folgende Argumente fehlen:',
            'unrecognized arguments:': 'Unbekannte Argumente:',
            'expected one argument': 'Ein Wert wird benötigt',
            'not allowed with argument': 'Nicht kombinierbar mit',
            'invalid choice:': 'Ungültige Auswahl:',
            'choose from': 'Erlaubt sind',
            'argument ': 'Argument ',
        }
        for old, new in replacements.items():
            message = message.replace(old, new)
        message = re.sub(r'invalid (\w+) value:', r'Ungültiger Wert (\1):', message)
        self.print_usage(sys.stderr)
        self.exit(2, f'{self.prog}: Fehler: {message}\n')
