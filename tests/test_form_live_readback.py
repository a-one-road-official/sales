import pytest
from playwright.sync_api import sync_playwright
from form_execution import _verify_identity_dom, _settle_identity_dom, _verify_phone_dom, _capture_browser_failure


@pytest.fixture
def form_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def audit(key, ident):
    return {'index': 0, 'key': key, 'action': 'FILLED', 'label': key.replace('_', ' '),
            'html_semantics': {'id': ident}, 'final_value': 'old audit value'}


def test_actual_failed_page_is_captured_before_driver_shutdown(tmp_path):
    page = None
    captures = []

    def capture(stage):
        destination = tmp_path / (stage + '.png')
        page.screenshot(path=str(destination))
        captures.append((stage, page.locator('#actual-value').input_value(), destination))

    with pytest.raises(ValueError, match='FORM_IDENTITY_VALUE_MISMATCH:first_name'):
        with _capture_browser_failure(sync_playwright(), capture) as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('<form><input id="actual-value" name="first_name" value="Tamura"></form>')
            _verify_identity_dom(page.locator('form'), [audit('first_name', 'actual-value')])

    assert captures[0][:2] == ('exception', 'Tamura')
    assert captures[0][2].read_bytes().startswith(b'\x89PNG\r\n\x1a\n')


def test_live_sender_anchor_survives_inserted_same_key_control(form_page):
    form_page.set_content('<form><input id="extra" name="first_name" value="">'
                          '<input id="filled" name="first_name" value="Kazuma"></form>')
    item = audit('first_name', 'filled')
    _verify_identity_dom(form_page.locator('form'), [item])
    assert item['final_value'] == 'Kazuma'
    assert item['identity_readback_verified'] is True
    assert form_page.locator('#extra').input_value() == ''


def test_real_mutation_is_recorded_and_remains_rejected(form_page):
    form_page.set_content('<form><input id="filled" name="first_name" value="Tamura"></form>')
    item = audit('first_name', 'filled')
    with pytest.raises(ValueError, match='FORM_IDENTITY_VALUE_MISMATCH:first_name'):
        _settle_identity_dom(form_page.locator('form'), [item])
    assert item['final_value'] == 'Tamura'
    assert item['identity_readback_verified'] is False
    assert form_page.locator('#filled').input_value() == 'Tamura'


def test_empty_known_sender_refill_uses_normal_blur_and_canonical_fact(form_page):
    form_page.set_content('<form><input id="filled" name="first_name" value="">'
                          '<input id="untouched" name="first_name" value=""></form>')
    item = audit('first_name', 'filled')
    _settle_identity_dom(form_page.locator('form'), [item])
    assert item['empty_control_refill_count'] == 1
    assert item['final_value'] == 'Kazuma'
    assert form_page.locator('#untouched').input_value() == ''


def test_duplicate_recorded_id_cannot_choose_an_arbitrary_sender(form_page):
    form_page.set_content('<form><input id="filled" name="first_name" value="Kazuma">'
                          '<input id="filled" name="first_name" value="Kazuma"></form>')
    with pytest.raises(ValueError, match='FORM_IDENTITY_CONTROL_MISSING'):
        _verify_identity_dom(form_page.locator('form'), [audit('first_name', 'filled')])


def test_phone_anchor_reads_same_filled_control_and_keeps_fake_number_guard(form_page):
    form_page.set_content('<form><input name="phone" value="000-0000-0000">'
                          '<input id="filled" type="tel" name="phone" value="+818048705690"></form>')
    item = audit('phone', 'filled')
    _verify_phone_dom(form_page.locator('form'), [item])
    form_page.locator('#filled').fill('000-0000-0000')
    with pytest.raises(ValueError, match='FORM_PHONE_VALUE_MISMATCH'):
        _verify_phone_dom(form_page.locator('form'), [item])
