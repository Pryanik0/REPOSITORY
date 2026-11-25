"""Kununu-Profile und eine begrenzte Stichprobe datierter Mitarbeiterbewertungen auslesen."""
from datetime import datetime, timezone
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .research_inputs import profile_url

MAX_REVIEWS = 15


def review_window(now=None):
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    try:
        start = now.replace(year=now.year - 2)
    except ValueError:  # Für den 29. Februar bei Bedarf den 28. Februar verwenden.
        start = now.replace(year=now.year - 2, day=28)
    return start.replace(hour=0, minute=0, second=0, microsecond=0), now


def review_date(value):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def aggregate_factor_scores(reviews):
    values = {}
    for review in reviews:
        for factor, score in review.get('factor_scores', {}).items():
            if isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= 5:
                values.setdefault(factor, []).append(score)
    return {factor: dict(mean=round(sum(scores) / len(scores), 2), count=len(scores))
            for factor, scores in sorted(values.items())}


def next_review_page(html, root, current_url=None):
    soup = page_soup(html)
    embedded_state(soup, root)
    # Hydrierte Seiten laden per Button nach. Redux-Seitenzahlen sind dort oft veraltet.
    for link in soup.select('a[href], link[rel~="next"][href]'):
        if not ('next' in link.get('rel', []) or link.get('id') == 'reviews-read-more-cta' or
                re.search(r'(?:mehr|weitere) Bewertungen lesen', link.get_text(' ', strip=True), re.I)):
            continue
        url = urljoin(current_url or root, link['href'])
        if (profile_url(url) == root and
                urlsplit(url).path.startswith(urlsplit(root).path + '/kommentare')):
            return url
    return None


def missing_review_navigation(html, root):
    state = embedded_state(page_soup(html), root)
    section = (state or {}).get('reviews', {})
    page, pages = section.get('page'), section.get('pagesCount')
    return isinstance(page, int) and isinstance(pages, int) and page < pages


def has_review_button(html):
    return BeautifulSoup(html, 'html.parser').select_one(
        'button#reviews-read-more-cta:not([disabled]):not([aria-disabled="true"])') is not None


def reviews_require_login(html):
    """Nur ein tatsächlich dargestelltes Login-Gate erkennen, keine Feature-Flags."""
    soup = BeautifulSoup(html, 'html.parser')
    return soup.select_one('[data-testid="reviews-gate"], #reviews-gate-cta-button') is not None


def review_ids(html, root):
    """Alle eigenen Karten zählen, auch ältere/Azubi-Bewertungen zur Fortschrittskontrolle."""
    soup = page_soup(html)
    state = embedded_state(soup, root)
    ids = {row.get('uuid') for row in (state or {}).get('reviews', {}).get('reviews', [])
           if isinstance(row, dict) and row.get('uuid')}
    ids.update(card.get('data-testid', '').removeprefix('review-')
               for card in soup.select('article[data-testid^="review-"]') if card.select_one('time[datetime]'))
    return ids


class ScrapeError(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def number(value, *, integer=False):
    if value is None:
        return None
    text = str(value).strip().replace('\u00a0', '').replace(' ', '')
    if integer:
        return int(text.replace('.', '').replace(',', '')) if re.fullmatch(r'\d[\d.,]*', text) else None
    try:
        return float(text.replace(',', '.').removesuffix('%'))
    except ValueError:
        return None


def block_kind(soup):
    labels = ' '.join(tag.get_text(' ', strip=True) for tag in soup.select('title, h1')).casefold()
    scripts = soup.select('script')
    aws_scripts = [tag for tag in scripts if
                   (urlsplit(tag.get('src', '')).hostname or '').endswith('.awswaf.com')]
    bootstrap = any('gokuProps' in (tag.string or tag.get_text()) for tag in scripts)
    if aws_scripts and bootstrap and soup.select_one('#__NEXT_DATA__') is None:
        if any('captcha.awswaf.com' in tag.get('src', '') for tag in aws_scripts):
            return 'aws_waf_captcha'
        return 'aws_waf_javascript'
    if any(label in labels for label in (
        'human verification', 'verify you are human', 'access denied',
        'request blocked', 'just a moment', 'captcha')):
        return 'verification'
    return None


def blocked(soup):
    return block_kind(soup) is not None


def page_soup(html):
    soup = BeautifulSoup(html, 'html.parser')
    if blocked(soup):
        raise ScrapeError('blocked', 'Kununu hat eine Sicherheitsabfrage geliefert. '
                          'Gespeichertes HTML prüfen; der automatische Zugriff wurde gesperrt.')
    for tag in soup.select('script:not([type="application/ld+json"]):not(#__NEXT_DATA__), style, nav, footer, aside'):
        tag.decompose()
    # Das Login-Gate zeigt eine künstliche, unscharfe Beispielbewertung.
    for tag in soup.select('[class*="reviewsGateBlurContainer"]'):
        tag.decompose()
    return soup


def embedded_state(soup, root):
    tag = soup.select_one('script#__NEXT_DATA__')
    if tag is None:
        return None
    try:
        state = json.loads(tag.string or tag.get_text())['props']['initialReduxState']
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get('profile'), dict):
        return None
    profile = state['profile']
    country, slug = profile.get('countryCode'), profile.get('slug')
    if not isinstance(country, str) or not isinstance(slug, str):
        return None
    if profile_url(f'https://www.kununu.com/{country}/{slug}') != root:
        raise ScrapeError('unexpected_profile', 'Die eingebetteten Daten gehören zu einem anderen Arbeitgeberprofil.')
    return state


def plain_text(value):
    return BeautifulSoup(value, 'html.parser').get_text('\n', strip=True) if isinstance(value, str) else ''


def visible_review_rows(soup):
    """Vollständige Bewertungskarten ohne eingebettete Redux-Daten auslesen."""
    factors = {
        'Arbeitsatmosphäre': 'atmosphere', 'Image': 'image', 'Work-Life-Balance': 'workLife',
        'Karriere/Weiterbildung': 'career', 'Gehalt/Benefits': 'salary',
        'Umwelt-/Sozialbewusstsein': 'environment', 'Kollegenzusammenhalt': 'teamwork',
        'Umgang mit älteren Kollegen': 'oldColleagues', 'Vorgesetztenverhalten': 'leadership',
        'Arbeitsbedingungen': 'workConditions', 'Kommunikation': 'communication',
        'Gleichberechtigung': 'equality', 'Interessante Aufgaben': 'tasks',
    }
    sections = {'Gut am Arbeitgeber finde ich': 'positive',
                'Schlecht am Arbeitgeber finde ich': 'negative',
                'Verbesserungsvorschläge': 'suggestion'}
    rows = []
    for card in soup.select('article[data-testid^="review-"]'):
        if not card.select_one('time[datetime]'):
            continue  # Empfehlungen anderer Arbeitgeber sind keine Bewertungskarten.
        for reply in card.select('[data-test="employer-response"], [data-test="employer-comment"], '
                                 '[data-testid="company-response"], #employer-comments'):
            reply.decompose()
        time = card.select_one('time[datetime]')
        title = card.select_one('h3')
        score = card.select_one('[data-testid="review-rating"]')
        recommendation = card.select_one('[data-testid="review-recommendation"]')
        employment = card.select_one('[class*="employmentInfoBlock"] b')
        label = employment.get_text(' ', strip=True) if employment else ''
        if re.search(r'auszubild|azubi|lehrling', label, re.I):
            review_type = 'apprenticeship'
        elif re.search(r'bewerb', label, re.I):
            review_type = 'applicant'
        else:
            review_type = 'employer' if label else 'unknown'
        row = dict(uuid=card['data-testid'].removeprefix('review-'), type=review_type,
                   publishedAt=time.get('datetime') if time else None,
                   title=title.get_text(' ', strip=True) if title else None,
                   score=score.get_text(strip=True) if score else None, texts=[], ratings=[])
        if recommendation:
            value = recommendation.get_text(' ', strip=True)
            row['recommended'] = {'Empfohlen': True, 'Nicht empfohlen': False}.get(value)
        for heading in card.select('h4'):
            label = heading.get_text(' ', strip=True)
            if label not in factors and label not in sections:
                continue
            block = heading.parent
            text = '\n'.join(p.get_text('\n', strip=True) for p in block.select('p'))
            if label in sections:
                row['texts'].append(dict(id=sections[label], text=text))
            else:
                rating = block.select_one('[data-score]')
                row['ratings'].append(dict(id=factors[label], text=text,
                                           score=rating.get('data-score') if rating else None))
        rows.append(row)
    return rows


def embedded_reviews(state, root, limit, since, until):
    section = state.get('reviews')
    rows = section.get('reviews') if isinstance(section, dict) else None
    if not isinstance(rows, list):
        return []
    reviews, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get('type') != 'employer':
            continue
        published = row.get('publishedAt') or row.get('createdAt')
        date = review_date(published)
        if date is None or not since <= date <= until:
            continue
        identity = row.get('uuid')
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identity) or identity in seen:
            continue
        sections, comments, scores = {}, {}, {}
        for field, target in (('texts', sections), ('ratings', comments)):
            values = row.get(field)
            for item in values if isinstance(values, list) else []:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                    continue
                label = item['id']
                text = plain_text(item.get('text'))
                if text:
                    target[label] = '\n'.join(filter(None, (target.get(label), text)))
                rating = number(item.get('score'))
                if field == 'ratings' and rating is not None and 0 <= rating <= 5:
                    scores[label] = rating
        title = plain_text(row.get('title')) or None
        rating = number(row.get('score'))
        rating = rating if rating is not None and 0 <= rating <= 5 else None
        content = '\n\n'.join([*sections.values(), *comments.values()])
        if not title and not content and rating is None:
            continue
        seen.add(identity)
        # responses enthält Arbeitgeberantworten und wird bewusst nicht ausgelesen.
        reviews.append(dict(id=identity, url=root + '/bewertung/' + identity, title=title,
                            content=content, rating=rating, sections=sections,
                            factor_comments=comments, factor_scores=scores,
                            published_at=published,
                            recommended=row.get('recommended') if isinstance(row.get('recommended'), bool) else None,
                            review_type='employee', has_text=bool(content)))
        if len(reviews) >= limit:
            break
    return reviews


def parse_profile(html, root):
    soup = page_soup(html)
    embedded_state(soup, root)  # Eingebettete Daten eines fremden Profils vor der Auswertung ablehnen.
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical and profile_url(urljoin(root, canonical['href'])) != root:
        raise ScrapeError('unexpected_profile', 'Die kanonische HTML-Adresse gehört zu einer anderen Seite.')
    main = soup.select_one('main') or soup
    heading = main.select_one('h1') or soup.select_one('h1')
    name = heading.get_text(' ', strip=True) if heading else ''
    name = re.sub(r'\s+(?:als Arbeitgeber|Bewertungen)\s*$', '', name).strip()
    if not name:
        raise ScrapeError('unusable_profile', 'Keine Arbeitgeberüberschrift im empfangenen HTML.')
    text = main.get_text(' ', strip=True)
    score_match = re.search(r'([0-5][.,]\d+)\s*kununu\s*Score', text, re.I)
    count_match = re.search(r'kununu\s*Score\s*([\d.\s]+)\s*Bewertung', text, re.I)
    score_tag = main.select_one('[data-test="overall-rating"]')
    score = number(score_match.group(1)) if score_match else number(
        score_tag.get_text(' ', strip=True) if score_tag else None)
    count = number(count_match.group(1), integer=True) if count_match else None
    if count is None:
        relative = urlsplit(root).path + '/kommentare'
        link = soup.select_one(f'a[href="{root}/kommentare"], a[href="{relative}"]')
        match = re.search(r'Bewertungen?\s*\(([\d.]+)\)', link.get_text(' ', strip=True)) if link else None
        count = number(match.group(1), integer=True) if match else None
    # Schema.org-Daten nur für diesen Arbeitgeber übernehmen, keine Werbeeinträge.
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            nodes = json.loads(tag.string or tag.get_text())
        except (ValueError, TypeError):
            continue
        pending = nodes if isinstance(nodes, list) else [nodes]
        while pending:
            node = pending.pop()
            if not isinstance(node, dict):
                continue
            pending.extend(node.get('@graph', []) if isinstance(node.get('@graph'), list) else [])
            if (profile_url(node.get('url')) != root or not isinstance(node.get('name'), str)
                    or node['name'].casefold() != name.casefold()):
                continue
            rating = node.get('aggregateRating')
            if isinstance(rating, dict):
                score = score if score is not None else number(rating.get('ratingValue'))
                count = count if count is not None else number(rating.get('reviewCount', rating.get('ratingCount')), integer=True)
    if score is not None and not 0 <= score <= 5:
        score = None
    if score is None and count is None:
        raise ScrapeError('unusable_profile', 'Arbeitgeberüberschrift gefunden, aber keine lesbare Bewertung oder Bewertungsanzahl. '
                          'Die Seite benötigt möglicherweise JavaScript oder angepasste Selektoren; gespeichertes HTML prüfen.')
    recommendation = re.search(r'(\d{1,3})\s*%\s*(?:\d{1,3}\s*)?Weiterempfehlung', text)
    percent = int(recommendation.group(1)) if recommendation else None
    return dict(url=root, name=name, overall_score=score, review_count=count,
                recommendation_percent=percent if percent is not None and percent <= 100 else None,
                recommendation_period='last_2_years' if 'Letzte 2 Jahre' in text else None,
                observed_at=datetime.now(timezone.utc).isoformat())


def parse_reviews(html, root, limit, *, since=None, until=None):
    if limit <= 0:
        return []
    limit = min(limit, MAX_REVIEWS)
    start, end = review_window(until)
    since, until = since or start, until or end
    soup = page_soup(html)
    state = embedded_state(soup, root)
    if state is not None:
        # Nach einem Klick bleibt __NEXT_DATA__ oft auf Seite 1. Neue sichtbare Karten ergänzen.
        rows = [dict(row) for row in state.get('reviews', {}).get('reviews', []) if isinstance(row, dict)]
        known = {row.get('uuid'): row for row in rows}
        for visible in visible_review_rows(soup):
            row = known.get(visible['uuid'])
            if row is None:
                rows.append(visible)
                continue
            # Hydrierte Karten können zusätzliche Teilnoten enthalten; vorhandene
            # exakte Redux-Werte nicht mit gerundeten DOM-Sternen überschreiben.
            ratings = {item['id']: dict(item) for item in row.get('ratings', [])
                       if isinstance(item, dict) and isinstance(item.get('id'), str)}
            for item in visible['ratings']:
                existing = ratings.setdefault(item['id'], dict(item))
                if number(existing.get('score')) is None and number(item.get('score')) is not None:
                    existing['score'] = item['score']
            row['ratings'] = list(ratings.values())
        return embedded_reviews(dict(reviews=dict(reviews=rows)), root, limit, since, until)
    cards = visible_review_rows(soup)
    if cards:
        return embedded_reviews(dict(reviews=dict(reviews=cards)), root, limit, since, until)
    containers = soup.select('[data-test="review-item"], article')
    reviews, seen = [], set()
    for container in containers:
        time = container.select_one('time[datetime]')
        published = time.get('datetime') if time else None
        date = review_date(published)
        if date is None or not since <= date <= until:
            continue
        link = container.select_one('a[href*="/bewertung/"]')
        url = urljoin(root, link['href']) if link else None
        if url and profile_url(url) != root:
            continue
        if container.name == 'article' and not url:
            continue  # Werbung und Firmenzusammenfassungen nicht als Bewertungen übernehmen.
        for reply in container.select('[data-test="employer-response"], [data-test="employer-comment"], '
                                      '[data-testid="company-response"], #employer-comments'):
            reply.decompose()
        title = container.select_one('h3, h2')
        body = container.select_one('[data-test="content"]')
        score = container.select_one('[data-test="rating"]')
        title = title.get_text(' ', strip=True) if title else None
        content = body.get_text(' ', strip=True) if body else None
        # Bei abweichendem Layout nur gekennzeichnete Abschnitte der Mitarbeiterbewertung lesen.
        if content is None:
            parts = []
            for label in container.select('h3, h4'):
                if label.get_text(' ', strip=True) not in (
                        'Gut am Arbeitgeber finde ich', 'Schlecht am Arbeitgeber finde ich',
                        'Verbesserungsvorschläge'):
                    continue
                for sibling in label.next_siblings:
                    if getattr(sibling, 'name', '') in ('h2', 'h3', 'h4'):
                        break
                    value = sibling.get_text(' ', strip=True) if hasattr(sibling, 'get_text') else str(sibling).strip()
                    if value:
                        parts.append(value)
            content = '\n'.join(parts) or None
        if not content:
            continue
        identity = url or (title, content)
        if identity in seen:
            continue
        seen.add(identity)
        rating = number(score.get_text(' ', strip=True)) if score else None
        reviews.append(dict(url=url, title=title, content=content,
                            rating=rating if rating is not None and 0 <= rating <= 5 else None,
                            published_at=published))
        if len(reviews) >= limit:
            break
    return reviews
