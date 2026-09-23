from playwright.sync_api import sync_playwright

from form_execution import (
    _choose_form,
    _field_key,
    _label_for,
    _form_score,
    _contact_link_score,
    VALIDATION_ERROR_RE,
    CAPTCHA_FAILURE_RE,
    _form_page_allowed,
    _verify_identity_dom,
    _reveal_contact_form,
)


def test_choose_form_finds_spa_container_without_form_tag():
    html = """
    <html><body>
      <section id="contact-card">
        <label>Name <input name="name" /></label>
        <label>Email <input type="email" name="email" /></label>
        <label>Message <textarea name="message"></textarea></label>
        <button type="button">Send message</button>
      </section>
    </body></html>
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        chosen = _choose_form([page])
        assert chosen is not None
        _ctx, container = chosen
        assert _form_score(container) > 0
        assert container.get_attribute("id") == "contact-card"
        browser.close()


def test_custom_dropdown_wrapper_text_does_not_become_message_field():
    html = """
    <html><body>
      <div>
        <div>Message preferences</div>
        <label id="reason-label">Contact reason</label>
        <div role="combobox" aria-haspopup="listbox" aria-labelledby="reason-label" aria-required="true">
          Please select
        </div>
      </div>
    </body></html>
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        el = page.locator("[role=combobox]")
        key = _field_key(el, _label_for(el))
        assert key != "message"
        browser.close()


def test_contact_link_scoring_prefers_real_contact_links():
    assert _contact_link_score("Contact sales", "/contact-us") > 50
    assert _contact_link_score("Careers", "/careers") == 0
    assert _contact_link_score("Privacy", "/privacy") == 0


def test_explicit_negative_submission_evidence_is_detected():
    assert VALIDATION_ERROR_RE.search("Please complete this required field.")
    assert CAPTCHA_FAILURE_RE.search("The Google reCAPTCHA failed to validate your submission.")


def test_verified_external_form_provider_is_allowed_but_arbitrary_external_is_blocked():
    website = "https://example-industrial.com"
    assert _form_page_allowed("https://share.hsforms.com/abc123", website)
    assert _form_page_allowed("https://tally.so/r/xyz987", website)
    assert not _form_page_allowed("https://evil.example/form", website)


def test_identity_validation_ignores_unfilled_secondary_email_widget():
    html = """
    <html><body><form>
      <input type="email" name="business_email" value="admin@a1-road.com" />
      <input type="email" name="newsletter_email" value="" />
      <textarea name="message">hello</textarea>
    </form></body></html>
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        form = page.locator("form")
        audit = [
            {"index": 0, "key": "email", "action": "FILLED"},
            {"index": 2, "key": "message", "action": "FILLED"},
        ]
        _verify_identity_dom(form, audit)
        browser.close()


def test_legacy_german_ids_map_without_labels():
    html = """
    <html><body><form>
      <input id="txtName" />
      <input id="txtFirma" />
      <input id="txtEmail" />
      <input id="txtTelefon" />
      <input id="txtLand" />
      <input id="txtOrt" />
      <input id="txtPLZ" />
      <input id="txtNachricht" />
    </form></body></html>
    """
    expected = ["name", "company", "email", "phone", "country", "city", "postal_code", "message"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        fields = page.locator("input")
        observed = [_field_key(fields.nth(i), _label_for(fields.nth(i))) for i in range(fields.count())]
        assert observed == expected
        browser.close()


def test_label_for_reads_local_preceding_sibling():
    html = """
    <html><body><form>
      <div class="field">
        <span class="field-label">Company</span>
        <div><input id="opaque123" /></div>
      </div>
    </form></body></html>
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        field = page.locator("input")
        assert _label_for(field) == "Company"
        assert _field_key(field, _label_for(field)) == "company"
        browser.close()


def test_contact_reason_is_classified_for_hubspot_style_name():
    html = """
    <html><body><form>
      <select name="contact_reason">
        <option>General inquiry</option>
        <option>Sales and commercial support</option>
      </select>
    </form></body></html>
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        field = page.locator("select")
        assert _field_key(field, _label_for(field)) == "reason"
        browser.close()


def test_optional_other_communications_checkbox_is_marketing():
    label = "I agree to receive other communications from Zivid. You may unsubscribe at any time."
    lower = label.lower()
    import re
    marketing = bool(re.search(
        r"newsletter|marketing|promotional?|promotions?|offers?|"
        r"product updates?|commercial communications?|"
        r"other communications?|receive.{0,80}communications?|"
        r"communications? from|email communications?|subscribe|subscription|"
        r"news and updates|メルマガ|配信|宣伝|広告",
        lower,
    ))
    assert marketing


def test_reveal_contact_form_opens_hidden_modal():
    html = """
    <html><body>
      <button id="open" type="button" onclick="document.getElementById('modal').style.display='block'">Contact us</button>
      <div id="modal" style="display:none">
        <form>
          <input name="name" />
          <input type="email" name="email" />
          <textarea name="message"></textarea>
          <button type="submit">Submit</button>
        </form>
      </div>
    </body></html>
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        before = _choose_form([page])
        assert before is None or _form_score(before[1]) == 0
        assert _reveal_contact_form(page, "https://example.com")
        chosen = _choose_form([page])
        assert chosen is not None
        assert _form_score(chosen[1]) > 0
        browser.close()


def test_security_question_marker_is_not_treated_as_business_field():
    html = """
    <html><body><form>
      <input name="security" placeholder="Security ?" />
    </form></body></html>
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        field = page.locator("input")
        assert _field_key(field, _label_for(field)) == ""
        browser.close()
