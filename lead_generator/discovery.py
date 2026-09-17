"""Deterministic directory discovery. Directory presence is never expo evidence."""
from urllib.parse import urljoin,urlparse
from bs4 import BeautifulSoup
from .store import now

DIRECTORY='https://www.roboticstomorrow.com/company_directory_search.php?Search=active'


def parse_directory(html,source_url=DIRECTORY):
    soup=BeautifulSoup(html,'html.parser')
    for e in soup.select('section.entry'):
        a=e.select_one('h3 a[href^="/company_directory/"]')
        if not a: continue
        meta=e.select_one('.entry-meta')
        desc=e.select_one('.entry-content')
        yield dict(profile_url=urljoin(source_url,a['href']), name=a.get_text(' ',strip=True),
                   location=meta.get_text(' ',strip=True) if meta else '',
                   description=desc.get_text(' ',strip=True) if desc else '',
                   source_url=source_url,collected_at=now())


def fetch_directory(session,store):
    # One request per pass, timeout, max bytes; no proxy/challenge circumvention.
    with session.get(DIRECTORY,timeout=(10,45),stream=True) as response:
        response.raise_for_status()
        if 'text/html' not in response.headers.get('Content-Type',''):
            raise ValueError('unexpected directory content type')
        parts=[]; size=0
        for part in response.iter_content(65536):
            size+=len(part)
            if size>8_000_000: raise ValueError('directory size limit')
            parts.append(part)
        html=b''.join(parts).decode(response.encoding or 'utf-8',errors='replace')
    rows=list(parse_directory(html))
    if len(rows)<100:
        raise ValueError('directory layout changed or challenge: expected >=100 profiles')
    for row in rows: store.seed(**row)
    store.event('DIRECTORY_COLLECTED',{'source':DIRECTORY,'profile_links':len(rows)})
    return len(rows)
