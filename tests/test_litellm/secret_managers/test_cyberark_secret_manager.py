import pytest

import litellm
from litellm.proxy.auth.entitlements import LicenseFeature
from litellm.secret_managers.cyberark_secret_manager import CyberArkSecretManager
from tests.test_litellm.proxy.auth.license_test_helpers import (
    install_entitlements,
    licensed_entitlements,
    unlicensed_entitlements,
)


@pytest.fixture
def cyberark_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm, "secret_manager_client", None)
    monkeypatch.setattr(litellm, "_key_management_system", None)
    monkeypatch.setenv("CYBERARK_API_KEY", "conjur-api-key")
    monkeypatch.setenv("CYBERARK_API_BASE", "http://conjur.test:8080")


@pytest.mark.usefixtures("cyberark_env")
@pytest.mark.parametrize("features", [(), ("sso",)])
def test_cyberark_refuses_to_start_or_register_without_the_secret_managers_feature(
    monkeypatch: pytest.MonkeyPatch, features: tuple[str, ...]
) -> None:
    install_entitlements(
        monkeypatch, licensed_entitlements(features=features) if features else unlicensed_entitlements()
    )

    with pytest.raises(ValueError, match="premium"):
        CyberArkSecretManager()

    assert litellm.secret_manager_client is None


@pytest.mark.usefixtures("cyberark_env")
def test_cyberark_registers_with_the_secret_managers_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=(LicenseFeature.SECRET_MANAGERS.value,)))

    manager = CyberArkSecretManager()

    assert litellm.secret_manager_client is manager
