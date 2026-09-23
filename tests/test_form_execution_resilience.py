from playwright.sync_api import sync_playwright

from form_execution import (
    _choose_form,
    _field_key,
    _label_for,
    _form_score,
    _contact_link_score,
    VALIDATION_ERROR_RE,
    CAPTCHA_FAILURE_RE,
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
