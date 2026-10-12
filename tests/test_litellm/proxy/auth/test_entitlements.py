from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from litellm.proxy.auth.entitlements import (
    BUNDLED_PUBLIC_KEY_PATH,
    LICENSE_ISSUER,
    EntitlementService,
    ExpiredLicense,
    InvalidLicense,
    LicenseFeature,
    NoLicense,
    ValidLicense,
    is_licensed,
    load_public_key,
    verify_license,
)
from tests.test_litellm.proxy.auth.license_test_helpers import install_entitlements, licensed_entitlements

NOW: Final = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
SIGNING_KEY: Final = Ed25519PrivateKey.generate()
PUBLIC_KEY: Final = SIGNING_KEY.public_key()


def _claims(**overrides: object) -> dict[str, object]:
    return {
        "iss": LICENSE_ISSUER,
        "sub": "example-customer",
        "jti": "lic-1",
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(days=30)).timestamp()),
        "features": ["auto_router"],
        **overrides,
    }


def _issue(claims: Mapping[str, object], key: Ed25519PrivateKey = SIGNING_KEY) -> str:
    return jwt.encode(dict(claims), key, algorithm="EdDSA")


def _service(token: str | None, clock_now: datetime = NOW) -> EntitlementService:
    service: Final = EntitlementService(public_key=PUBLIC_KEY, clock=lambda: clock_now)
    service.load(token)
    return service


@pytest.mark.parametrize("token", [None, ""])
def test_missing_token_is_no_license(token: str | None) -> None:
    assert verify_license(token, PUBLIC_KEY, NOW) == NoLicense()


def test_valid_license_exposes_signed_claims() -> None:
    status: Final = verify_license(_issue(_claims(max_users=10, max_teams=3)), PUBLIC_KEY, NOW)

    assert isinstance(status, ValidLicense)
    assert status.entitlements.customer == "example-customer"
    assert status.entitlements.license_id == "lic-1"
    assert status.entitlements.features == frozenset({"auto_router"})
    assert status.entitlements.max_users == 10
    assert status.entitlements.max_teams == 3


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_issue(_claims(), key=Ed25519PrivateKey.generate()), id="wrong-signer"),
        pytest.param(_issue(_claims(iss="someone-else")), id="wrong-issuer"),
        pytest.param(_issue({k: v for k, v in _claims().items() if k != "jti"}), id="missing-jti"),
        pytest.param(_issue({k: v for k, v in _claims().items() if k != "exp"}), id="missing-exp"),
        pytest.param(_issue(_claims(max_users=-1)), id="negative-limit"),
        pytest.param(jwt.encode(_claims(), "x" * 32, algorithm="HS256"), id="hmac-downgrade"),
        pytest.param("not-a-jwt", id="garbage"),
    ],
)
def test_untrusted_tokens_are_invalid(token: str) -> None:
    assert isinstance(verify_license(token, PUBLIC_KEY, NOW), InvalidLicense)


def test_token_is_invalid_without_a_public_key() -> None:
    assert isinstance(verify_license(_issue(_claims()), None, NOW), InvalidLicense)


def test_license_past_exp_is_expired() -> None:
    expired_at: Final = NOW - timedelta(seconds=1)

    assert verify_license(_issue(_claims(exp=int(expired_at.timestamp()))), PUBLIC_KEY, NOW) == ExpiredLicense(
        expired_at=expired_at.replace(microsecond=0)
    )


def test_premium_lapses_when_clock_passes_expiry_without_reload() -> None:
    clock_now: list[datetime] = [NOW]  # mutable-ok: simulates time passing for the injected clock
    service: Final = EntitlementService(public_key=PUBLIC_KEY, clock=lambda: clock_now[0])
    service.load(_issue(_claims()))
    assert service.is_premium()

    clock_now[0] = NOW + timedelta(days=31)

    assert not service.is_premium()
    assert service.license_data is None
    assert service.has_license


def test_invalid_token_counts_as_present_but_not_premium() -> None:
    service: Final = _service("not-a-jwt")

    assert service.has_license
    assert not service.is_premium()
    assert service.license_data is None


def test_reload_replaces_previous_license() -> None:
    service: Final = _service(_issue(_claims()))

    service.load(None)

    assert service.status == NoLicense()
    assert not service.has_license
    assert not service.is_premium()


def test_wildcard_feature_grants_everything() -> None:
    service: Final = _service(_issue(_claims(features=["*"])))

    assert service.grants_feature("auto_router")
    assert service.grants_feature("anything")


def test_feature_not_listed_is_not_granted() -> None:
    assert not _service(_issue(_claims(features=["sso"]))).grants_feature("auto_router")


@pytest.mark.parametrize(
    ("features", "expected"),
    [(["auto_router"], None), (["sso"], 1)],
)
def test_auto_router_capability_limit(features: list[str], expected: int | None) -> None:
    assert _service(_issue(_claims(features=features))).auto_router_capability_limit() == expected


def test_auto_router_capability_limit_without_license() -> None:
    assert _service(None).auto_router_capability_limit() == 1


def test_user_and_team_limits_apply_strictly_above_the_cap() -> None:
    service: Final = _service(_issue(_claims(max_users=5, max_teams=2)))

    assert not service.is_over_limit(5)
    assert service.is_over_limit(6)
    assert not service.is_team_count_over_limit(2)
    assert service.is_team_count_over_limit(3)


def test_limits_are_unbounded_when_license_omits_them() -> None:
    service: Final = _service(_issue(_claims()))

    assert not service.is_over_limit(10**6)
    assert not service.is_team_count_over_limit(10**6)


def test_license_data_mirrors_entitlements() -> None:
    data: Final = _service(_issue(_claims(features=["sso", "auto_router"], max_users=7))).license_data

    assert data == {
        "expiration_date": (NOW + timedelta(days=30)).date().isoformat(),
        "user_id": "example-customer",
        "allowed_features": ["auto_router", "sso"],
        "max_users": 7,
    }


def test_load_public_key_reads_ed25519_pem(tmp_path: Path) -> None:
    path: Final = tmp_path / "key.pem"
    path.write_bytes(PUBLIC_KEY.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))

    loaded: Final = load_public_key(path)

    assert loaded is not None
    assert loaded.public_bytes(Encoding.Raw, PublicFormat.Raw) == PUBLIC_KEY.public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )


def test_load_public_key_rejects_missing_file(tmp_path: Path) -> None:
    assert load_public_key(tmp_path / "missing.pem") is None


def test_bundled_public_key_is_a_usable_ed25519_key() -> None:
    assert load_public_key(BUNDLED_PUBLIC_KEY_PATH) is not None


@pytest.mark.parametrize(
    ("features", "expected"),
    [(["sso"], True), (["*"], True), (["organizations"], False), ([], False)],
)
def test_is_licensed_checks_the_named_feature(features: list[str], expected: bool) -> None:
    assert is_licensed(LicenseFeature.SSO, _service(_issue(_claims(features=features)))) is expected


def test_is_licensed_lapses_at_expiry_without_reload() -> None:
    clock_now: list[datetime] = [NOW]  # mutable-ok: simulates time passing for the injected clock
    service: Final = EntitlementService(public_key=PUBLIC_KEY, clock=lambda: clock_now[0])
    service.load(_issue(_claims(features=["*"])))
    assert is_licensed(LicenseFeature.SSO, service)

    clock_now[0] = NOW + timedelta(days=31)

    assert not is_licensed(LicenseFeature.SSO, service)


def test_is_licensed_reads_the_proxy_license_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=(LicenseFeature.AUDIT_LOGS.value,)))

    assert is_licensed(LicenseFeature.AUDIT_LOGS)
    assert not is_licensed(LicenseFeature.SSO)
