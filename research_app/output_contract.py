"""Liefer-JSONs gegen die mitgelieferten Empfängerschemas prüfen und normalisieren."""
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from functools import lru_cache
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator

from .research_inputs import write_json


FACTORS = ('atmosphere', 'workLife', 'communication', 'workConditions', 'leadership',
           'teamwork', 'oldColleagues', 'equality', 'career', 'salary', 'image', 'environment', 'tasks')
SECTIONS = ('positive', 'negative', 'suggestion')
CATEGORIES = ('finanzberichte', 'bekanntmachungen')
ACTIVE = ('ok', 'partial')
COVERAGE = ('bounded_employee_review_sample_last_2_years_not_all_reviews', 'company_profile_only')
SCHEMA_DIR = Path(__file__).resolve().parent.parent / 'data' / 'json-schemas'


@lru_cache(maxsize=None)
def schema_validator(kind):
    name = 'kununu_snapshot' if kind == 'kununu' else kind
    path = SCHEMA_DIR / f'{name}.schema.json'
    if not path.is_file():
        raise ContractError(f'JSON-Schema fehlt: {path}')
    schema = json.loads(path.read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_schema(kind, value):
    error = next(schema_validator(kind).iter_errors(value), None)
    if error:
        raise ContractError(f'{kind} {error.json_path}: {error.message}')


class ContractError(ValueError):
    """Die Datei kann mit den vorhandenen Fakten nicht vertragskonform geliefert werden."""


def text(value):
    return isinstance(value, str)


def word(value):
    return text(value) and bool(value.strip())


def obj(value):
    return isinstance(value, dict)


def array(value):
    return isinstance(value, list)


def boolean(value):
    return type(value) is bool


def integer(value):
    return type(value) is int and value >= 0


def positive_integer(value):
    return integer(value) and value >= 1


def score(value):
    return type(value) in (int, float) and math.isfinite(value) and 1 <= value <= 5


def nullable(check):
    return lambda value: value is None or check(value)


def choice(*values):
    return lambda value: text(value) and value in values


def url(value):
    try:
        parts = urlsplit(value) if text(value) else None
        return bool(parts and parts.scheme in ('http', 'https') and parts.hostname
                    and not re.search(r'\s', value))
    except ValueError:
        return False


def day(value):
    try:
        return bool(text(value) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) and date.fromisoformat(value))
    except ValueError:
        return False


def timestamp(value):
    try:
        return bool(text(value) and 'T' in value
                    and datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None)
    except ValueError:
        return False


def uuid(value):
    try:
        return text(value) and str(UUID(value)) == value.lower()
    except ValueError:
        return False


def fields(value, required, optional=None, *, path='$', closed=False):
    if not obj(value):
        raise ContractError(f'{path}: Objekt erwartet')
    for key, check in required.items():
        if key not in value:
            raise ContractError(f'{path}.{key}: Pflichtfeld fehlt')
        if not check(value[key]):
            raise ContractError(f'{path}.{key}: ungültiger Wert oder Typ')
    for key, check in (optional or {}).items():
        if key in value and not check(value[key]):
            raise ContractError(f'{path}.{key}: ungültiger Wert oder Typ')
    if closed and set(value) - required.keys() - (optional or {}).keys():
        raise ContractError(f'{path}: unbekannte Schlüssel')


def unique(rows, keys, path):
    seen = set()
    for row in rows:
        identity = tuple(row[key] for key in keys)
        if identity in seen:
            raise ContractError(f'{path}: doppelte Kennung {identity}')
        seen.add(identity)


def validate_profile(profile):
    fields(profile, dict(url=url, name=text, overall_score=score, review_count=integer,
                         recommendation_percent=nullable(lambda v: integer(v) and v <= 100),
                         recommendation_period=nullable(text),
                         observed_at=timestamp), path='$.profile')


def validate_review(review):
    fields(review, dict(id=word, published_at=lambda v: day(v) or timestamp(v), rating=score,
                        recommended=boolean, review_type=text, factor_scores=obj, sections=obj),
           dict(url=nullable(url), title=nullable(text), content=nullable(text),
                factor_comments=nullable(obj), has_text=boolean), path='$.reviews[]')
    fields(review['factor_scores'], {}, {key: nullable(score) for key in FACTORS},
           path='$.reviews[].factor_scores')
    fields(review['sections'], {}, {key: nullable(text) for key in SECTIONS},
           path='$.reviews[].sections')


ARTICLE_OPTIONAL = dict(gewerkschaft=text,
                        content_hash=nullable(lambda v: text(v) and bool(re.fullmatch(r'[0-9a-fA-F]{64}', v))),
                        fetched_at=nullable(timestamp))
MATCH_FIELDS = dict(matched_name=text, matched_in=text)


def validate_article(article, *, metadata=False):
    required = dict(id=word, title=text, url=url)
    optional = dict(ARTICLE_OPTIONAL)
    if metadata:
        required.update(published_at=nullable(day), match=obj)
        optional['branches'] = lambda v: array(v) and all(text(item) for item in v)
    else:
        required['text'] = text
        optional.update(published_at=nullable(day), match=obj)
    fields(article, required, optional, path='$.articles[]' if metadata else '$')
    if 'match' in article:
        fields(article['match'], MATCH_FIELDS if metadata else {},
               dict(excerpt=nullable(text)) if metadata else dict(MATCH_FIELDS, excerpt=nullable(text)),
               path='$.match')


def validate(kind, value):
    """Originalschemas sowie Kalenderdaten und eindeutige Kennungen prüfen."""
    validate_schema(kind, value)
    if kind in ('kununu', 'artikel_snapshot'):
        fields(value, dict(status=text), dict(reason=nullable(text)))
        if value['status'] not in ACTIVE:
            return
        if kind == 'kununu':
            fields(value, dict(profile=nullable(obj), reviews=array, review_period=nullable(obj),
                               coverage=text))
            if value['profile'] is not None:
                validate_profile(value['profile'])
            if value['review_period'] is not None:
                fields(value['review_period'], dict(since=timestamp, until=timestamp), path='$.review_period')
            for review in value['reviews']:
                validate_review(review)
            unique(value['reviews'], ('id',), '$.reviews')
        else:
            fields(value, dict(date_from=day, date_to=day, truncated=boolean, articles=array))
            for article in value['articles']:
                validate_article(article, metadata=True)
            unique(value['articles'], ('id',), '$.articles')
    elif kind == 'artikel_article':
        validate_article(value)
    elif kind == 'register_publications':
        fields(value, dict(company=obj, date_from=day, date_to=day, searched_at=timestamp,
                           truncated=boolean, publications=array))
        fields(value['company'], dict(key=text, name=text), path='$.company')
        for row in value['publications']:
            fields(row, dict(category=text, index=positive_integer, title=text,
                             publication_date=day, company=text, url=url, language=text),
                   dict(metadata=nullable(text), source_area=nullable(text)), path='$.publications[]')
        unique(value['publications'], ('category', 'index'), '$.publications')
    elif kind == 'register_downloads':
        if not array(value):
            raise ContractError('$: Liste erwartet')
        for row in value:
            fields(row, dict(category=text, index=positive_integer,
                             status=text), dict(error=nullable(text)), path='$[]')
        unique(value, ('category', 'index'), '$')
    elif kind == 'register_documents':
        if not array(value):
            raise ContractError('$: Liste erwartet')
        for document in value:
            fields(document, dict(format=word, text=text, tables=array), dict(title=nullable(text)), path='$[]')
            for table in document['tables']:
                fields(table, dict(index=positive_integer,
                                   rows=lambda v: array(v) and all(array(row) and all(text(c) for c in row) for row in v)),
                       dict(title=nullable(text)), path='$[].tables[]')
    elif kind == 'implisense_news':
        fields(value, dict(fetched_at=timestamp, period_days=positive_integer, articles=array, register_events=array))
        common = dict(title=text, companyId=word, companyName=text, publishedAt=day)
        for article in value['articles']:
            fields(article, dict(common, sourceUrl=url),
                   {key: nullable(text) for key in ('sourceName', 'signal', 'summaryDe', 'summaryEn')}, path='$.articles[]')
        for event in value['register_events']:
            fields(event, dict(common, category=word), path='$.register_events[]')
    else:
        raise ContractError(f'Unbekannte Dateiart: {kind}')


def _review_fields(review):
    """Fehlende nullable-Felder ergänzen; offene Faktor-/Abschnittsschlüssel erhalten."""
    if not obj(review):
        raise ContractError('Bewertung ist kein Objekt')
    row = deepcopy(review)
    if 'id' not in row and url(row.get('url')):
        match = re.search(r'/bewertung/([^/]+)', urlsplit(row['url']).path)
        if match and uuid(match[1]):
            row['id'] = match[1]
    for key, names in (('factor_scores', FACTORS), ('sections', SECTIONS)):
        supplied = row.get(key, {})
        if not obj(supplied):
            raise ContractError(f'{key}: Objekt erwartet')
        for k, v in row.get('contract_unmapped_' + key, {}).items():
            supplied.setdefault(k, v)
        # Ungültige vorhandene Werte nicht stillschweigend durch null ersetzen.
        row[key] = dict.fromkeys(names)
        row[key].update(supplied)
    validate_review(row)
    return row


def _kununu(value):
    rejected = []
    if 'reviews' not in value or not array(value['reviews']):
        raise ContractError('$.reviews: Liste erforderlich')
    value.setdefault('profile', None)
    value.setdefault('review_period', None)
    value.setdefault('coverage', COVERAGE[0] if value['reviews'] else COVERAGE[1])
    if value['profile'] is not None:
        try:
            validate_profile(value['profile'])
        except ContractError as error:
            rejected.append(dict(field='profile', error=str(error), value=value['profile']))
            value['profile'] = None
    reviews, seen = [], set()
    for original in value['reviews']:
        try:
            row = _review_fields(original)
            if row['id'] in seen:
                raise ContractError('Doppelte Bewertungs-ID')
            seen.add(row['id'])
            reviews.append(row)
        except ContractError as error:
            rejected.append(dict(field='reviews', error=str(error), value=original))
    value['reviews'] = reviews
    if rejected:
        value.setdefault('contract_rejections', []).extend(rejected)
        value['status'] = 'partial' if reviews or value['profile'] else 'error'
        value['reason'] = value.get('reason') or 'output_contract_incomplete'
    # Diagnose und Aggregat müssen zur tatsächlich gelieferten Stichprobe passen.
    values = {key: [row['factor_scores'][key] for row in reviews if row['factor_scores'][key] is not None]
              for key in FACTORS}
    value['factor_scores_aggregated'] = {key: dict(mean=round(sum(v) / len(v), 2), count=len(v))
                                         for key, v in values.items() if v}
    scored = sum(any(v is not None for v in row['factor_scores'].values()) for row in reviews)
    complete = sum(all(row['factor_scores'][key] is not None for key in FACTORS) for row in reviews)
    value['factor_score_coverage'] = dict(reviews_with_scores=scored, reviews_without_scores=len(reviews) - scored,
                                         reviews_with_all_known_scores=complete,
                                         status='complete' if complete == len(reviews) and complete else
                                         'partial' if scored else 'unavailable' if reviews else 'no_reviews')


def prepare(kind, data):
    """Eine Kopie normalisieren und prüfen; Rohdaten des Sammlers nicht verändern."""
    value = deepcopy(data)
    if kind in ('kununu', 'artikel_snapshot'):
        fields(value, dict(status=word))
        value.setdefault('reason', None)
        if value['status'] not in ACTIVE:
            if not word(value['reason']):
                value['reason'] = 'scrape_error' if value['status'] == 'error' else value['status']
        elif kind == 'kununu':
            _kununu(value)
    elif kind == 'register_downloads' and array(value):
        for row in value:
            if obj(row) and row.get('status') in ('downloaded_no_text', 'unsupported'):
                row['source_status'] = row['status']
                row['error'] = row.get('error') or row['status']
                row['status'] = 'error'
    elif kind == 'register_documents' and array(value):
        for document in value:
            if not obj(document):
                continue
            if document.get('format') == 'pdf' and 'text' not in document:
                pages = document.get('pages')
                if array(pages) and all(obj(p) and text(p.get('text')) for p in pages):
                    document['text'] = '\n\n'.join(p['text'] for p in pages)
            document.setdefault('tables', [])
    elif kind == 'register_publications' and obj(value):
        for row in value.get('publications', []):
            if obj(row) and row.get('language') in ('de', 'en'):
                row['source_language'] = row['language']
                row['language'] = {'de': 'Deutsch', 'en': 'Englisch'}[row['language']]
        coverage = value.get('coverage', {})
        searches = value.get('searches', [])
        if (obj(coverage) and (coverage.get('more_pages') or coverage.get('search_complete') is False)
                or array(searches) and any(obj(s) and s.get('has_more') for s in searches)):
            value['truncated'] = True
        elif 'truncated' not in value and obj(coverage):
            if type(coverage.get('publication_limit_reached')) is bool and coverage.get('search_complete') is True:
                value['truncated'] = coverage['publication_limit_reached']
    validate(kind, value)
    return value


def write_contract_json(path, data, kind):
    value = prepare(kind, data)
    write_json(path, value)
    return value


def download_category(row, publications):
    matches = [p for p in publications if p.get('index') == row.get('index')]
    if row.get('source_url'):
        matches = [p for p in matches if p.get('url') == row['source_url']]
    if row.get('title'):
        matches = [p for p in matches if p.get('title') == row['title']]
    if row.get('category'):
        matches = [p for p in matches if p.get('category') == row['category']]
    if len(matches) != 1 or not word(matches[0].get('category')):
        raise ContractError(f'Download {row.get("index")}: Kategorie nicht eindeutig zuordenbar')
    return matches[0]['category']


def document_path(folder, row):
    category, index = row.get('category'), row.get('index')
    if not word(category) or not re.fullmatch(r'[A-Za-z0-9_-]+', category) or not positive_integer(index):
        raise ContractError('Ungültige Dokumentreferenz')
    supplied = row.get('documents_path')
    target = Path(supplied) if word(supplied) else Path(category) / f'publication-{index}' / 'documents.json'
    # Lokale Sammler schreiben auch Pfade relativ zum Arbeitsverzeichnis.
    if not target.is_absolute() and not target.resolve().is_relative_to(folder.resolve()):
        target = folder / target
    target = target.resolve()
    if not target.is_relative_to(folder.resolve()):
        raise ContractError(f'Dokument außerhalb des Firmenordners: {target}')
    return target


def register_files(path):
    yield path, 'register_publications'
    downloads = path.with_name('downloads.json')
    if not downloads.exists():
        return
    yield downloads, 'register_downloads'
    publications = json.loads(path.read_text(encoding='utf-8'))['publications']
    for original in json.loads(downloads.read_text(encoding='utf-8')):
        row = dict(original)
        row.setdefault('category', download_category(row, publications))
        document = document_path(path.parent, row)
        if document.exists() or row.get('status') == 'downloaded':
            yield document, 'register_documents'


def prepare_file(path, kind, data):
    data = deepcopy(data)
    if kind == 'register_publications' and text(data.get('company')):
        # Im flachen Lieferpaket ist der Firmenordner der lokale Exportschlüssel.
        key = path.parent.parent.name if path.parent.name == 'register' else path.parent.name
        data['company'] = dict(key=key, name=data['company'])
    elif kind == 'register_downloads':
        publications = json.loads(path.with_name('publications.json').read_text(encoding='utf-8'))['publications']
        for row in data:
            if 'category' not in row:
                row['category'] = download_category(row, publications)
    return prepare(kind, data)


def contract_files(root):
    """Nur Lieferdateien aktueller Indizes; niemals Caches, Historie oder Browserprofile."""
    root = Path(root).resolve()
    # Exportpakete behalten ihre eigene, flache Verzeichnisstruktur.
    companies = [root] if (root / 'register' / 'publications.json').is_file() else [
        path.parent.parent for path in sorted(root.glob('*/register/publications.json'))]
    for company in companies:
        snapshot = company / 'kununu.json'
        if snapshot.is_file():
            yield snapshot, 'kununu'
        yield from register_files(company / 'register' / 'publications.json')
    providers = ('kununu', 'unternehmensregister', 'gewerkschaften', 'implisense')
    sources = [(root.name, root)] if root.name in providers else [(p, root / p) for p in providers]
    for provider, source in sources:
        if provider in ('kununu', 'gewerkschaften'):
            for path in sorted(source.glob('*/snapshot.json')):
                if path.parent.name.startswith('_'):
                    continue
                yield path, 'kununu' if provider == 'kununu' else 'artikel_snapshot'
                if provider != 'kununu':
                    data = json.loads(path.read_text(encoding='utf-8'))
                    if data.get('status') in ACTIVE:
                        for row in data.get('articles', []):
                            identity = row.get('id')
                            if not word(identity) or not re.fullmatch(r'[A-Za-z0-9_-]+', identity):
                                raise ContractError(f'{path}: unsichere Artikel-ID')
                            yield path.parent / 'articles' / identity / 'article.json', 'artikel_article'
        elif provider == 'unternehmensregister':
            for path in sorted(source.glob('*/publications.json')):
                yield from register_files(path)
        else:
            path = source / 'browser' / 'news.json'
            if path.exists():
                yield path, 'implisense_news'


def check_links(records):
    """Firmenbezogene Referenzen prüfen, statt alte Dateien rekursiv mitzunehmen."""
    for path, (kind, value) in records.items():
        if kind == 'artikel_snapshot' and value['status'] in ACTIVE:
            for row in value['articles']:
                target = path.parent / 'articles' / row['id'] / 'article.json'
                if target not in records or records[target][1]['id'] != row['id']:
                    raise ContractError(f'{path}: Artikelreferenz stimmt nicht: {row["id"]}')
        elif kind == 'register_publications':
            target = path.with_name('downloads.json')
            if target in records:
                keys = {(row['category'], row['index']) for row in value['publications']}
                for row in records[target][1]:
                    if (row['category'], row['index']) not in keys:
                        raise ContractError(f'{target}: Download gehört nicht zur aktuellen Veröffentlichungsliste')
                    if row['status'] == 'downloaded':
                        document = document_path(path.parent, row)
                        if document not in records or not records[document][1]:
                            raise ContractError(f'{target}: erfolgreicher Download ohne Dokumentinhalt')


def migrate(roots, *, apply=False, backup_root=None, kununu_history=()):
    """Erst alle Dateien prüfen, dann mit exakter Sicherung nur Änderungen schreiben."""
    from .kununu_storage import restore_factor_scores
    records, changes = {}, []
    history = []
    for root in kununu_history:
        for source, kind in contract_files(root):
            if kind == 'kununu':
                history.append((source, json.loads(source.read_text(encoding='utf-8'))))
    for number, root in enumerate(roots, 1):
        root = Path(root).resolve()
        if not root.is_dir():
            raise ContractError(f'Ausgabeordner fehlt: {root}')
        for path, kind in contract_files(root):
            if not path.resolve().is_relative_to(root):
                raise ContractError(f'Datei außerhalb des Ausgabeordners: {path}')
            if path in records:
                continue
            try:
                original = path.read_bytes()
                data = json.loads(original)
                value = data
                if apply:
                    if kind == 'kununu':
                        for source, previous in history:
                            value = restore_factor_scores(value, previous, source)
                    value = prepare_file(path, kind, value)
                validate(kind, value)
            except (ValueError, OSError) as error:
                raise ContractError(f'{path}: {error}') from error
            records[path] = (kind, value)
            if apply and value != data:
                changes.append((path, value, original, Path(f'root-{number}') / path.relative_to(root)))
    if not records:
        raise ContractError('Keine Lieferdateien gefunden; Ausgabeordner oder Quellenordner angeben.')
    check_links(records)
    backup = None
    if changes:
        if backup_root is None:
            backup_root = Path(roots[0]) / 'contract-backups'
        backup = Path(backup_root).resolve() / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
        # Alle Sicherungen vor der ersten Änderung anlegen. Bei einem I/O-Abbruch
        # sind bereits geänderte Dateien vollständig wiederherstellbar.
        backup.mkdir(parents=True, exist_ok=False)
        manifest = []
        for path, value, original, relative in changes:
            if path.read_bytes() != original:
                raise ContractError(f'Datei während der Prüfung verändert: {path}')
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(original)
            manifest.append(dict(path=str(path), backup=str(saved), sha256=hashlib.sha256(original).hexdigest()))
        write_json(backup / 'manifest.json', manifest)
        for path, value, original, relative in changes:
            if path.read_bytes() != original:
                raise ContractError(f'Datei während der Sicherung verändert: {path}')
            write_json(path, value)
    return dict(checked=len(records), changed=len(changes), backup=str(backup) if backup else None)


def main(argv=None):
    from .cli import ArgumentParser
    parser = ArgumentParser(description='Liefer-JSONs gegen data/json-schemas prüfen/normalisieren, ohne Netzwerk.')
    parser.add_argument('roots', nargs='*', type=Path, default=[Path('output')], help='Ausgabe- oder Quellenordner')
    parser.add_argument('--apply', action='store_true', help='Normalisieren, prüfen und mit Sicherung aktualisieren; sonst nur prüfen.')
    parser.add_argument('--kununu-history', action='append', type=Path, default=[],
                        help='Expliziter alter Ausgabeordner: fehlende Teilnoten unveränderter Reviews mit Herkunft ergänzen.')
    parser.add_argument('--backup-root', type=Path, help='Sicherungen außerhalb eines Lieferpakets ablegen.')
    args = parser.parse_args(argv)
    try:
        result = migrate(args.roots, apply=args.apply, kununu_history=args.kununu_history, backup_root=args.backup_root)
    except (ContractError, OSError, ValueError) as error:
        print(f'Ausgabevertrag: {error}')
        return 1
    print(f'{result["checked"]} Lieferdateien geprüft; {result["changed"]} geändert.')
    if result['backup']:
        print(f'Sicherung: {result["backup"]}')
    return 0
