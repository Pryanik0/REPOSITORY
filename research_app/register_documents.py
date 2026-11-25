"""Text und Tabellen aus Registerdokumenten auslesen, ohne ZIP-Pfade zu entpacken."""
from io import BytesIO
from pathlib import Path
import json
import re
from zipfile import ZipFile, is_zipfile

from bs4 import BeautifulSoup, Comment

from .pdf_reader import read_pdf
from .cli import ArgumentParser
from .output_contract import write_contract_json

MAX_EXPANDED_BYTES = 100_000_000
MAX_FILES = 300


def publication_document(content):
    """Auch kurze Registermitteilungen sind gültig, wenn der Inhaltsbereich geladen ist."""
    soup = BeautifulSoup(content, 'html.parser')
    root = soup.select_one('.publicationcontent, #begin_pub')
    if root is None:
        raise ValueError('Kein bestätigter Veröffentlichungsbereich; Diagnose-HTML prüfen.')
    record = html_document(content, 'publication.html')
    if not record['text'].strip() or not re.search(r'[^\W\d_]', record['text']):
        raise ValueError('Der Veröffentlichungsbereich enthält keinen auslesbaren Text.')
    return record


def normalized_text(value):
    return ' '.join(value.replace('\xad', '').split())


def html_document(content, filename):
    """Veröffentlichungstext und Tabellenstruktur ohne finanzielle Interpretation erhalten."""
    soup = BeautifulSoup(content, 'html.parser')
    for tag in soup.select('script, style, [hidden], ix\\:hidden'):
        tag.decompose()
    root = soup.select_one('.publicationcontent') or soup.select_one('#begin_pub') or soup.body or soup
    for node in list(root.find_all(string=True)):
        if isinstance(node, Comment):
            node.extract()
        else:
            node.replace_with(normalized_text(str(node)))
    title_tag = root.select_one('.l_titel')
    company_tag = root.select_one('.z_titel')
    title = normalized_text(title_tag.get_text(' ', strip=True)) if title_tag else ''
    company = normalized_text(company_tag.get_text(' ', strip=True)) if company_tag else ''
    name = ' — '.join(value for value in (company, title) if value) or filename
    tables = []
    for table in root.find_all('table'):
        # Layouttabellen des Registers enthalten die ganze Veröffentlichung oder Aufzählungen.
        if table.get('id') == 'begin_pub' or table.find('table') or 'list_table_ul' in table.get('class', []):
            continue
        rows, cells, occupied = [], [], {}
        source_rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
        if len(source_rows) > 10000:
            raise ValueError('HTML-Tabelle überschreitet das Zeilenlimit.')
        for row_index, row in enumerate(source_rows):
            column = 0
            original_cells = []
            for cell in row.find_all(['td', 'th'], recursive=False):
                while (row_index, column) in occupied:
                    column += 1
                rowspan = int(cell.get('rowspan', 1))
                if rowspan == 0:
                    rowspan = len(source_rows) - row_index
                colspan = int(cell.get('colspan', 1))
                if not 1 <= rowspan <= 1000 or not 1 <= colspan <= 200 or column + colspan > 200:
                    raise ValueError('HTML-Tabelle überschreitet die Grenzen für Zellverbünde oder Spalten.')
                value = normalized_text(cell.get_text(' ', strip=True))
                original_cells.append({'text': value, 'column': column, 'rowspan': rowspan,
                                       'colspan': colspan, 'header': cell.name == 'th' or row.find_parent('thead') is not None})
                for y in range(row_index, row_index + rowspan):
                    for x in range(column, column + colspan):
                        occupied[y, x] = value
                if len(occupied) > 100000:
                    raise ValueError('HTML-Tabelle überschreitet das Zellenlimit.')
                column += colspan
            cells.append(original_cells)
        width = max((column for _, column in occupied), default=-1) + 1
        rows = [[occupied.get((y, x), '') for x in range(width)] for y in range(len(source_rows))]
        heading = table.find_previous(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
        caption = table.find('caption', recursive=False)
        tables.append({'index': len(tables) + 1,
                       'title': normalized_text((caption or heading).get_text(' ', strip=True)) if caption or heading else '',
                       'rows': rows, 'cells': cells})
    # Datentabellen vor der Textausgabe durch lesbare, tabulatorgetrennte Zeilen ersetzen.
    data_tables = [table for table in root.find_all('table')
                   if table.get('id') != 'begin_pub' and not table.find('table')
                   and 'list_table_ul' not in table.get('class', [])]
    for table, data in zip(data_tables, tables):
        table.replace_with('\n\n' + '\n'.join('\t'.join(row) for row in data['rows']) + '\n\n')
    for tag in root.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'tr']):
        tag.insert_before('\n\n')
        tag.insert_after('\n\n')
    for tag in root.find_all('br'):
        tag.replace_with('\n')
    # Die eingefügten Absatz- und Tabellentrenner erhalten.
    text = root.get_text(' ', strip=False)
    text = re.sub(r'[^\S\n\t]+', ' ', text.replace('\xad', ''))
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r' +([,.;:!?])', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip(' \r\n')
    return {'name': name, 'original_name': filename, 'title': title or filename,
            'company': company, 'format': 'html', 'text': text, 'tables': tables}


def extract_documents(content, name):
    """Dokumenttexte und Warnungen mit Größen-, Datei- und Verschachtelungsgrenzen sammeln."""
    records, warnings = [], []
    budget = {'bytes': 0, 'files': 0}

    def visit(data, filename, depth=0):
        budget['bytes'] += len(data)
        budget['files'] += 1
        if budget['bytes'] > MAX_EXPANDED_BYTES or budget['files'] > MAX_FILES:
            raise ValueError('Dokumentverarbeitung überschreitet das Größen- oder Dateilimit.')
        if is_zipfile(BytesIO(data)):
            if depth >= 3:
                raise ValueError('Dokumentarchiv überschreitet die erlaubte Verschachtelungstiefe.')
            with ZipFile(BytesIO(data)) as archive:
                for entry in archive.infolist():
                    if entry.is_dir():
                        continue
                    suffix = Path(entry.filename).suffix.lower()
                    if suffix not in ('.zip', '.pdf', '.html', '.htm', '.xhtml'):
                        continue
                    if entry.file_size > MAX_EXPANDED_BYTES - budget['bytes']:
                        raise ValueError('Archivdatei überschreitet das Größenlimit für die Verarbeitung.')
                    visit(archive.read(entry), filename + '!' + entry.filename, depth + 1)
        elif data.lstrip().startswith(b'%PDF'):
            try:
                _, pages, notes = read_pdf(data, filename)
                records.append({'name': filename, 'format': 'pdf', 'pages': [
                    {'number': number, 'text': text} for number, text in pages]})
                warnings.extend(notes)
            except ValueError as exc:
                warnings.append(f'{filename}: {exc}')
        elif Path(filename).suffix.lower() in ('.xhtml', '.html', '.htm'):
            record = html_document(data, filename)
            if record['text']:
                records.append(record)
    visit(content, name)
    return records, warnings


def refresh_html(root):
    """Text und JSON aus gespeicherten HTML-Originalen ohne Netzwerkzugriff erneuern."""
    count = 0
    for source in Path(root).glob('*/*/publication-*/publication.html'):
        record = html_document(source.read_bytes(), source.name)
        write_contract_json(source.with_name('documents.json'), [record], 'register_documents')
        source.with_name('publication.txt').write_text(record['name'] + '\n\n' + record['text'], encoding='utf-8')
        count += 1
    return count


def reclassify_saved(root):
    # Lokale Importe vermeiden einen Importkreis mit Browser und Registersteuerung.
    from dataclasses import fields
    from .register_jobs import CATEGORIES, write_json
    from .unternehmensregister import FinancialReport

    root = Path(root).resolve()
    changed = 0
    allowed_fields = {field.name for field in fields(FinancialReport)}
    for manifest_path in root.glob('*/publications.json'):
        folder = manifest_path.parent.resolve()
        if not folder.is_relative_to(root):
            raise ValueError('Firmenordner liegt außerhalb des Ausgabeordners.')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        publications = manifest['publications']
        moves = []
        for row in publications:
            report = FinancialReport(**{key: value for key, value in row.items() if key in allowed_fields})
            category = report.category
            previous = row['category']
            if previous == category:
                continue
            if previous not in CATEGORIES or not isinstance(row['index'], int):
                raise ValueError('Ungültige gespeicherte Kategorie oder Nummer.')
            source = folder / previous / f'publication-{row["index"]}'
            target = folder / category / source.name
            for path in (source, target):
                if not path.resolve().is_relative_to(folder):
                    raise ValueError('Veröffentlichungspfad liegt außerhalb des Firmenordners.')
            if source.exists() and target.exists():
                raise ValueError(f'Ziel der Neueinordnung existiert bereits: {target}')
            moves.append((source, target))
            row['category'] = category
            changed += 1
        if not moves:
            continue

        def update_paths(value):
            if isinstance(value, dict):
                return {key: update_paths(item) for key, item in value.items()}
            if isinstance(value, list):
                return [update_paths(item) for item in value]
            if isinstance(value, str):
                for source, target in moves:
                    # Vorhandene Ergebnisdateien können relative Windows-Pfade enthalten.
                    pairs = [(str(source), str(target))]
                    if source.is_relative_to(Path.cwd()):
                        pairs.append((str(source.relative_to(Path.cwd())), str(target.relative_to(Path.cwd()))))
                    for old, new in pairs:
                        value = value.replace(old + '\\', new + '\\').replace(old + '/', new + '/')
                return value
            return value

        downloads_path = folder / 'downloads.json'
        downloads = json.loads(downloads_path.read_text(encoding='utf-8')) if downloads_path.exists() else []
        categories = {row['index']: row['category'] for row in publications}
        downloads = update_paths(downloads)
        for result in downloads:
            result['category'] = categories.get(result['index'], result['category'])
        for source, target in moves:
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                source.rename(target)
        write_contract_json(manifest_path, manifest, 'register_publications')
        write_contract_json(downloads_path, downloads, 'register_downloads')
        for category in CATEGORIES:
            write_json(folder / category / 'publications.json', [row for row in publications if row['category'] == category])
            write_contract_json(folder / category / 'downloads.json',
                                [row for row in downloads if row['category'] == category], 'register_downloads')
    return changed


def main(argv=None):
    parser = ArgumentParser(description='Gespeicherte Registerdokumente ohne erneuten Download aufbereiten.')
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--refresh-html', type=Path, metavar='ROOT')
    actions.add_argument('--reclassify', type=Path, metavar='ROOT')
    args = parser.parse_args(argv)
    if args.refresh_html is not None:
        print(f'{refresh_html(args.refresh_html)} HTML-Veröffentlichungen neu aufbereitet.')
    else:
        print(f'{reclassify_saved(args.reclassify)} Veröffentlichungen neu eingeordnet.')
    return 0
