from lead_generator.scheduled import _extract_labeled_website


def test_extract_labeled_website_ignores_later_advertiser_urls():
    text = (
        "Symbiosis School of Design Company Sector: Education / Training "
        "Website: http://sid.edu.in/ Back Search Visit Our Partners "
        "RoboBusiness https://www.robobusiness.com/"
    )
    assert _extract_labeled_website(text) == "http://sid.edu.in/"


def test_extract_labeled_website_requires_explicit_label():
    text = "Sponsor https://www.robobusiness.com/ Company Name Fixture Robotics"
    assert _extract_labeled_website(text) == ""
