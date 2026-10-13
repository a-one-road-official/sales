"""Local DOM fixtures: no recipient site and no customer submission."""
import pytest
from playwright.sync_api import sync_playwright
from form_execution import (_verify_identity_dom, _verify_phone_dom,
    _verify_sender_contact_dom, _value_for, _field_key, _fill_verified_phone)


@pytest.mark.parametrize('html,label,expected', [
    ('<input type="tel" name="form_fields[email]">', 'Tel.', 'phone'),
    ('<input type="text" name="email">', 'Tel.', 'phone'),
    ('<input type="text" name="form_fields[name]">', 'Campany', 'company'),
    ('<input name="state">', 'State/Region*', 'state'),
    ('<input name="region">', 'Region', 'region'),
])
def test_live_field_semantics_outrank_stale_builder_identifiers(html, label, expected):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        assert _field_key(page.locator('input'), label) == expected
        browser.close()


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


def test_observed_digits_hyphen_pattern_preserves_exact_international_number():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page()
        page.set_content('<form><label>Phone<input type="tel" name="contact[Phone number]" autocomplete="tel" pattern="[0-9\\-]*"></label></form>')
        el=page.locator('input');audit={'index':0,'key':'phone','action':'FILLED'}
        el.fill('+818048705690')
        with pytest.raises(ValueError,match='FORM_PHONE_INPUT_INVALID'):_verify_phone_dom(page.locator('form'),[audit])
        assert _fill_verified_phone(el,japan_widget=False,item=audit)=='818048705690'
        assert el.get_attribute('pattern')=='[0-9\\-]*'
        _verify_phone_dom(page.locator('form'),[audit])
        assert audit['phone_country_verified']=='E164_JAPAN'
        el.fill('000-0000-0000')
        with pytest.raises(ValueError,match='FORM_PHONE_VALUE_MISMATCH'):_verify_phone_dom(page.locator('form'),[audit])
        browser.close()


def test_international_digits_require_observed_constraint_and_unchanged_number():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page()
        page.set_content('<form><input type="tel" pattern="[0-9\\-]*"></form>')
        el=page.locator('input');audit={'index':0,'key':'phone','action':'FILLED'}
        el.fill('818048705690')
        with pytest.raises(ValueError):_verify_phone_dom(page.locator('form'),[audit])
        _fill_verified_phone(el,japan_widget=False,item=audit)
        el.evaluate("el=>el.removeAttribute('pattern')")
        with pytest.raises(ValueError):_verify_phone_dom(page.locator('form'),[audit])
        browser.close()


@pytest.mark.parametrize('pattern',['[0-9]{10}', '[0-9]{11}'])
def test_domestic_pattern_never_authorizes_unverified_japan_national_number(pattern):
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page()
        page.set_content(f'<form><input type="tel" pattern="{pattern}"></form>')
        audit={'index':0,'key':'phone','action':'FILLED'};el=page.locator('input')
        assert _fill_verified_phone(el,japan_widget=False,item=audit)=='+818048705690'
        with pytest.raises(ValueError):_verify_phone_dom(page.locator('form'),[audit])
        browser.close()
