from refinery.core import parse_listing, source_url


def test_current_maktek_brand_links_are_accepted_as_material():
    url='https://www.maktekfuari.com/en/brand/abm-makine-san-ve-tic-as'
    assert source_url(url)==url
    # Public directory inspected 2026-09-18 now uses /en/brand/, not only the older /exhibitor-list/ path.
    html=f'<a href="{url}">ABM MAKİNE SAN. VE TİC. A.Ş. Türki̇ye Review in Detail Hall: 12 Booth: 1228</a>'
    rows,_=parse_listing(html,'https://www.maktekfuari.com/en/exhibitor-list')
    assert len(rows)==1
    assert rows[0]['company_name']=='ABM MAKİNE SAN. VE TİC. A.Ş.'
    assert rows[0]['source_record_url']==url


def test_live_country_suffixes_are_not_company_name_tokens():
    html="""<a href='/en/brand/1ci'>1Ci Russia Review in Detail Hall: 12A Booth: 57B</a>
    <a href='/en/brand/a-ryung'>A-RYUNG Republic Of Korea Review in Detail Hall: 9 Booth: 906</a>"""
    rows,_=parse_listing(html,'https://www.maktekfuari.com/en/exhibitor-list')
    assert [(r['company_name'],r['country_candidate']) for r in rows]==[('1Ci','Russia'),('A-RYUNG','South Korea')]
