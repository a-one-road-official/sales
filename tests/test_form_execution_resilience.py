from playwright.sync_api import sync_playwright
import pytest

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
    _verify_phone_dom,
    _select_phone_country,
    _value_for,
    _verify_sender_contact_dom,
    _provider_managed_captcha_checkbox,
)


@pytest.mark.parametrize('value,country,valid', [
    ('+81 80-4870-5690', None, True),
    ('08048705690', 'JP', True),
    ('8048705690', 'JP', True),
    ('08048705690', None, False),
    ('+818048705690', 'US', False),
    ('+818048705691', 'JP', False),
    ('', 'JP', False),
])
def test_phone_readback_checks_number_and_actual_country(value, country, valid):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        selector = '<select class="PhoneInputCountrySelect"><option value="US">United States</option><option value="JP">Japan</option></select>' if country else ''
        page.set_content('<form><div class="PhoneInput">' + selector + '<input type="tel" name="phone"></div></form>')
        if country:
            page.locator('select').select_option(country)
        page.locator('input').fill(value)
        audit = [{'index': 1 if country else 0, 'key': 'phone', 'action': 'FILLED'}]
        if valid:
            _verify_phone_dom(page.locator('form'), audit)
            assert audit[0]['final_value'] == value
        else:
            with pytest.raises(ValueError, match='FORM_PHONE_'):
                _verify_phone_dom(page.locator('form'), audit)
        browser.close()


def test_native_phone_widget_selects_and_verifies_japan():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<form><div class="PhoneInput"><select class="PhoneInputCountrySelect"><option value="US">United States</option><option value="JP">Japan</option></select><input type="tel"></div></form>')
        assert _select_phone_country(page.locator('input'), page) == (True, True)
        assert page.locator('select').input_value() == 'JP'
        browser.close()


def test_sender_fields_use_verified_sender_instead_of_target_company():
    assert _value_for('first_name', '', subject='', message='') == 'Kazuma'
    assert _value_for('last_name', '', subject='', message='') == 'Tamura'
    assert _value_for('phone', '', subject='', message='') == '+818048705690'
    assert _value_for('website', '', subject='', message='') == 'https://a-oneroad.com/'


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


def test_wix_style_topic_and_country_dropdown_markers_are_classified():
    html = """
    <html><body><form>
      <button role="button" aria-haspopup="listbox" aria-expanded="false" aria-label="Country">Select</button>
      <button role="button" aria-haspopup="listbox" aria-expanded="false" aria-label="Topic">Select</button>
    </form></body></html>
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        country = page.locator("button").nth(0)
        topic = page.locator("button").nth(1)
        page.set_content(html)
        country = page.locator("button").nth(0)
        topic = page.locator("button").nth(1)
        assert _field_key(country, _label_for(country)) == "country"
        assert _field_key(topic, _label_for(topic)) == "reason"
        browser.close()


def test_this_field_is_required_is_explicit_validation_error():
    assert VALIDATION_ERROR_RE.search("This field is required.")



"""Local DOM fixtures: no recipient site and no customer submission."""
import pytest
from playwright.sync_api import sync_playwright
from form_execution import (_verify_identity_dom, _verify_phone_dom,
    _verify_sender_contact_dom, _value_for)


@pytest.mark.parametrize('key,valid,bad', [
    ('name', 'Kazuma Tamura', 'Tamura Tamura'),
    ('first_name', 'Kazuma', 'Tamura'),
    ('last_name', 'Tamura', 'Kazuma'),
    ('phone', '+818048705690', '000-0000-0000'),
    ('address', '〒220-0072 神奈川県横浜市西区浅間町1丁目4-3 ウィザードビル402', '123 Fake Street'),
    ('postal_code', '220-0072', '000-0000'),
    ('city', 'Yokohama', 'Tokyo'),
    ('state', 'Kanagawa', 'California'),
    ('country', 'Japan', 'United States'),
    ('website', 'https://a-oneroad.com/', 'https://example.com'),
])
def test_mutated_sender_value_stops_before_submit(key, valid, bad):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<form><input required name="sender_field"><button type="submit">Send</button></form>')
        audit = [{'index': 0, 'key': key, 'action': 'FILLED'}]
        validator = _verify_identity_dom if key in {'name', 'first_name', 'last_name'} else _verify_phone_dom if key == 'phone' else _verify_sender_contact_dom
        page.locator('input').fill(valid)
        validator(page.locator('form'), audit)
        page.locator('input').fill(bad)
        with pytest.raises(ValueError, match='FORM_'):
            validator(page.locator('form'), audit)
        browser.close()


@pytest.mark.parametrize('key,bad', [('name','Tamura Tamura'), ('phone','000-0000-0000'),
                                    ('address','123 Fake Street'), ('postal_code','000-0000'),
                                    ('message','New shortened message')])
def test_field_override_cannot_replace_sender_facts_or_saved_copy(key, bad):
    expected = _value_for(key, 'specific_field', subject='Saved subject', message='Saved body')
    assert _value_for(key, 'specific_field', subject='Saved subject', message='Saved body',
                      overrides={'specific_field':bad}) == expected


def test_missing_identity_control_is_not_silently_skipped():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<form></form>')
        with pytest.raises(ValueError, match='FORM_IDENTITY_CONTROL_MISSING'):
            _verify_identity_dom(page.locator('form'), [{'index':0,'key':'name','action':'FILLED'}])
        browser.close()

def test_provider_managed_hidden_captcha_checkbox_is_not_a_business_fact():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<form><div class="Captcha3940957316">'
            '<input class="Captcha3940957316__checkbox" type="checkbox" '
            'required tabindex="-1" aria-hidden="true">'
            '</div></form>'
        )
        checkbox = page.locator('input[type=checkbox]')
        assert checkbox.is_visible()
        assert _provider_managed_captcha_checkbox(checkbox)
        assert not checkbox.is_checked()
        browser.close()


def test_visible_captcha_checkbox_is_never_reclassified_as_provider_hidden_state():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<form><label class="captcha">I am not a robot'
            '<input type="checkbox" required></label></form>'
        )
        checkbox = page.locator('input[type=checkbox]')
        assert checkbox.is_visible()
        assert not _provider_managed_captcha_checkbox(checkbox)
        browser.close()

