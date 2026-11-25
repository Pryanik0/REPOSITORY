"""Primäre und sekundäre Recherche sowie einzelne Quellen ausführen."""
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import UUID

from .kununu_zyte import KununuZyte
from .kununu_storage import save_snapshot
from .cli import status_text
from .register_jobs import make_parser as register_parser, run_company, validate_args
from .research_inputs import company_folder, profile_url, select_companies, write_json
from .unternehmensregister import UnternehmensregisterClient
from .union_articles import add_arguments, validate_arguments
from .gewerkschaften import plan_for as union_plan_for, collect_gewerkschaften
from .karriere import (add_arguments as add_career_arguments, validate_arguments as validate_career_arguments,
                       collect_karriere, seed_urls)


SOURCE_GROUPS = {
    'primary': ('unternehmensregister', 'kununu', 'gewerkschaften', 'karriere'),
    'secondary': ('implisense',),
    'all': ('unternehmensregister', 'kununu', 'gewerkschaften', 'karriere'),
}


def uses_source(source, provider):
    return provider in SOURCE_GROUPS.get(source, (source,))


def make_parser():
    parser = register_parser()
    parser.description = __doc__
    parser.set_defaults(output=Path('output'))
    parser.add_argument('--source', choices=('all', 'primary', 'secondary', 'unternehmensregister', 'kununu', 'gewerkschaften', 'karriere'), default='all',
                        help='primary: Register, Kununu, Gewerkschaften, Karriereseiten; secondary: gespeicherte Implisense-Nachrichten')
    add_arguments(parser)
    add_career_arguments(parser)
    parser.add_argument('--kununu-url', help='Bekannte Arbeitgeberprofil-Adresse; benötigt genau ein ausgewähltes Unternehmen.')
    parser.add_argument('--zyte-max-requests', type=int,
                        help='Maximale API-Aufrufe im gesamten Lauf; einer je Profil-/Bewertungsseite, ohne Wiederholung.')
    parser.add_argument('--zyte-ip-type', choices=('auto', 'datacenter', 'residential'), default='auto',
                        help='Zyte-IP-Typ; residential benötigt Geschäftskonto-Verifizierung und kann andere Kosten verursachen.')
    parser.add_argument('--zyte-country', type=str.upper,
                        help='Optionaler zweistelliger Ländercode, z. B. DE; sonst wählt Zyte.')
    parser.add_argument('--kununu-timeout', type=int, default=60,
                        help='Zeitlimit je Zyte-API-Aufruf in Sekunden.')
    parser.add_argument('--kununu-reviews', type=int, default=15,
                        help='Maximale Mitarbeiterbewertungen aus den letzten zwei Jahren (0–15); 0 lädt nur das Profil.')
    parser.add_argument('--implisense-list-id', type=UUID,
                        help='Sekundär: eine Implisense-Liste wählen; sonst alle gespeicherten Listen lesen. '
                             'Firmenfilter gelten nur für die anderen Quellen.')
    parser.add_argument('--implisense-period', type=int, choices=(7, 30, 90), default=90,
                        help='Sekundär: Zeitraum gespeicherter Implisense-Nachrichten in Tagen (Standard: 90).')
    return parser


def collect_implisense(args):
    from .implisense_browser import main as implisense_main
    output = args.output / 'implisense' / 'browser'
    options = ['news', '--headless', '--output', str(output),
               '--period', str(args.implisense_period)]
    if args.implisense_list_id:
        options += ['--list-id', str(args.implisense_list_id)]
    if args.browser_channel:
        options += ['--browser-channel', args.browser_channel]
    print('Implisense: gespeicherte Nachrichten; Firmenfilter wählen keine Implisense-Listen aus.', flush=True)
    try:
        status = implisense_main(options)
        return dict(status='ok' if status == 0 else 'error', output=str(output))
    except Exception as error:
        print(f'Implisense: {error}', file=sys.stderr)
        return dict(status='error', error=str(error), output=str(output))


def collect_company(company, args, register, register_error, kununu, kununu_error, gewerkschaften=None, karriere=None):
    summary = dict(company=asdict(company), providers={})
    if gewerkschaften is not None:
        summary['providers']['gewerkschaften'] = gewerkschaften[company.key]
    if karriere is not None:
        summary['providers']['karriere'] = karriere[company.key]
    if uses_source(args.source, 'unternehmensregister'):
        folder = args.output / 'unternehmensregister' / company_folder(company.key)
        try:
            if register is None:
                raise ValueError(register_error)
            result = run_company(register, company, folder, args)
        except Exception as error:
            result = dict(status='error', error=str(error))
            write_json(folder / 'error.json', result)
        summary['providers']['unternehmensregister'] = dict(result, output=str(folder))
    if uses_source(args.source, 'kununu'):
        print(f'kununu: {company.name}', flush=True)
        if company.kununu_url:
            print(f'  Profiladresse: {company.kununu_url}', flush=True)
        if not company.kununu_url:
            snapshot = dict(provider='kununu', status='skipped', reason='missing_kununu_url', profile=None, reviews=[])
            print('  Übersprungen: keine kununu_url in sources.csv und kein --kununu-url angegeben.', flush=True)
        elif kununu is not None:
            try:
                snapshot = kununu.research(company)
            except Exception as error:
                snapshot = dict(provider='kununu', status='error', error=str(error), profile=None, reviews=[])
        else:
            snapshot = dict(provider='kununu', backend='zyte', status='error', error=kununu_error,
                            profile=None, reviews=[])
        path = args.output / 'kununu' / company_folder(company.key) / 'snapshot.json'
        snapshot = save_snapshot(path, snapshot)
        if snapshot.get('error'):
            print(f'  Fehler: {snapshot["error"]}', flush=True)
        if snapshot.get('request_limits'):
            limits = snapshot['request_limits']
            print(f'  Zyte-API-Aufrufe: {limits["requests_attempted"]}/{limits["max_requests_per_command"]}', flush=True)
        if snapshot.get('review_limit'):
            print(f'  Mitarbeiterbewertungen: {len(snapshot.get("reviews", []))}/{snapshot["review_limit"]} '
                  '(letzte 2 Jahre)', flush=True)
        if snapshot.get('reviews') and snapshot.get('factor_score_coverage'):
            coverage = snapshot['factor_score_coverage']
            print(f'  Kategoriebewertungen: {coverage["reviews_with_scores"]}/{len(snapshot["reviews"])} Bewertungen '
                  f'({status_text(coverage["status"])})', flush=True)
        diagnostics = snapshot.get('diagnostics', {})
        for page in diagnostics.get('pages', []):
            if page.get('http_status') is not None:
                print(f'  HTTP {page["http_status"]}: {page.get("title") or page.get("final_url")}', flush=True)
            if page.get('html_bytes') is not None:
                print(f'  HTML: {page["html_path"]}', flush=True)
        print(f'  Ergebnisdatei: {path}', flush=True)
        summary['providers']['kununu'] = dict(status=snapshot['status'], error=snapshot.get('error'),
                                            reason=snapshot.get('reason'), path=str(path))
    statuses = [result['status'] for result in summary['providers'].values()]
    successful = ('ok', 'searched', 'complete', 'no_results')
    if all(status == 'skipped' for status in statuses):
        summary['status'] = 'skipped'
    elif all(status in (*successful, 'skipped') for status in statuses):
        summary['status'] = 'ok'
    elif any(status in (*successful, 'partial', 'incomplete') for status in statuses):
        summary['status'] = 'partial'
    else:
        summary['status'] = 'error'
    print(f'{company.key}: {status_text(summary["status"])}', flush=True)
    return summary


def main(argv=None):
    # Bei umgeleiteten Windows-Konsolen nicht darstellbare Zeichen ersetzen.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = make_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    validate_arguments(parser, args)
    validate_career_arguments(parser, args)
    if not 1 <= args.kununu_timeout <= 3600 or not 0 <= args.kununu_reviews <= 15:
        parser.error('--kununu-timeout muss 1–3600 und --kununu-reviews 0–15 sein.')
    if args.zyte_country and (len(args.zyte_country) != 2 or not args.zyte_country.isascii()
                             or not args.zyte_country.isalpha()):
        parser.error('--zyte-country benötigt einen zweistelligen Ländercode, z. B. DE.')
    if args.kununu_url and not profile_url(args.kununu_url):
        parser.error('--kununu-url muss ein öffentliches Kununu-Arbeitgeberprofil sein.')
    try:
        companies = select_companies(args)
        if args.career_url:
            if len(companies) != 1:
                parser.error('--career-url benötigt genau ein ausgewähltes Unternehmen.')
            companies[0].career_urls = [args.career_url]
        if (args.kununu_url or args.open or args.report_index) and len(companies) != 1:
            parser.error('--kununu-url, --open und --report-index benötigen genau ein ausgewähltes Unternehmen.')
        if args.kununu_url:
            companies[0].kununu_url = profile_url(args.kununu_url)
        if args.list_companies:
            for company in companies:
                location = ' '.join(filter(None, (company.postal_code, company.location)))
                print(f'{company.key}: {company.name}; Ort={location or "fehlt"}; Branche={"; ".join(company.branches) or "fehlt"}; '
                      f'kununu={company.kununu_url or "übersprungen (keine kununu_url)"}')
            return 0
        if args.list_career_sites:
            for company in companies:
                urls, rejected = seed_urls(company)
                print(f'{company.key}: {", ".join(urls) or "URL FEHLT"}')
                for row in rejected:
                    print(f'  Nicht verwendet: {row["url"]} ({row["reason"]})')
            print(f'{len(companies)} Unternehmen; keine Netzwerkanfragen.')
            return 0
        use_gewerkschaften = args.source == 'gewerkschaften' or (
            uses_source(args.source, 'gewerkschaften') and args.gewerkschaften.exists())
        preview = args.list_gewerkschaften
        union_plan = union_plan_for(companies, args) if args.list_gewerkschaften or (use_gewerkschaften and not preview) else []
        if preview:
            for company in companies:
                print(f'{company.key}: {"; ".join(company.branches) or "Branche FEHLT (Spalte Branche in der Firmenliste ergänzen)"}')
                for item in union_plan:
                    if company.key in item['company_keys']:
                        association = item['source']
                        reserve = ' [Reserve ohne Fachtreffer]' if item.get('phase') == 'fallback' else ''
                        print(f'  {association.source_name or association.name}{reserve}: {", ".join(association.seeds) or "URL FEHLT"}')
            union_count = len({item['source'].name for item in union_plan})
            print(f'{union_count} Gewerkschaften, {len(companies)} Unternehmen; '
                  f'{len(union_plan)} Gewerkschaftsseiten; keine Netzwerkanfragen.')
            return 0
        if use_gewerkschaften:
            missing = [c.key for c in companies if not c.branches]
            if missing:
                print(f'Branche fehlt; branchenübergreifende Reserve für: {", ".join(missing)}', file=sys.stderr)
        if (uses_source(args.source, 'kununu')
                and any(company.kununu_url for company in companies)):
            if args.zyte_max_requests is None or not 1 <= args.zyte_max_requests <= 10000:
                parser.error('--zyte-max-requests ausdrücklich angeben (1–10000 Aufrufe für den gesamten Lauf).')
            linked = sum(bool(company.kununu_url) for company in companies)
            required = linked
            if args.zyte_max_requests < required:
                parser.error(f'--zyte-max-requests muss für diese Auswahl mindestens {required} sein: '
                             'ein erster Seitenabruf je Unternehmen; weitere Bewertungsseiten benötigen zusätzliche Aufrufe.')
        register = kununu = None
        register_error = kununu_error = None
        batch = dict(started_at=datetime.now(timezone.utc).isoformat(), source=args.source, companies=[])
        union_results = collect_gewerkschaften(companies, args, union_plan) if use_gewerkschaften else None
        career_results = collect_karriere(companies, args) if uses_source(args.source, 'karriere') else None
        with ExitStack() as stack:
            if uses_source(args.source, 'unternehmensregister'):
                try:
                    register = stack.enter_context(UnternehmensregisterClient(
                        headless=args.open is None, browser_channel=args.browser_channel))
                except Exception as error:
                    register_error = str(error)
            if uses_source(args.source, 'kununu') and any(company.kununu_url for company in companies):
                try:
                    kununu = stack.enter_context(KununuZyte(
                        output=args.output, timeout=args.kununu_timeout,
                        max_reviews=args.kununu_reviews, max_requests=args.zyte_max_requests,
                        ip_type=args.zyte_ip_type, country=args.zyte_country))
                except (ValueError, ImportError) as error:
                    kununu_error = str(error)
            for company in companies:
                result = collect_company(company, args, register, register_error, kununu, kununu_error,
                                         union_results, career_results)
                batch['companies'].append(result)
                write_json(args.output / 'research' / 'batch.json', batch)
        if uses_source(args.source, 'implisense'):
            batch['implisense'] = collect_implisense(args)
            write_json(args.output / 'research' / 'batch.json', batch)
        return int(any(company['status'] not in ('ok', 'skipped') for company in batch['companies'])
                   or batch.get('implisense', {}).get('status') == 'error')
    except (Exception, KeyboardInterrupt) as error:
        print(f'Recherche: {error}', file=sys.stderr)
        return 1
