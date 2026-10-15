import pytest

from litellm.integrations.email_templates.templates import KEY_CREATED_EMAIL_TEMPLATE
from litellm.types.integrations.slack_alerting import DEFAULT_EMAIL_SUPPORT_CONTACT, default_email_logo_url


def test_default_email_logo_is_the_gateway_logo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_BASE_URL", "https://gateway.example.com/")

    assert default_email_logo_url() == "https://gateway.example.com/get_image"


def test_default_email_logo_is_empty_without_a_public_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY_BASE_URL", raising=False)

    assert default_email_logo_url() == ""


def test_key_created_email_carries_no_upstream_branding() -> None:
    html = KEY_CREATED_EMAIL_TEMPLATE.format(
        email_logo_url="",
        recipient_email="dev@example.com",
        key_budget=10,
        key_token="sk-test",
        base_url="https://gateway.example.com",
        email_support_contact=DEFAULT_EMAIL_SUPPORT_CONTACT,
    )

    assert "LiteLLM" not in html
    assert "berri.ai" not in html
    assert "please contact your Iagami administrator" in html
