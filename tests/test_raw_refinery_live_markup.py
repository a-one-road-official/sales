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
