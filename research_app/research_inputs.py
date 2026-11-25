"""Gemeinsame Firmenauswahl aus CSV und Excel sowie JSON-Ausgabe."""
import csv
from dataclasses import dataclass, field
import hashlib
from io import StringIO
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

from .branches import normalize_branches


@dataclass
class CompanyQuery:
    key: str
    name: str
    location: str = ''
    name_source: str = 'command_line'
    urls: list[str] = field(default_factory=list)
    kununu_url: str | None = None
    branches: list[str] = field(default_factory=list)
    search_terms: list[str] = field(default_factory=list)
    career_urls: list[str] = field(default_factory=list)
    postal_code: str = ''
    street: str = ''
    region: str = ''
    career_sitemap_locations: list[str] = field(default_factory=list)
    implisense_id: str = ''


def normalized(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(value or ''))).strip().casefold()


def workbook_rows(path, *, required_headers=(), allow_missing=False):
    from openpyxl import load_workbook
    book = load_workbook(Path(path), read_only=True, data_only=True)
    matched = False
    try:
        for sheet in book:
            rows = iter(sheet.iter_rows(values_only=True))
            headers = [normalized(value) for value in next(rows, ())]
            if not any(headers):
                continue
            if not set(required_headers).issubset(headers):
                continue
            matched = True
            if len([h for h in headers if h]) != len(set(h for h in headers if h)):
                raise ValueError(f'{path}/{sheet.title}: doppelte Spaltenüberschriften.')
            for number, values in enumerate(rows, 2):
                if any(value is not None for value in values):
                    yield sheet.title, number, dict(zip(headers, [str(v).strip() if v is not None else '' for v in values]))
        if required_headers and not matched and not allow_missing:
            raise ValueError(f'{path}: kein Tabellenblatt mit den benötigten Spalten: {", ".join(required_headers)}.')
    finally:
        book.close()


def load_excel_companies(path):
    companies = {}
    for sheet, number, row in workbook_rows(path, required_headers=('unternehmen', 'branche')):
        if 'unternehmen' not in row or 'branche' not in row:
            raise ValueError(f'{path}/{sheet}: Die Spalten Unternehmen und Branche sind erforderlich.')
        name, branch = row['unternehmen'], row['branche']
        if not name or not branch:
            raise ValueError(f'{path}/{sheet}:{number}: Unternehmen oder Branche ist leer.')
        company = companies.setdefault(normalized(name), CompanyQuery(name, name, name_source='excel'))
        for column, target in (('url', company.urls), ('career_url', company.career_urls)):
            value = row.get(column, '').strip()
            if value and value not in target:
                target.append(value)
        for term in re.split(r'[;|]', row.get('suchbegriffe', row.get('search_terms', row.get('aliases', '')))):
            if term.strip() and term.strip() not in company.search_terms:
                company.search_terms.append(term.strip())
        company.branches = normalize_branches([*company.branches, branch])
    return list(companies.values())


def profile_url(value):
    try:
        parts = urlsplit(value)
        segments = parts.path.strip('/').split('/')
        if (parts.scheme not in ('http', 'https') or parts.username or parts.password
                or parts.hostname not in ('kununu.com', 'www.kununu.com') or parts.port
                or len(segments) < 2 or segments[0] not in ('de', 'at', 'ch')
                or segments[1] in ('search', 'login', 'bewerten', 'gehalt', 'insights', 'puls')
                or not segments[1] or '\\' in value):
            return None
        return 'https://www.kununu.com/' + '/'.join(segments[:2])
    except (TypeError, ValueError):
        return None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def company_folder(key):
    slug = re.sub(r'[^a-zA-Z0-9_-]+', '-', key).strip('-')[:60] or 'company'
    return slug + '-' + hashlib.sha256(key.encode()).hexdigest()[:8]


def load_companies(path):
    """Firmen aus CSV lesen, einschließlich optionaler offizieller Karriereadressen."""
    path = Path(path)
    raw = path.read_bytes()
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = raw.decode('cp1252')
    try:
        delimiter = csv.Sniffer().sniff(content[:8192], delimiters=',;\t').delimiter
    except csv.Error:
        delimiter = ','
    reader = csv.DictReader(StringIO(content, newline=''), delimiter=delimiter, strict=True)
    reader.fieldnames = [header.strip() for header in (reader.fieldnames or [])]
    if len(set(reader.fieldnames)) != len(reader.fieldnames) or 'company_key' not in reader.fieldnames:
        raise ValueError(f'{path}: Eindeutige Spaltenüberschriften und company_key sind erforderlich.')
    companies = {}
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f'{path}:{reader.line_num}: falsche Anzahl von CSV-Feldern.')
        if not any(value.strip() for value in row.values()):
            continue
        enabled = row.get('enabled', '').strip().casefold()
        if enabled in ('false', '0', 'no', 'nein'):
            continue
        if enabled not in ('true', '1', 'yes', 'ja', ''):
            raise ValueError(f'{path}:{reader.line_num}: ungültiger enabled-Wert.')
        key = row['company_key'].strip()
        explicit = row.get('company_name', '').strip()
        title = row.get('name', '').strip()
        name = explicit or (re.split(r'\s+[–—]\s+|\s+-\s+', title, maxsplit=1)[0] if title else key)
        location = row.get('company_location', '').strip()
        if not key or not name:
            raise ValueError(f'{path}:{reader.line_num}: leerer Firmenschlüssel oder Firmenname.')
        name_source = 'company_name' if explicit else 'source_title' if title else 'company_key'
        company = companies.setdefault(key, CompanyQuery(key, name, location, name_source))
        for term in re.split(r'[;|]', row.get('Suchbegriffe', row.get('search_terms', ''))):
            if term.strip() and term.strip() not in company.search_terms:
                company.search_terms.append(term.strip())
        company.branches = normalize_branches([
            *company.branches, row.get('Branche', row.get('branche', row.get('branches', '')))])
        if explicit:
            if company.name_source == 'company_name' and company.name != name:
                raise ValueError(f'Widersprüchliche company_name-Werte für {key}.')
            company.name, company.name_source = name, name_source
        if location:
            if company.location and company.location != location:
                raise ValueError(f'Widersprüchliche company_location-Werte für {key}.')
            company.location = location
        for column, attribute in (('company_postal_code', 'postal_code'),
                                  ('company_street', 'street'), ('company_region', 'region'),
                                  ('implisense_id', 'implisense_id')):
            value = row.get(column, '').strip()
            if value:
                if getattr(company, attribute) not in ('', value):
                    raise ValueError(f'Widersprüchliche {column}-Werte für {key}.')
                setattr(company, attribute, value)
        url = row.get('url', '').strip()
        employer = row.get('kununu_url', '').strip()
        if urlsplit(url).hostname in ('kununu.com', 'www.kununu.com'):
            if employer and profile_url(employer) != profile_url(url):
                raise ValueError(f'{path}:{reader.line_num}: url und kununu_url verweisen auf verschiedene Profile.')
            employer = url
        elif url and url not in company.urls:
            company.urls.append(url)
        career = row.get('career_url', '').strip()
        if career and career not in company.career_urls:
            company.career_urls.append(career)
        for value in row.get('career_sitemap_locations', '').split(';'):
            if value.strip() and value.strip() not in company.career_sitemap_locations:
                company.career_sitemap_locations.append(value.strip())
        if employer:
            root = profile_url(employer)
            if not root:
                raise ValueError(f'{path}:{reader.line_num}: ungültige Kununu-Arbeitgeberadresse.')
            if company.kununu_url and company.kununu_url != root:
                raise ValueError(f'Mehrere Kununu-Profile für {key}.')
            company.kununu_url = root
    return list(companies.values())


def select_companies(args):
    if args.company:
        return [CompanyQuery(args.company, args.company, args.location)]
    if getattr(args, 'companies', None):
        companies = load_excel_companies(args.companies)
        sources = args.sources or Path('data/sources.csv')
        saved = load_companies(sources) if sources.exists() else []
        for company in companies:
            matches = [row for row in saved if normalized(company.key) in
                       (normalized(row.key), normalized(row.name))]
            if len(matches) > 1:
                raise ValueError(f'Mehrdeutige Ergänzung aus sources.csv für {company.key}.')
            if matches:
                row = matches[0]
                company.kununu_url = row.kununu_url
                company.urls = list(dict.fromkeys([*company.urls, *row.urls]))
                company.career_urls = list(dict.fromkeys([*company.career_urls, *row.career_urls]))
                company.location = row.location
                company.postal_code, company.street, company.region = row.postal_code, row.street, row.region
                company.implisense_id = row.implisense_id
                company.career_sitemap_locations = row.career_sitemap_locations
                company.branches = normalize_branches([*company.branches, *row.branches])
                for term in row.search_terms:
                    if normalized(term) not in [normalized(value) for value in company.search_terms]:
                        company.search_terms.append(term)
                if row.name_source == 'company_name':
                    company.name, company.name_source = row.name, row.name_source
    else:
        companies = load_companies(args.sources or Path('data/sources.csv'))
    if args.company_key:
        companies = [company for company in companies if company.key == args.company_key]
    if not companies:
        raise ValueError('Keine aktiven Unternehmen ausgewählt.')
    return companies
