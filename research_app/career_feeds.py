"""Öffentliche Stellenfeeds bekannter, vom Arbeitgeber verlinkter Portale."""
from dataclasses import dataclass
from html import unescape
import json
import re
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from .site_http import clean_url, host_key


GREENHOUSE_HOSTS = ('boards.greenhouse.io', 'job-boards.greenhouse.io')
TOKEN = re.compile(r'[A-Za-z0-9_-]+\Z')


def greenhouse_tenant(url):
    parts = urlsplit(url)
    if host_key(url) not in GREENHOUSE_HOSTS:
        return None
    path = parts.path.strip('/').split('/')
    tenant = (parse_qs(parts.query).get('for', [''])[0] if path[0] == 'embed' else path[0])
    return tenant if TOKEN.fullmatch(tenant) else None


@dataclass(frozen=True)
class JobFeed:
    provider: str
    url: str
    portal: str
    tenant: str = ''


def discover_feed(url):
    """Nur dokumentierte Endpunkte aus echten Portaladressen ableiten, keine Firmennamen raten."""
    parts = urlsplit(url)
    host = host_key(url)
    if re.fullmatch(r'[a-z0-9-]+\.jobs\.personio\.(?:de|com)', host):
        if (parts.path.rstrip('/') in ('', '/xml')
                or re.fullmatch(r'/job/\d+/?', parts.path)):
            origin = f'{parts.scheme}://{parts.netloc}'
            return JobFeed('personio', origin + '/xml', origin)
    tenant = greenhouse_tenant(url)
    if tenant and (parts.path.strip('/') == tenant or parts.path.startswith('/embed/job_board')):
        return JobFeed('greenhouse', f'https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs?content=true',
                       f'https://{host}/{tenant}', tenant)
    ashby = (re.fullmatch(r'/posting-api/job-board/([A-Za-z0-9_-]+)/?', parts.path)
             if host == 'api.ashbyhq.com' else
             re.fullmatch(r'/([A-Za-z0-9_-]+)(?:/[A-Za-z0-9_-]+)?/?', parts.path)
             if host == 'jobs.ashbyhq.com' else None)
    if ashby:
        tenant = ashby[1]
        return JobFeed('ashby', f'https://api.ashbyhq.com/posting-api/job-board/{tenant}',
                       f'https://jobs.ashbyhq.com/{tenant}', tenant)
    return None


def embedded_feed_urls(script):
    """Veröffentlichte fetch-URLs lesen; nur Stringkonstanten, kein JavaScript ausführen."""
    assignments = dict(re.findall(r'\b(?:var|let|const)\s+([\w$]+)\s*=\s*([^;\n]+)', script))

    def resolve(expression, depth=0):
        expression = expression.strip()
        if depth > 3:
            return ''
        literal = re.fullmatch(r'''(['"])([^'"\r\n]*)\1(?:\s*\+\s*([\w$]+))?''', expression)
        if literal:
            suffix = resolve(literal[3], depth + 1) if literal[3] else ''
            return literal[2] + suffix if not literal[3] or suffix else ''
        if expression in assignments:
            return resolve(assignments[expression], depth + 1)
        return ''

    urls = []
    for expression in re.findall(r'\bfetch\s*\(\s*([^,)\r\n]+)', script):
        url = clean_url(resolve(expression))
        if url and discover_feed(url) and url not in urls:
            urls.append(url)
    return urls


def read_feed(feed, content):
    """Feed nach JobPosting übersetzen; kaputte Antworten sind keine belegten Leerlisten."""
    if feed.provider == 'personio':
        # Keine DTDs/Entities aus fremden XML-Antworten verarbeiten.
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', content, re.I):
            raise ValueError('DTD/Entities im Stellenfeed werden nicht unterstützt.')
        root = ElementTree.fromstring(content)
        if root.tag != 'workzag-jobs' or any(node.tag != 'position' for node in root):
            raise ValueError('Unbekanntes Personio-Feedformat.')
        rows = []
        for node in root:
            reference, title = node.findtext('id', '').strip(), node.findtext('name', '').strip()
            if not reference.isdigit() or not title:
                raise ValueError('Personio-Stelle ohne gültige ID oder Titel.')
            descriptions = ['\n'.join(filter(None, (block.findtext('name'), block.findtext('value'))))
                            for block in node.findall('jobDescriptions/jobDescription')]
            offices = [node.findtext('office', '')] + [office.text or '' for office in node.findall('additionalOffices/office')]
            rows.append(dict(title=title, url=feed.portal + '/job/' + reference,
                             description='\n\n'.join(descriptions), identifier=reference,
                             hiringOrganization=node.findtext('subcompany', ''),
                             jobLocation=[dict(address=dict(addressLocality=office)) for office in dict.fromkeys(offices) if office],
                             employmentType=[node.findtext('employmentType', ''), node.findtext('schedule', '')]))
        return rows
    if feed.provider == 'greenhouse':
        data = json.loads(content)
        if not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
            raise ValueError('Unbekanntes Greenhouse-Feedformat.')
        rows = []
        total = data.get('meta', {}).get('total')
        if total is not None and total != len(data['jobs']):
            raise ValueError('Unvollständiger Greenhouse-Stellenfeed.')
        for item in data['jobs']:
            if not isinstance(item, dict) or not item.get('title') or not clean_url(item.get('absolute_url')):
                raise ValueError('Greenhouse-Stelle ohne Titel oder gültige URL.')
            rows.append(dict(title=item['title'], url=item['absolute_url'],
                             description=unescape(unescape(item.get('content') or '')),
                             identifier=str(item.get('id') or ''),
                             jobLocation=dict(address=dict(addressLocality=(item.get('location') or {}).get('name', ''))),
                             validThrough=item.get('application_deadline'),
                             _general_application=('internal_job_id' in item and item['internal_job_id'] is None)))
        return rows
    if feed.provider == 'ashby':
        data = json.loads(content)
        if not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
            raise ValueError('Unbekanntes Ashby-Stellenfeedformat.')
        rows = []
        for item in data['jobs']:
            if not isinstance(item, dict) or not isinstance(item.get('isListed'), bool):
                raise ValueError('Ashby-Stelle ohne Veröffentlichungsstatus.')
            if not item['isListed']:
                continue
            if not item.get('title') or not clean_url(item.get('jobUrl')):
                raise ValueError('Ashby-Stelle ohne Titel oder gültige URL.')
            offices = [item.get('location', '')] + [value.get('location', '')
                                                    for value in item.get('secondaryLocations', [])]
            rows.append(dict(title=item['title'], url=item['jobUrl'],
                             description=item.get('descriptionHtml') or item.get('descriptionPlain') or '',
                             jobLocation=[dict(address=dict(addressLocality=value)) for value in offices if value],
                             employmentType=item.get('employmentType'), jobLocationType=item.get('workplaceType'),
                             datePosted=item.get('publishedAt')))
        return rows
    raise ValueError('Unbekannter Stellenfeed.')
