"""Branchenzellen vereinheitlichen; Vorschau ohne --write, Sicherung vor jeder Änderung."""
import argparse
import csv
import hashlib
from io import StringIO
from pathlib import Path
import shutil

from openpyxl import load_workbook

from .branches import normalize_branches


def normalize_file(path, *, write=False):
    path = Path(path)
    original = path.read_bytes()
    changes = []
    book = None
    if path.suffix.lower() == '.xlsx':
        book = load_workbook(path)
        for sheet in book:
            columns = [cell.column for cell in sheet[1] if str(cell.value or '').strip().casefold() == 'branche']
            for column in columns:
                for row in range(2, sheet.max_row + 1):
                    cell = sheet.cell(row, column)
                    if cell.value is None:
                        continue
                    value = '; '.join(normalize_branches(str(cell.value)))
                    if value != cell.value:
                        changes.append(dict(sheet=sheet.title, row=row, before=cell.value, after=value))
                        cell.value = value
    else:
        try:
            text = original.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = original.decode('cp1252')
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t')
        reader = csv.DictReader(StringIO(text, newline=''), dialect=dialect)
        rows = list(reader)
        column = next((key for key in reader.fieldnames if key.strip().casefold() == 'branche'), None)
        if column:
            for number, row in enumerate(rows, 2):
                value = '; '.join(normalize_branches(row[column]))
                if value != row[column]:
                    changes.append(dict(row=number, before=row[column], after=value))
                    row[column] = value
    try:
        if changes and write:
            digest = hashlib.sha256(original).hexdigest()[:12]
            backup = path.parent / 'backups' / 'branch-normalization' / f'{path.stem}-{digest}{path.suffix}'
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(path, backup)
            temporary = path.with_name(path.stem + '.normalized' + path.suffix)
            if book:
                book.save(temporary)
            else:
                with temporary.open('w', encoding='utf-8', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=reader.fieldnames, dialect=dialect)
                    writer.writeheader()
                    writer.writerows(rows)
            temporary.replace(path)
    finally:
        if book:
            book.close()
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('files', nargs='*', type=Path, default=[Path('data/sources.csv'),
                        Path('data/Gewerkschaften.xlsx')])
    args = parser.parse_args()
    for path in args.files:
        changes = normalize_file(path, write=args.write)
        print(f'{path}: {len(changes)} Änderungen' + (' gespeichert' if args.write else ' (Vorschau)'))
        for row in changes:
            print(f'  {row["row"]}: {row["before"]} -> {row["after"]}')


if __name__ == '__main__':
    main()
