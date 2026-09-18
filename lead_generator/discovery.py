"""Deterministic public-source discovery for industrial lead production."""
import json
import re
import time
from urllib.parse import parse_qs, urljoin, urlparse
from bs4 import BeautifulSoup
from .store import now

DIRECTORY = 'https://www.roboticstomorrow.com/company_directory_search.php?Search=active'
VDMA_DIRECTORY = 'https://www.vdma.eu/en/members'
VDMA_ROBOTICS = 'https://www.vdma.eu/en-GB/members-robotics-automation'
TAIROS_DIRECTORY = 'https://www.tairos.tw/en/visitorExhibitor.asp?Area=&sort=name&view=list'

_RESOURCE_QUERY = (
    'p_p_cacheability=cacheLevelPage&'
    'p_p_lifecycle=2&p_p_mode=view&p_p_resource_id=getPage&p_p_state=normal'
)
VDMA_RESOURCE_SOURCES = (
    'https://www.vdma.eu/en/members?'
    'p_p_id=org_vdma_publicusers_portlet_PublicUsersPortlet_INSTANCE_kFQ0SZSZDPTt&' + _RESOURCE_QUERY,
    'https://www.vdma.eu/de/mitglieder-robotik-automation?'
    'p_p_id=org_vdma_publicusers_portlet_PublicUsersPortlet_INSTANCE_kFQ0SZSZDPTt&' + _RESOURCE_QUERY,
    'https://www.vdma.org/en/members-wg-additive-manufacturing?'
    'p_p_id=org_vdma_publicusers_portlet_PublicUsersPortlet_INSTANCE_saKOZNxEwUzE&' + _RESOURCE_QUERY,
    'https://vdma.eu/en/members-robotics?'
    'p_p_id=org_vdma_publicusers_portlet_PublicUsersPortlet_INSTANCE_lma5BmtcnQxi&' + _RESOURCE_QUERY,
    'https://vdma.eu/en/members-process-plant-equipment?'
    'p_p_id=org_vdma_publicusers_portlet_PublicUsersPortlet_INSTANCE_kFQ0SZSZDPTt&' + _RESOURCE_QUERY,
    'https://www.vdma.eu/en/memberlist-electronics-solar-battery-production?'
    'p_p_id=org_vdma_publicusers_portlet_PublicUsersPortlet_INSTANCE_kFQ0SZSZDPTt&' + _RESOURCE_QUERY,
    'https://www.vdma.org/mitglieder-foerdertechnik-intralogistik?'
    'p_p_id=org_vdma_publicusers_portlet_PublicUsersPortlet_INSTANCE_kFQ0SZSZDPTt&' + _RESOURCE_QUERY,
)
SOURCES = VDMA_RESOURCE_SOURCES + (TAIROS_DIRECTORY, DIRECTORY,)


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
    """Static fallback for VDMA pages when the resource endpoint is unavailable."""
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


def _normalise_webaddr(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if value.startswith('//'):
        value = 'https:' + value
    elif not re.match(r'^https?://', value, re.I):
        value = 'https://' + value.lstrip('/')
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return ''
    return value


def _decode_resource_payload(response):
    data = response.json()
    nested = data.get('publicUserList', data)
    if isinstance(nested, str):
        nested = json.loads(nested)
    if not isinstance(nested, dict) or not isinstance(nested.get('content'), list):
        raise ValueError('vdma_resource_schema_changed')
    return nested


def parse_vdma_resource(payload, source_url):
    """Convert one VDMA public-user JSON page into deterministic seed records."""
    for item in payload.get('content', []):
        name = re.sub(r'\s+', ' ', str(item.get('companyName') or '')).strip()
        website = _normalise_webaddr(item.get('webAddr'))
        if not name or not website:
            continue
        location = ' '.join(str(item.get(k) or '').strip() for k in ('address', 'plz', 'city', 'country')).strip()
        detail = ' | '.join(x for x in [
            'VDMA machinery/equipment industry member',
            f"country={item.get('country')}" if item.get('country') else '',
            f"city={item.get('city')}" if item.get('city') else '',
            f"email={item.get('email')}" if item.get('email') else '',
        ] if x)
        yield dict(profile_url=website, name=name, location=location, description=detail[:1800],
                   source_url=source_url, collected_at=now())


def _resource_get(session, source_url, extra_params=None):
    with session.get(source_url, params=extra_params or {}, timeout=(8, 30), allow_redirects=True) as response:
        response.raise_for_status()
        return _decode_resource_payload(response)


def _pagination_probe(session, source_url, first):
    """Detect the live Liferay page parameter instead of assuming a portal version."""
    query = parse_qs(urlparse(source_url).query)
    portlet = (query.get('p_p_id') or [''])[0]
    namespace = '_' + portlet + '_' if portlet else ''
    first_ids = tuple(str(x.get('companyName') or '') + '|' + str(x.get('webAddr') or '')
                      for x in first.get('content', []))
    candidates = []
    for key in ('page', 'currentPage', 'cur'):
        if namespace:
            candidates.append((namespace + key, 2 if key == 'cur' else 1))
        candidates.append((key, 2 if key == 'cur' else 1))
    for key, value in candidates:
        try:
            probe = _resource_get(session, source_url, {key: value})
        except Exception:
            continue
        probe_ids = tuple(str(x.get('companyName') or '') + '|' + str(x.get('webAddr') or '')
                          for x in probe.get('content', []))
        if probe_ids and probe_ids != first_ids:
            return key, value, probe
    return None, None, None


def fetch_vdma_resource(session, store, source_url):
    first = _resource_get(session, source_url)
    pages = [first]
    total_pages = max(1, int(first.get('totalPages') or 1))
    total_records = max(0, int(first.get('totalRecords') or 0))
    page_key = None

    if total_pages > 1:
        page_key, page_value, probe = _pagination_probe(session, source_url, first)
        if page_key and probe:
            pages.append(probe)
            seen_fingerprints = {
                tuple(str(x.get('companyName') or '') + '|' + str(x.get('webAddr') or '')
                      for x in page.get('content', []))
                for page in pages
            }
            next_value = page_value + 1
            while len(pages) < total_pages:
                page = _resource_get(session, source_url, {page_key: next_value})
                fingerprint = tuple(str(x.get('companyName') or '') + '|' + str(x.get('webAddr') or '')
                                    for x in page.get('content', []))
                if not fingerprint or fingerprint in seen_fingerprints:
                    break
                seen_fingerprints.add(fingerprint)
                pages.append(page)
                next_value += 1
                time.sleep(0.05)

    rows, seen = [], set()
    for page in pages:
        for row in parse_vdma_resource(page, source_url):
            key = (urlparse(row['profile_url']).hostname or '').lower().removeprefix('www.')
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(row)
    if len(rows) < 8:
        raise ValueError('vdma_resource_empty_or_changed')
    for row in rows:
        store.seed(**row)
    store.event('DIRECTORY_COLLECTED', {
        'source': source_url, 'profile_links': len(rows), 'reported_total': total_records,
        'reported_pages': total_pages, 'pages_retrieved': len(pages), 'pagination_key': page_key or 'unresolved',
    })
    return len(rows)


def fetch_vdma(session, store, source_url=VDMA_DIRECTORY):
    html = _read_html(session, source_url)
    rows = list(parse_vdma(html, source_url))
    if len(rows) < 8:
        raise ValueError('vdma layout changed or incomplete')
    for row in rows:
        store.seed(**row)
    store.event('DIRECTORY_COLLECTED', {'source': source_url, 'profile_links': len(rows)})
    return len(rows)


def parse_tairos_list(html, source_url=TAIROS_DIRECTORY):
    """Extract unique exhibitor detail links from the official Taiwan automation show directory."""
    soup = BeautifulSoup(html, 'html.parser')
    seen = set()
    for a in soup.find_all('a', href=True):
        href = urljoin(source_url, a.get('href'))
        parsed = urlparse(href)
        if 'tairos.tw' not in (parsed.hostname or '').lower() or 'visitorExhibitorDetail.asp' not in parsed.path:
            continue
        name = re.sub(r'\s+', ' ', a.get_text(' ', strip=True)).strip()
        if not name or name.casefold() in {'more', 'detail', 'website'}:
            continue
        if href in seen:
            continue
        seen.add(href)
        yield dict(profile_url=href, name=name[:300], location='Taiwan',
                   description='Taiwan Automation Intelligence and Robot Show 2026 official exhibitor',
                   source_url=source_url, collected_at=now())


def fetch_tairos(session, store, source_url=TAIROS_DIRECTORY):
    rows, seen = [], set()
    empty_streak = 0
    for page in range(1, 61):
        sep = '&' if '?' in source_url else '?'
        page_url = source_url + sep + 'page=' + str(page)
        html = _read_html(session, page_url, 6_000_000)
        current = list(parse_tairos_list(html, source_url))
        new = [row for row in current if row['profile_url'] not in seen]
        if not new:
            empty_streak += 1
            if empty_streak >= 2:
                break
            continue
        empty_streak = 0
        for row in new:
            seen.add(row['profile_url'])
            rows.append(row)
        time.sleep(0.05)
    if len(rows) < 50:
        raise ValueError('tairos_layout_changed_or_incomplete')
    for row in rows:
        store.seed(**row)
    store.event('DIRECTORY_COLLECTED', {
        'source': source_url, 'profile_links': len(rows), 'country': 'Taiwan',
    })
    return len(rows)


def fetch_source(session, store, source_url):
    if source_url == DIRECTORY:
        return fetch_directory(session, store)
    if source_url == TAIROS_DIRECTORY:
        return fetch_tairos(session, store, source_url)
    if source_url in VDMA_RESOURCE_SOURCES:
        return fetch_vdma_resource(session, store, source_url)
    if source_url in {VDMA_DIRECTORY, VDMA_ROBOTICS}:
        return fetch_vdma(session, store, source_url)
    raise ValueError('unknown discovery source')
