import pytest

import litellm
from litellm.proxy.auth.entitlements import LicenseFeature
from litellm.secret_managers.google_secret_manager import GoogleSecretManager
from tests.test_litellm.proxy.auth.license_test_helpers import (
    install_entitlements,
    licensed_entitlements,
    unlicensed_entitlements,
)


@pytest.fixture
def google_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm, "secret_manager_client", None)
    monkeypatch.setattr(litellm, "_key_management_system", None)
    monkeypatch.setenv("GOOGLE_SECRET_MANAGER_PROJECT_ID", "agami-test")


@pytest.mark.usefixtures("google_env")
@pytest.mark.parametrize("features", [(), ("sso",)])
def test_google_secret_manager_requires_the_secret_managers_feature(
    monkeypatch: pytest.MonkeyPatch, features: tuple[str, ...]
) -> None:
    install_entitlements(
        monkeypatch, licensed_entitlements(features=features) if features else unlicensed_entitlements()
    )

    with pytest.raises(ValueError, match="Enterprise License"):
        GoogleSecretManager()

    assert litellm.secret_manager_client is None


@pytest.mark.usefixtures("google_env")
def test_google_secret_manager_registers_with_the_secret_managers_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=(LicenseFeature.SECRET_MANAGERS.value,)))

    manager = GoogleSecretManager()

    assert litellm.secret_manager_client is manager
