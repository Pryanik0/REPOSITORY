"""PDF-Text seitenweise auslesen; keine OCR und keine verschlüsselten PDFs."""
from io import BytesIO
import re
from pypdf import PdfReader
import base64
import hashlib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .research_inputs import normalized, write_json


def read_pdf(content: bytes, fallback_title: str, max_pages: int = 300):
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise ValueError('Verschlüsseltes PDF: eine unverschlüsselte Datei verwenden.')
        if len(reader.pages) > max_pages:
            raise ValueError(f'PDF überschreitet das Limit von {max_pages} Seiten.')
        title = str((reader.metadata or {}).get('/Title') or fallback_title)
        pages, warnings = [], []
        for number, page in enumerate(reader.pages, start=1):
            try:
                text = re.sub(r'\s+', ' ', page.extract_text() or '').strip()
                if text:
                    pages.append((number, text))
                else:
                    warnings.append(f'Seite {number}: kein auslesbarer Text (leer oder OCR erforderlich)')
            except Exception as error:
                warnings.append(f'Seite {number}: Textextraktion fehlgeschlagen ({type(error).__name__})')
        if not pages:
            raise ValueError('PDF enthält keinen auslesbaren Text; möglicherweise ist OCR erforderlich. ' + '; '.join(warnings))
        return title, pages, warnings
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f'PDF kann nicht gelesen werden: {type(error).__name__}: {error}') from error


def extract_pdf(response, output, *, start, end, parse_date, keywords=(), published=None, title=''):
    """PDF-Original auch bei fehlendem Text oder unpassendem Veröffentlichungsdatum speichern."""
    url = response['final_url']
    identity = hashlib.sha256(url.encode()).hexdigest()[:20]
    folder = output / identity
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / 'original.pdf'
    original.write_bytes(base64.b64decode(response['body_b64'], validate=True))
    document = dict(url=url, pdf_path=str(original), fetched_at=response['fetched_at'],
                    status='downloaded_no_text', included=False)
    fallback = title or unquote(Path(urlsplit(url).path).name) or 'PDF-Dokument'
    article = None
    reason = None
    try:
        pdf_title, pages, warnings = read_pdf(original.read_bytes(), fallback)
        text = '\n\n'.join(text for _, text in pages)
        text_path = folder / 'document.txt'
        text_path.write_text(text + '\n', encoding='utf-8')
        pages_path = folder / 'pages.json'
        write_json(pages_path, [dict(page=number, text=value) for number, value in pages])
        document.update(status='extracted', title=pdf_title, text_path=str(text_path),
                        pages_path=str(pages_path), extracted_pages=len(pages), warnings=warnings)
        # PDF-Metadaten sind kein Veröffentlichungsnachweis. Zuerst Quelldatum verwenden,
        # danach ein sichtbares Datum am Anfang der ersten Seite oder in der URL.
        date_source = 'source_listing' if published else 'pdf_first_page'
        stamp = published or parse_date(pages[0][1][:300])
        if not stamp:
            stamp, date_source = parse_date(unquote(urlsplit(url).path)), 'url'
        if not stamp:
            reason = 'unknown_publication_date'
        elif not start <= stamp <= end:
            reason = 'outside_publication_window'
        elif keywords and not any(normalized(word) in normalized(pdf_title + '\n' + text) for word in keywords):
            reason = 'keyword_filter'
        else:
            article = dict(title=pdf_title, url=url, fetched_url=url, published_at=stamp.isoformat(),
                           date_source=date_source, text=text, format='pdf', pdf_path=str(original),
                           document_path=str(folder / 'document.json'), extracted_pages=len(pages),
                           warnings=warnings, content_hash=hashlib.sha256(normalized(text).encode()).hexdigest())
        if stamp:
            document.update(published_at=stamp.isoformat(), date_source=date_source)
    except ValueError as error:
        document['error'] = str(error)
        reason = 'pdf_text_unavailable'
    document.update(reason=reason)
    write_json(folder / 'document.json', document)
    return article, reason, document
