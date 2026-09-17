from unittest.mock import Mock

import pytest

from form_execution import contact_policy_blocked, final_submit_once, _select_option, _value_for


@pytest.mark.parametrize("text", ["No sales inquiries", "We do not accept unsolicited marketing messages", "営業目的のご連絡はお断りします", "サポート専用"])
def test_contact_policy_restrictions_are_respected(text):
    assert contact_policy_blocked(text)


def test_submit_timeout_never_reclicks():
    control = Mock()
    control.click.side_effect = TimeoutError("accepted by server but response lost")
    assert not final_submit_once(control)
    assert control.click.call_count == 1


def test_sender_is_not_presented_as_a_retail_buyer():
    assert _value_for("industry", "", subject="", message="") == "Business consulting"
    assert "partnership" in _value_for("reason", "", subject="", message="").lower()


@pytest.mark.browser
def test_actual_browser_selects_partnership_and_submits_once():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('''<select id="reason"><option value="buy">Buy a product</option><option value="partner">Business partnership</option></select><button id="send" onclick="window.clicks=(window.clicks||0)+1">Send</button>''')
        assert _select_option(page.locator("#reason"), "reason")[0]
        assert page.locator("#reason").input_value() == "partner"
        assert final_submit_once(page.locator("#send"))
        assert page.evaluate("window.clicks") == 1
        browser.close()
