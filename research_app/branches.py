"""Gemeinsamer Branchenschlüssel für Firmen und Gewerkschaften."""
import re
import unicodedata


def branch_key(value):
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    text = re.sub(r'\bund\b|&', ' und ', text)
    return re.sub(r'\s+', ' ', text).strip()


CANONICAL = (
    'Abfallwirtschaft', 'Automobilindustrie', 'Banken', 'Bauindustrie',
    'Bildung & Wissenschaft', 'Biotechnologie', 'Branchenübergreifend',
    'Chemieindustrie', 'Digitale Wirtschaft', 'Einzelhandel', 'Elektroindustrie',
    'Energie & Wasser', 'Gastronomie & Hotellerie', 'Gesundheit & Sozialwesen',
    'Glasindustrie', 'Großhandel & Außenhandel', 'Handwerk', 'Immobilienwirtschaft',
    'IT & Telekommunikation', 'Journalisten und Medien', 'Keramikindustrie',
    'Kunststoffindustrie', 'Landwirtschaft', 'Lebensmittelindustrie', 'Logistik',
    'Luftfahrt', 'Maschinenbau', 'Metallindustrie', 'Mittelstand',
    'Öffentlicher Dienst', 'Pharma & Medizintechnik', 'Rechtsberatung',
    'Sozialversicherung', 'Technik und Naturwissenschaft',
    'Unternehmens- & Personalberatung', 'Versicherungen',
)
ALIASES = {branch_key(name): (name,) for name in CANONICAL}
ALIASES.update({branch_key(old): new for old, new in {
    'Transport, Logistik und Entsorgung': ('Logistik', 'Abfallwirtschaft'),
    'Transport & Logistik': ('Logistik',),
    'Digitale Wirtschaft und IT': ('Digitale Wirtschaft', 'IT & Telekommunikation'),
    'Chemieindustrie, Biotechnologie': ('Chemieindustrie', 'Biotechnologie'),
    'Glass, Kunststoffindustrie und Keramikindustrie':
        ('Glasindustrie', 'Kunststoffindustrie', 'Keramikindustrie'),
    'Glas, Kunststoffindustrie und Keramikindustrie':
        ('Glasindustrie', 'Kunststoffindustrie', 'Keramikindustrie'),
    'Bildung und Erziehung': ('Bildung & Wissenschaft',),
    'Pflege und Gesundheitswesen': ('Gesundheit & Sozialwesen',),
    'Sozialwesen': ('Gesundheit & Sozialwesen',),
    'Mittelstand, branchenübergreifend': ('Mittelstand', 'Branchenübergreifend'),
}.items()})


def normalize_branches(values):
    """Bekannte Sammelbegriffe aufteilen; unbekannte Branchen unverändert erhalten."""
    if isinstance(values, str):
        values = [values]
    result, seen = [], set()
    for value in values:
        for part in re.split(r'[;|]', value):
            part = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', part)).strip()
            for name in ALIASES.get(branch_key(part), (part,)):
                key = branch_key(name)
                if key and key not in seen:
                    result.append(name)
                    seen.add(key)
    return result
