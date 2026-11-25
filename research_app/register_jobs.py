"""Unternehmensregister: Firmen auswählen, Veröffentlichungen suchen und herunterladen."""
from dataclasses import asdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import re
import sys

from .unternehmensregister import UnternehmensregisterClient
from .cli import ArgumentParser, status_text
from .research_inputs import company_folder, select_companies, write_json
from .output_contract import write_contract_json

CATEGORIES = ('finanzberichte', 'bekanntmachungen')


def publication_window(years=2, today=None):
    """Das laufende Berliner Kalenderjahr und die gewählten Vorjahre einschließen."""
    today = today or datetime.now(ZoneInfo('Europe/Berlin')).date()
    start = date(today.year - years, 1, 1)
    return start, today


def date_is_in_window(value, start, end):
    try:
        return start <= date.fromisoformat(value) <= end
    except (TypeError, ValueError):
        return False  # Ohne Datum lässt sich die Aktualität nicht belegen.


def publication_data(report, index):
    return dict(asdict(report), category=report.category, index=index)


def company_name_matches(query, returned_name):
    """Vollständige Namensteile verlangen; keine rechtliche Identitätsprüfung."""
    def normalize(value):
        return ' ' + ' '.join(re.findall(r'\w+', value.casefold())) + ' '
    return bool(query.strip()) and normalize(query) in normalize(returned_name)


def run_company(client, company, output, args):
    """Registerbereiche getrennt durchsuchen und Ergebnisse je Kategorie speichern."""
    categories = CATEGORIES if args.category == 'both' else (args.category,)
    publications, seen, searches, excluded = [], set(), [], []
    start, end = publication_window(getattr(args, 'years', 2))
    excluded_dates = []
    # Finanzberichte separat suchen, damit viele Bekanntmachungen sie nicht verdrängen.
    scopes = [True] if args.category == 'finanzberichte' else [True, False]
    for accounting_only in scopes:
        reports = client.search(company.name, location=company.location,
                                max_pages=args.max_pages, accounting_only=accounting_only,
                                date_from=start, date_to=end)
        searches.append({'accounting_only': accounting_only, 'url': client.page.url,
                         'pages_read': client.pages_read, 'has_more': client.has_more})
        for report in reports:
            # Finanzberichte wurden bereits im ersten Durchgang erfasst. Da sich die
            # Abruf-URLs ändern können, auch die Veröffentlichungsdaten vergleichen.
            identity = (report.company, report.title, report.publication_date, report.language)
            if not company_name_matches(company.name, report.company):
                excluded.append(dict(asdict(report), reason='company_name_mismatch'))
                continue
            if not date_is_in_window(report.publication_date, start, end):
                excluded_dates.append(dict(asdict(report), reason='outside_publication_window_or_unknown_date'))
                continue
            if report.category in categories and identity not in seen:
                seen.add(identity)
                publications.append(report)

    publications.sort(key=lambda report: report.publication_date, reverse=True)
    maximum = getattr(args, 'max_publications', 30)
    truncated = len(publications) > maximum
    publications = publications[:maximum]
    search_complete = not truncated and not any(search['has_more'] for search in searches)
    coverage = dict(search_complete=search_complete,
                    more_pages=any(search['has_more'] for search in searches),
                    publication_limit_reached=truncated)
    manifest = {'company': asdict(company), 'searched_at': datetime.now(timezone.utc).isoformat(),
                'searches': searches, 'excluded_company_matches': excluded,
                'date_from': start.isoformat(), 'date_to': end.isoformat(),
                'excluded_dates': excluded_dates, 'max_publications': maximum, 'truncated': truncated,
                'coverage': coverage,
                'publications': [publication_data(r, i) for i, r in enumerate(publications, 1)]}
    manifest = write_contract_json(output / 'publications.json', manifest, 'register_publications')
    for category in categories:
        rows = [row for row in manifest['publications'] if row['category'] == category]
        write_json(output / category / 'publications.json', rows)
        print(f'{company.key}: {category}: {len(rows)} Veröffentlichungen', flush=True)
    if args.open is not None:
        if args.open > len(publications):
            raise ValueError('--open ist größer als die Anzahl der Ergebnisse.')
        report = publications[args.open - 1]
        client.access(report, output / report.category / f'publication-{args.open}')
    if not args.download:
        # Keine alten Downloadzuordnungen als Teil des neuen Suchlaufs ausgeben.
        write_contract_json(output / 'downloads.json', [], 'register_downloads')
        return dict(status=('partial' if not search_complete else 'searched' if publications else 'no_results'),
                    count=len(publications), coverage=coverage)
    selected = list(enumerate(publications, 1))
    if args.report_index is not None:
        selected = [item for item in selected if item[0] == args.report_index]
        if not selected:
            raise ValueError('--report-index ist größer als die Anzahl der Ergebnisse.')
    else:
        # Das Downloadlimit je Kategorie anwenden.
        limited = []
        for category in categories:
            matching = [item for item in selected if item[1].category == category]
            limited.extend(matching[:args.max_reports])
        selected = limited
    for category in categories:
        write_json(output / category / 'downloads.json', [])
    results = []
    for index, report in selected:
        print(f'Download {company.key}/{report.category}/{index}: {report.title}', flush=True)
        result = client.download(report, output / report.category / f'publication-{index}')
        result.update(index=index, category=report.category, company=report.company, title=report.title)
        # Ausgelesene Texte bei den Originalen speichern,
        # statt sie in jeder Sammelübersicht zu vervielfältigen.
        documents = result.pop('documents', [])
        document_path = output / report.category / f'publication-{index}' / 'documents.json'
        write_contract_json(document_path, documents, 'register_documents')
        result['documents_path'] = str(document_path)
        results.append(result)
        write_contract_json(output / 'downloads.json', results, 'register_downloads')
        write_contract_json(output / report.category / 'downloads.json',
                            [r for r in results if r['category'] == report.category], 'register_downloads')
        print(f'  {status_text(result["status"])}', flush=True)
    if not selected:
        write_json(output / 'downloads.json', [])
    downloaded = sum(r['status'] in ('downloaded', 'downloaded_no_text') for r in results)
    coverage.update(downloads_selected=len(selected), downloads_succeeded=downloaded,
                    download_limit_reached=len(selected) < len(publications),
                    downloads_complete=downloaded == len(publications),
                    text_complete=all(r['status'] == 'downloaded' for r in results))
    manifest['coverage'] = coverage
    write_contract_json(output / 'publications.json', manifest, 'register_publications')
    status = ('incomplete' if downloaded < len(selected) else
              'partial' if not search_complete or not coverage['downloads_complete'] or not coverage['text_complete'] else
              'complete' if publications else 'no_results')
    return dict(status=status, count=len(publications), attempted=len(results), coverage=coverage)


def make_parser():
    parser = ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--company', help='Ein einzelnes Unternehmen statt der Firmenliste durchsuchen.')
    source.add_argument('--sources', type=Path, help='Firmenliste als CSV (Standard: data/sources.csv).')
    parser.add_argument('--company-key', help='Nur diesen company_key aus der Firmenliste bearbeiten.')
    parser.add_argument('--location', default='', help='Ort für --company; in der CSV gilt company_location.')
    parser.add_argument('--category', choices=('both', *CATEGORIES), default='both')
    parser.add_argument('--max-pages', type=int, default=1, help='Maximale Seitenzahl je Suche und Unternehmen.')
    parser.add_argument('--years', type=int, default=2, help='Laufendes Jahr und diese Anzahl Vorjahre ab 1. Januar einschließen (Standard: 2).')
    parser.add_argument('--max-publications', type=int, default=30, help='Maximale Veröffentlichungsanzahl je Unternehmen über beide Kategorien (Standard: 30).')
    parser.add_argument('--max-reports', type=int, default=3, help='Downloadlimit je Kategorie und Unternehmen.')
    parser.add_argument('--output', type=Path, default=Path('output/unternehmensregister'))
    parser.add_argument('--browser-channel', choices=('chrome', 'msedge'),
                        help='Optional Chrome oder Edge festlegen; sonst Chromium oder installierten Windows-Browser wählen.')
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--report-index', type=int, help='Eine Veröffentlichung anhand ihrer Nummer in publications.json herunterladen.')
    parser.add_argument('--open', type=int, help='Eine Veröffentlichung im sichtbaren Browser öffnen (nur ein Unternehmen).')
    parser.add_argument('--list-companies', action='store_true', help='Ausgewählte Unternehmen ohne Browser- oder Netzwerkzugriff anzeigen.')
    return parser


def validate_args(parser, args):
    for option in ('max_pages', 'max_reports', 'max_publications', 'years', 'report_index', 'open'):
        if getattr(args, option) is not None and getattr(args, option) < 1:
            parser.error(f'--{option.replace("_", "-")} muss größer als null sein.')
    if args.years > 100:
        parser.error('--years darf höchstens 100 sein.')
    if args.download and args.open is not None:
        parser.error('--download und --open sind nicht kombinierbar.')
    if args.report_index and not args.download:
        parser.error('--report-index benötigt --download.')
    if args.company and args.company_key:
        parser.error('--company-key ist nur für Firmenlisten zulässig.')
    if args.location and not args.company:
        parser.error('--location benötigt --company; in der CSV gilt company_location.')


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    try:
        companies = select_companies(args)
        if not companies:
            raise ValueError('Keine aktiven Unternehmen ausgewählt.')
        if (args.open or args.report_index) and len(companies) != 1:
            parser.error('--open und --report-index benötigen genau ein ausgewähltes Unternehmen.')
        if args.list_companies:
            for company in companies:
                print(json.dumps(asdict(company), ensure_ascii=False))
            return 0
        summary = []
        with UnternehmensregisterClient(headless=args.open is None, browser_channel=args.browser_channel) as client:
            for company in companies:
                folder = args.output / company_folder(company.key)
                try:
                    result = run_company(client, company, folder, args)
                except Exception as exc:
                    result = {'status': 'error', 'error': f'{type(exc).__name__}: {exc}'}
                    write_json(folder / 'error.json', result)
                    print(f'{company.key}: {result["error"]}', file=sys.stderr)
                summary.append(dict(result, company=asdict(company), output=str(folder)))
                write_json(args.output / 'batch.json', summary)
        return 2 if any(row['status'] in ('error', 'incomplete', 'partial') for row in summary) else 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Unternehmensregister: {exc}', file=sys.stderr)
        return 1
