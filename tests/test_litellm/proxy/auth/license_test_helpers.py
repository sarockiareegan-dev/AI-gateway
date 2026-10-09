from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Final

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from litellm.proxy.auth import entitlements as entitlements_module
from litellm.proxy.auth.entitlements import LICENSE_ALGORITHM, LICENSE_ISSUER, EntitlementService

_SIGNING_KEY: Final = Ed25519PrivateKey.generate()


def issue_test_license(
    features: tuple[str, ...] = ("*",),
    max_users: int | None = None,
    max_teams: int | None = None,
) -> str:
    now: Final = datetime.now(timezone.utc)
    limits: Final = {
        **({"max_users": max_users} if max_users is not None else {}),
        **({"max_teams": max_teams} if max_teams is not None else {}),
    }
    return jwt.encode(
        {
            "iss": LICENSE_ISSUER,
            "sub": "test-customer",
            "jti": "test-license",
            "exp": int((now + timedelta(days=1)).timestamp()),
            "features": list(features),
            **limits,
        },
        _SIGNING_KEY,
        algorithm=LICENSE_ALGORITHM,
    )


def unlicensed_entitlements() -> EntitlementService:
    return EntitlementService(public_key=_SIGNING_KEY.public_key())


def licensed_entitlements(
    features: tuple[str, ...] = ("*",),
    max_users: int | None = None,
    max_teams: int | None = None,
) -> EntitlementService:
    service: Final = unlicensed_entitlements()
    service.load(issue_test_license(features=features, max_users=max_users, max_teams=max_teams))
    return service


def jwt_licence(licensed: bool) -> Callable[[], EntitlementService]:
    service: Final = licensed_entitlements(features=("jwt_auth",)) if licensed else unlicensed_entitlements()
    return lambda: service


def install_entitlements(monkeypatch: pytest.MonkeyPatch, service: EntitlementService) -> EntitlementService:
    monkeypatch.setattr(entitlements_module, "get_entitlement_service", lambda: service)
    return service
