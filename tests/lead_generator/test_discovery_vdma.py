from lead_generator.discovery import parse_vdma, parse_vdma_resource


def test_parse_vdma_member_block():
    html = """
    <html><body>
      <div class="member">
        <h3>Aformic Polska Sp. z o.o.</h3>
        <div>Contact</div>
        <a href="mailto:office@aformic.com">office@aformic.com</a>
        <a href="https://aformic.com/">https://aformic.com/</a>
        <div>Address</div>
        <div>Ul. Wyczólkowskiego 113</div>
        <div>44-109 Gliwice</div>
        <div>Polen</div>
      </div>
    </body></html>
    """
    rows = list(parse_vdma(html))
    assert len(rows) == 1
    assert rows[0]["name"] == "Aformic Polska Sp. z o.o."
    assert rows[0]["profile_url"] == "https://aformic.com/"
    assert "Polen" in rows[0]["location"]


def test_parse_vdma_ignores_navigation_external_link():
    html = """
    <html><body>
      <nav><a href="https://youtube.com/vdma">YouTube</a></nav>
      <div class="member">
        <h3>Fixture Automation GmbH</h3>
        <div>Kontakt</div>
        <a href="https://fixture.example.com/">https://fixture.example.com/</a>
        <div>Adresse</div>
        <div>Werkstr. 1</div>
        <div>Deutschland</div>
      </div>
    </body></html>
    """
    rows = list(parse_vdma(html))
    assert len(rows) == 1
    assert rows[0]["name"] == "Fixture Automation GmbH"


def test_parse_vdma_resource_page():
    payload = {
        "currentPage": 0,
        "totalPages": 39,
        "totalRecords": 382,
        "content": [{
            "companyName": "Aformic Polska Sp. z o.o.",
            "address": "Ul. Wyczólkowskiego 113",
            "plz": "44-109",
            "city": "Gliwice",
            "country": "Polen",
            "email": "office@aformic.com",
            "webAddr": "aformic.com",
        }]
    }
    rows = list(parse_vdma_resource(payload, "https://www.vdma.eu/example"))
    assert rows == [{
        "profile_url": "https://aformic.com",
        "name": "Aformic Polska Sp. z o.o.",
        "location": "Ul. Wyczólkowskiego 113 44-109 Gliwice Polen",
        "description": "VDMA machinery/equipment industry member | country=Polen | city=Gliwice | email=office@aformic.com",
        "source_url": "https://www.vdma.eu/example",
        "collected_at": rows[0]["collected_at"],
    }]
