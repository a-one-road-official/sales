"""Deterministic public-source discovery for industrial lead production."""
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from .store import now

DIRECTORY = 'https://www.roboticstomorrow.com/company_directory_search.php?Search=active'
VDMA_DIRECTORY = 'https://www.vdma.eu/en/members'
VDMA_ROBOTICS = 'https://www.vdma.eu/en-GB/members-robotics-automation'
SOURCES = (VDMA_DIRECTORY, DIRECTORY)


def parse_directory(html, source_url=DIRECTORY):
    soup = BeautifulSoup(html, 'html.parser')
    for e in soup.select('section.entry'):
        a = e.select_one('h3 a[href^="/company_directory/"]')
        if not a:
            continue
        meta = e.select_one('.entry-meta')
        desc = e.select_one('.entry-content')
        yield dict(profile_url=urljoin(source_url, a['href']), name=a.get_text(' ', strip=True),
                   location=meta.get_text(' ', strip=True) if meta else '',
                   description=desc.get_text(' ', strip=True) if desc else '',
                   source_url=source_url, collected_at=now())


def _read_html(session, url, max_bytes=24_000_000):
    with session.get(url, timeout=(10, 45), stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        if 'text/html' not in response.headers.get('Content-Type', '').lower():
            raise ValueError('unexpected directory content type')
        parts, size = [], 0
        for part in response.iter_content(65536):
            size += len(part)
            if size > max_bytes:
                raise ValueError('directory size limit')
            parts.append(part)
        return b''.join(parts).decode(response.encoding or 'utf-8', errors='replace')


def fetch_directory(session, store):
    html = _read_html(session, DIRECTORY, 8_000_000)
    rows = list(parse_directory(html))
    if len(rows) < 100:
        raise ValueError('directory layout changed or challenge: expected >=100 profiles')
    for row in rows:
        store.seed(**row)
    store.event('DIRECTORY_COLLECTED', {'source': DIRECTORY, 'profile_links': len(rows)})
    return len(rows)


def _external_http(href, source_url):
    absolute = urljoin(source_url, str(href or '').strip())
    parsed = urlparse(absolute)
    source_host = (urlparse(source_url).hostname or '').lower()
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in {'http', 'https'} or not host:
        return ''
    if host == source_host or host.endswith('.vdma.eu') or host.endswith('.vdma.org'):
        return ''
    if host in {'linkedin.com', 'www.linkedin.com', 'facebook.com', 'www.facebook.com',
                'youtube.com', 'www.youtube.com', 'instagram.com', 'www.instagram.com',
                'x.com', 'twitter.com', 'www.twitter.com'}:
        return ''
    return absolute


def _member_card(anchor):
    node = anchor
    for _ in range(9):
        node = getattr(node, 'parent', None)
        if node is None:
            return None
        text = ' '.join(node.stripped_strings)
        low = text.casefold()
        has_contact = 'contact' in low or 'kontakt' in low
        has_address = 'address' in low or 'adresse' in low
        if has_contact and has_address and 35 <= len(text) <= 2600:
            return node
    return None


def _member_name(card, website_text=''):
    generic = {'contact', 'kontakt', 'address', 'adresse', 'image', 'more information', 'less information',
               'mehr information', 'weniger information'}
    for tag in card.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'strong', 'b']):
        value = re.sub(r'\s+', ' ', tag.get_text(' ', strip=True)).strip()
        if value and value.casefold() not in generic and 'http' not in value.casefold() and '@' not in value:
            return value[:300]
    lines = [re.sub(r'\s+', ' ', x).strip() for x in card.stripped_strings]
    for value in lines:
        low = value.casefold()
        if low in generic or low.startswith(('http://', 'https://', 'www.')) or '@' in value:
            continue
        if re.fullmatch(r'[+()\d\s./-]{6,}', value):
            continue
        if len(value) >= 2:
            return value[:300]
    return website_text[:300]


def _address_text(card):
    lines = [re.sub(r'\s+', ' ', x).strip() for x in card.stripped_strings]
    start = None
    for i, value in enumerate(lines):
        if value.casefold().rstrip(':') in {'address', 'adresse'}:
            start = i + 1
            break
    if start is None:
        return ''
    out = []
    for value in lines[start:start + 8]:
        if value.casefold() in {'image', 'more information', 'less information', 'mehr information', 'weniger information'}:
            break
        out.append(value)
    return ' '.join(out)[:700]


def parse_vdma(html, source_url=VDMA_DIRECTORY):
    """Extract VDMA member identity, website and address from the public member list."""
    soup = BeautifulSoup(html, 'html.parser')
    seen = set()
    for a in soup.find_all('a', href=True):
        website = _external_http(a.get('href'), source_url)
        if not website:
            continue
        card = _member_card(a)
        if card is None:
            continue
        key = (urlparse(website).hostname or '').lower().removeprefix('www.')
        if not key or key in seen:
            continue
        seen.add(key)
        name = _member_name(card, a.get_text(' ', strip=True))
        location = _address_text(card)
        if not name or not location:
            continue
        card_text = re.sub(r'\s+', ' ', card.get_text(' ', strip=True))
        yield dict(profile_url=website, name=name, location=location,
                   description=('VDMA machinery/equipment industry member. ' + card_text)[:1800],
                   source_url=source_url, collected_at=now())


def fetch_vdma(session, store, source_url=VDMA_DIRECTORY):
    html = _read_html(session, source_url)
    rows = list(parse_vdma(html, source_url))
    minimum = 40 if 'robotics-automation' in source_url else 200
    if len(rows) < minimum:
        raise ValueError(f'vdma layout changed or incomplete: expected >={minimum} profiles')
    for row in rows:
        store.seed(**row)
    store.event('DIRECTORY_COLLECTED', {'source': source_url, 'profile_links': len(rows)})
    return len(rows)


def fetch_source(session, store, source_url):
    if source_url == DIRECTORY:
        return fetch_directory(session, store)
    if source_url in {VDMA_DIRECTORY, VDMA_ROBOTICS}:
        return fetch_vdma(session, store, source_url)
    raise ValueError('unknown discovery source')
