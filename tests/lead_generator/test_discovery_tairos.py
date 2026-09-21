from lead_generator.discovery import parse_tairos_list


def test_parse_tairos_exhibitor_links():
    html = """
    <html><body>
      <a href="/en/visitorExhibitorDetail.asp?comNo=100099&sno=213362">
        HIWIN TECHNOLOGIES CORP.
      </a>
      <a href="/en/visitorExhibitorDetail.asp?comNo=112440&sno=208622">
        ACE PILLAR CO., LTD.
      </a>
      <a href="/en/visitorExhibitorDetail.asp?comNo=100099&sno=213362">
        HIWIN TECHNOLOGIES CORP.
      </a>
      <a href="/en/visitorSearch.asp">Search</a>
    </body></html>
    """
    rows = list(parse_tairos_list(html))
    assert len(rows) == 2
    assert rows[0]["name"] == "HIWIN TECHNOLOGIES CORP."
    assert rows[0]["location"] == "Taiwan"
    assert "visitorExhibitorDetail.asp" in rows[0]["profile_url"]
    assert rows[1]["name"] == "ACE PILLAR CO., LTD."
