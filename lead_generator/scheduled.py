"""Free-runner industrial lead discovery, bounded public research and dual-list intake.

The worker performs no paid model inference and no customer-facing send. It
turns deterministic public directory entries into auditable candidate records,
applies the current high-recall gate, and promotes PASS records to both lists.
"""
import json
import os
import re
import tempfile
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .checkpoint import SheetCheckpoint
from .discovery import DIRECTORY, VDMA_DIRECTORY, SOURCES, fetch_source
from .policy import domain, normalize_country, classify
from .store import Store, now
from .sync import Sheets, Mirror

CONFIG = {
    'run_id': 'overnight-20260917',
    'ssot': {'spreadsheet_id': '1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo', 'tab': '営業リスト＿Factory/BPO'},
    'sacrifice': {'spreadsheet_id': '1QBZKoN82O-SrFUnWaHBQtvflcdMT1gDp-QMPtZvLsEk', 'tab': '営業リスト_Vendor'},
    'control': {'tab': 'Sales Control'},
}

USER_AGENT = 'A-one-road-public-industrial-research/2.0'
SOCIAL_DOMAINS = {
    'linkedin.com', 'facebook.com', 'instagram.com', 'youtube.com', 'youtu.be', 'x.com', 'twitter.com',
}
COUNTRY_NAMES = (
    'United States of America', 'United States', 'South Korea', 'Republic of Korea', 'United Kingdom',
    'Czech Republic', 'New Zealand', 'Saudi Arabia', 'United Arab Emirates', 'Hong Kong',
    'Germany', 'Deutschland', 'Austria', 'Österreich', 'Switzerland', 'Schweiz', 'Poland', 'Polen',
    'France', 'Italy', 'Spain', 'Portugal', 'Netherlands', 'Niederlande', 'Belgium', 'Belgien',
    'Denmark', 'Dänemark', 'Sweden', 'Schweden', 'Norway', 'Norwegen', 'Finland', 'Finnland',
    'Czechia', 'Tschechien', 'Slovakia', 'Slowakei', 'Slovenia', 'Slowenien', 'Hungary', 'Ungarn',
    'Romania', 'Rumänien', 'Bulgaria', 'Bulgarien', 'Croatia', 'Serbia', 'Greece', 'Griechenland',
    'Estonia', 'Latvia', 'Lithuania', 'Luxembourg', 'Ireland', 'Iceland', 'Turkey', 'Türkiye',
    'Israel', 'India', 'Taiwan', 'Korea', 'Singapore', 'Malaysia', 'Thailand', 'Vietnam', 'Indonesia',
    'Philippines', 'Australia', 'Canada', 'Mexico', 'Brazil', 'Argentina', 'Chile', 'Colombia',
    'South Africa', 'Morocco', 'Egypt', 'China', 'Japan',
)


class PublicAccessBlocked(ValueError):
    pass


def _proof(url, excerpt, checked_at=None, status=200, **extra):
    value = {
        'url': str(url or ''),
        'excerpt': re.sub(r'\s+', ' ', str(excerpt or '')).strip()[:1200] or 'public source record',
        'checked_at': checked_at or now(),
        'http_status': int(status),
    }
    value.update(extra)
    return value


def _infer_country(location, text=''):
    haystack = f'{location or ""} {text or ""}'
    low = haystack.casefold()
    for name in COUNTRY_NAMES:
        if name.casefold() in low:
            return normalize_country(name)
    return ''


def _robots_allowed(session, url, cache):
    p = urlparse(url)
    if p.scheme not in {'http', 'https'} or not p.hostname:
        return False
    origin = f'{p.scheme}://{p.hostname}'
    if p.port:
        origin += f':{p.port}'
    key = origin.casefold()
    if key not in cache:
        rp = RobotFileParser()
        try:
            r = session.get(origin + '/robots.txt', timeout=(5, 10), allow_redirects=True)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
                cache[key] = rp
            elif r.status_code == 404:
                rp.parse([])
                cache[key] = rp
            else:
                cache[key] = False
        except requests.RequestException:
            cache[key] = False
    value = cache[key]
    return bool(value and value.can_fetch(USER_AGENT, url))


def _fetch_html(session, url, robots_cache, max_bytes=2_000_000):
    if not _robots_allowed(session, url, robots_cache):
        raise PublicAccessBlocked('robots_unavailable_or_disallowed')
    with session.get(url, timeout=(7, 16), stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        if 'text/html' not in response.headers.get('Content-Type', '').lower():
            raise ValueError('not_html')
        parts, size = [], 0
        for part in response.iter_content(65536):
            size += len(part)
            if size > max_bytes:
                raise ValueError('page_too_large')
            parts.append(part)
        html = b''.join(parts).decode(response.encoding or 'utf-8', errors='replace')
        final_url = response.url
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.select('script,style,noscript,svg'):
        tag.decompose()
    text = re.sub(r'\s+', ' ', soup.get_text(' ', strip=True))
    if len(text) < 80 or any(x in text.casefold() for x in ('verify you are human', 'checking your browser', 'just a moment...')):
        raise PublicAccessBlocked('page_block_or_incomplete')
    return final_url, soup, text


def _extract_company_website(soup, source_url):
    source_domain = domain(source_url)
    candidates = []
    for a in soup.find_all('a', href=True):
        href = urljoin(source_url, a.get('href'))
        d = domain(href)
        if not d or d == source_domain or d in SOCIAL_DOMAINS:
            continue
        p = urlparse(href)
        if p.scheme not in {'http', 'https'}:
            continue
        context = re.sub(r'\s+', ' ', a.parent.get_text(' ', strip=True) if a.parent else a.get_text(' ', strip=True)).casefold()
        anchor = a.get_text(' ', strip=True).casefold()
        score = (4 if 'website' in context else 0) + (2 if anchor.startswith(('http://', 'https://', 'www.')) else 0)
        if any(x in d for x in ('doubleclick.', 'googleadservices.', 'bthmanagement.')):
            score -= 10
        candidates.append((score, href))
    return max(candidates, default=(None, ''))[1]


def _robotics_profile(session, seed, robots_cache):
    url = seed['profile_url']
    parsed = urlparse(url)
    if parsed.hostname != 'www.roboticstomorrow.com' or not parsed.path.startswith('/company_directory/'):
        raise ValueError('profile_not_on_allowed_public_catalog')
    final_url, soup, _ = _fetch_html(session, url, robots_cache)
    main = soup.select_one('main') or soup.select_one('.main') or soup
    main_text = re.sub(r'\s+', ' ', main.get_text(' ', strip=True))
    return {
        'url': final_url,
        'company_name': seed['name'],
        'website': _extract_company_website(soup, final_url),
        'text': main_text[:12000],
        'checked_at': now(),
    }


def _official_site_scan(session, website, robots_cache):
    """Bounded first-party scan for identity/product text and obvious Japan GTM presence."""
    final_url, soup, root_text = _fetch_html(session, website, robots_cache)
    base_domain = domain(final_url)
    pages = [(final_url, root_text)]
    link_tokens = ('contact', 'location', 'office', 'global', 'company', 'about', 'where-we-are', 'worldwide')
    seen = {final_url}
    for a in soup.find_all('a', href=True):
        if len(pages) >= 3:
            break
        href = urljoin(final_url, a.get('href'))
        if href in seen or domain(href) != base_domain:
            continue
        signal = (a.get_text(' ', strip=True) + ' ' + urlparse(href).path).casefold()
        if not any(token in signal for token in link_tokens):
            continue
        seen.add(href)
        try:
            child_url, _, child_text = _fetch_html(session, href, robots_cache, 1_500_000)
            pages.append((child_url, child_text))
        except (requests.RequestException, ValueError, PublicAccessBlocked):
            continue

    combined = ' '.join(text for _, text in pages)[:28000]
    low = combined.casefold()
    japan_terms = ('japan', 'tokyo', 'osaka', 'yokohama', 'nagoya', '日本', '東京', '大阪', '横浜', '名古屋')
    mentions = []
    for term in japan_terms:
        start = 0
        while True:
            pos = low.find(term.casefold(), start)
            if pos < 0:
                break
            mentions.append(low[max(0, pos - 220):pos + 320])
            start = pos + len(term)
            if len(mentions) >= 20:
                break
        if len(mentions) >= 20:
            break

    direct_terms = (
        'japan office', 'japan branch', 'japan subsidiary', 'japan k.k', 'japan kk', 'japan co., ltd',
        'tokyo office', 'osaka office', 'yokohama office', 'nagoya office', 'japan sales office',
        '株式会社', '日本法人', '東京支社', '大阪支社',
    )
    distributor_terms = ('distributor', 'dealer', 'reseller', 'sales partner', 'channel partner', 'representative')
    direct = any(any(term in window for term in direct_terms) for window in mentions)
    distributor_only = bool(mentions) and not direct and any(any(term in window for term in distributor_terms) for window in mentions)
    if direct:
        outcome = 'direct_presence_found'
    elif distributor_only:
        outcome = 'distributor_only'
    elif mentions:
        outcome = 'japan_mention_needs_review'
    else:
        outcome = 'no_direct_presence_found'
    checked_urls = [url for url, _ in pages]
    bounded = _proof(final_url, f'First-party bounded scan across {len(pages)} page(s): {outcome}',
                     outcome=outcome, checked_urls=checked_urls)
    return {
        'website': final_url,
        'text': combined,
        'direct_presence': direct,
        'distributor_only': distributor_only,
        'bounded_check': bounded,
    }


def _source_family(source_url):
    if source_url == VDMA_DIRECTORY:
        return 'vdma_members'
    if source_url == DIRECTORY:
        return 'robotics_tomorrow'
    return 'public_industrial_directory'


def _build_record(seed, profile, official):
    source_url = seed['source_url']
    family = _source_family(source_url)
    website = official.get('website') or profile.get('website') or (seed['profile_url'] if family.startswith('vdma') else '')
    product_text = ' '.join(filter(None, [seed['description'], profile.get('text'), official.get('text')]))[:18000]
    country = _infer_country(seed['location'], profile.get('text', ''))
    checked_at = profile.get('checked_at') or seed['collected_at'] or now()
    source_excerpt = f"{seed['name']} | {seed['location']} | {seed['description']} | website={website}"
    source_proof = _proof(source_url, source_excerpt, checked_at=seed['collected_at'] or checked_at)
    official_proof = _proof(official.get('website') or website,
                            (official.get('text') or product_text)[:1000], checked_at=checked_at)

    low = product_text.casefold()
    sector = classify(product_text)
    negative = any(term in low for term in (
        'market research report', 'market research reports', 'conference on robotics', 'world conference on',
        'marketing agency', 'seo agency', 'publication and media', 'event organizer',
    ))
    non_vendor_only = bool(negative and family == 'robotics_tomorrow')
    ip_signal = bool(re.search(r'\b(patent(?:ed|s)?|proprietary|intellectual property|own technology|deeptech)\b', low))
    commercial_signal = bool(re.search(r'\b(customer|customers|client|clients|deployed|deployment|installed|product|products|solution|platform|system|systems)\b', low))
    manufacturing_signal = bool(sector['score'] or re.search(r'\b(manufactur|factory|production|industrial|automation|robot|warehouse|machin|inspection|material|tooling)\w*\b', low))

    japan = {
        'direct_presence': bool(official.get('direct_presence')),
        'distributor_only': bool(official.get('distributor_only')),
        'bounded_check': official.get('bounded_check'),
    }
    return {
        'company_name': seed['name'],
        'website': website,
        'country': country,
        'product_text': product_text,
        'source_family': family,
        'identity_proof': official_proof if official.get('website') else source_proof,
        'country_proof': source_proof,
        'discovery_proof': source_proof,
        'commercial_proof': official_proof if commercial_signal else {},
        'payment_capacity': {},
        'initial_offer': {'delivery': 'qualified_leads'},
        'japan': japan,
        'ip_signal': ip_signal,
        'commercial_signal': commercial_signal,
        'manufacturing_signal': manufacturing_signal,
        'non_vendor_only': non_vendor_only,
    }


def _retry_state(seed_state, prefix):
    if str(seed_state).startswith(prefix + '_'):
        try:
            return int(str(seed_state).rsplit('_', 1)[-1]) + 1
        except ValueError:
            return 1
    return 1


def run(api, store, checkpoint, budget_seconds=900):
    checkpoint.load(store)
    if api.control_command() != 'START':
        store.set('command', 'STOP'); store.set('state', 'STOPPED'); api.publish_status(store)
        return
    store.set('command', 'START'); store.set('run_id', CONFIG['run_id'])
    mirror = Mirror(store, api, CONFIG['run_id'])
    if store.completed():
        mirror.reconcile_completed()
    if len(store.completed()) >= 2000:
        store.set('state', 'TARGET_REACHED'); store.set('command', 'STOP')
        store.set('target_verified', now()); checkpoint.save(store); api.publish_status(store); return

    session = requests.Session(); session.headers['User-Agent'] = USER_AGENT
    robots_cache = {}
    start = time.monotonic()
    try:
        store.set('state', 'DISCOVERING_PUBLIC_SOURCES'); api.publish_status(store)
        for source_url in SOURCES:
            exists = store.db.execute('SELECT count(*) FROM seeds WHERE source_url=?', (source_url,)).fetchone()[0]
            if exists:
                continue
            try:
                fetch_source(session, store, source_url)
                checkpoint.save(store); api.publish_status(store)
            except Exception as exc:
                store.event('SOURCE_DISCOVERY_ERROR', {'source': source_url, 'type': type(exc).__name__})

        states = ('DISCOVERED', 'FETCH_FAILED_1', 'FETCH_FAILED_2', 'OFFICIAL_SITE_RETRY_1', 'OFFICIAL_SITE_RETRY_2')
        placeholders = ','.join('?' for _ in states)
        seeds = store.db.execute(f'''SELECT * FROM seeds WHERE state IN ({placeholders})
            ORDER BY CASE WHEN source_url=? THEN 0 ELSE 1 END,
                     CASE WHEN location LIKE '%India%' OR location LIKE '%Taiwan%' OR location LIKE '%Korea%'
                               OR location LIKE '%Poland%' OR location LIKE '%Israel%' OR location LIKE '%Austria%'
                          THEN 0 ELSE 1 END, profile_url''', (*states, VDMA_DIRECTORY)).fetchall()

        attempted = 0
        for seed in seeds:
            if attempted >= 80 or time.monotonic() - start >= budget_seconds * 0.68:
                break
            if api.control_command() != 'START':
                store.set('command', 'STOP'); break
            attempted += 1
            family = _source_family(seed['source_url'])
            try:
                if family == 'robotics_tomorrow':
                    profile = _robotics_profile(session, seed, robots_cache)
                else:
                    profile = {'url': seed['source_url'], 'company_name': seed['name'],
                               'website': seed['profile_url'], 'text': seed['description'],
                               'checked_at': seed['collected_at'] or now()}

                website = profile.get('website') or (seed['profile_url'] if family.startswith('vdma') else '')
                if not website:
                    with store.db:
                        store.db.execute("UPDATE seeds SET state='PROFILE_COLLECTED_REVIEW' WHERE profile_url=?", (seed['profile_url'],))
                    store.event('PROFILE_WITHOUT_OFFICIAL_WEBSITE', {'source': seed['source_url']})
                    continue

                try:
                    official = _official_site_scan(session, website, robots_cache)
                except (requests.RequestException, ValueError, PublicAccessBlocked) as exc:
                    retry = _retry_state(seed['state'], 'OFFICIAL_SITE_RETRY')
                    state = 'OFFICIAL_SITE_RETRY_' + str(retry) if retry <= 2 else 'OFFICIAL_SITE_REVIEW'
                    with store.db:
                        store.db.execute('UPDATE seeds SET state=? WHERE profile_url=?', (state, seed['profile_url']))
                    store.event('OFFICIAL_SITE_ERROR', {'source': seed['source_url'], 'type': type(exc).__name__})
                    continue

                record = _build_record(seed, profile, official)
                result = store.record(record)
                with store.db:
                    store.db.execute("UPDATE seeds SET description=?,state='PROFILE_COLLECTED_REVIEW' WHERE profile_url=?",
                                     (json.dumps({'source_description': seed['description'], 'record_decision': result['decision'],
                                                  'reason_count': len(result['reasons'])}, ensure_ascii=False), seed['profile_url']))
            except (requests.RequestException, ValueError, PublicAccessBlocked) as exc:
                retry = _retry_state(seed['state'], 'FETCH_FAILED')
                state = 'FETCH_FAILED_' + str(retry) if retry <= 2 else 'PROFILE_REVIEW'
                with store.db:
                    store.db.execute('UPDATE seeds SET state=? WHERE profile_url=?', (state, seed['profile_url']))
                store.event('PUBLIC_PROFILE_ERROR', {'source': seed['source_url'], 'type': type(exc).__name__})

            if attempted % 20 == 0:
                store.set('heartbeat', now()); checkpoint.save(store); api.publish_status(store)
            time.sleep(0.35)

        promoted_this_cycle = 0
        for row in store.db.execute("SELECT company_key,payload FROM records WHERE decision='PASS' ORDER BY updated_at").fetchall():
            if promoted_this_cycle >= 30 or time.monotonic() - start >= budget_seconds - 45 or api.control_command() != 'START':
                break
            if row['company_key'] in store.completed():
                continue
            existing = store.db.execute("SELECT 1 FROM mirrors WHERE company_key=? AND state='EXISTING_OR_CONFLICT'", (row['company_key'],)).fetchone()
            if existing:
                continue
            outcome = mirror.sync_one(row['company_key'], json.loads(row['payload']))
            if outcome == 'BOTH_VERIFIED':
                promoted_this_cycle += 1
                checkpoint.save(store)
            if len(store.completed()) >= 500 and not store.get('milestone_verified'):
                mirror.reconcile_completed(); store.set('milestone_verified', now()); checkpoint.save(store)
            if len(store.completed()) >= 2000:
                break

        if len(store.completed()) >= 2000:
            mirror.reconcile_completed(); store.set('target_verified', now())
            store.set('state', 'TARGET_REACHED'); store.set('command', 'STOP')
        elif store.get('command') != 'START':
            store.set('state', 'STOPPED')
        else:
            remaining = store.db.execute(f"SELECT count(*) FROM seeds WHERE state IN ({placeholders})", states).fetchone()[0]
            pass_waiting = store.db.execute("SELECT count(*) FROM records WHERE decision='PASS'").fetchone()[0] - len(store.completed())
            store.set('state', 'PROMOTING_QUALIFIED' if pass_waiting > 0 else
                      'PUBLIC_RESEARCH_CONTINUES' if remaining else 'SOURCE_EXPANSION_REQUIRED')
    except Exception as exc:
        store.set('state', 'ERROR'); store.event('RUN_ERROR', {'type': type(exc).__name__})
        raise
    finally:
        session.close(); store.set('heartbeat', now()); checkpoint.save(store); api.publish_status(store)


def main():
    if os.environ.get('AONE_FREE_PUBLIC_RUNNER') != 'true':
        raise SystemExit('Free public runner required')
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/spreadsheets'])
    api = Sheets(CONFIG, AuthorizedSession(credentials))
    with tempfile.TemporaryDirectory() as temp:
        store = Store(temp + '/state.sqlite')
        try:
            run(api, store, SheetCheckpoint(api))
        except Exception as exc:
            print('Worker stopped: ' + type(exc).__name__); raise SystemExit(1)
        finally:
            store.db.close()
    print('Worker cycle finished. Detailed progress remains in private Sales Control.')


if __name__ == '__main__':
    main()
