"""Agami license verification and the entitlements a valid license grants.

A license is an EdDSA (Ed25519) signed JWT issued by ``scripts/agami_license.py`` and
verified offline against the public key bundled next to this module.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeAlias

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from litellm._logging import verbose_proxy_logger

if TYPE_CHECKING:
    from litellm.proxy._types import EnterpriseLicenseData

LICENSE_ENV_VAR: Final = "AGAMI_LICENSE"
LICENSE_CONFIG_KEY: Final = "agami_license"
LICENSE_ISSUER: Final = "agami"
LICENSE_ALGORITHM: Final = "EdDSA"
LICENSE_ALL_FEATURES: Final = "*"
AUTO_ROUTER_LICENSE_FEATURE: Final = "auto_router"
AUTO_ROUTER_LICENSE_REMEDY: Final = "An Agami license with the 'auto_router' feature lifts the limit."
BUNDLED_PUBLIC_KEY_PATH: Final = Path(__file__).with_name("agami_license_public_key.pem")

Clock: TypeAlias = Callable[[], datetime]


class LicenseFeature(str, Enum):
    SSO = "sso"
    ORGANIZATIONS = "organizations"
    SECRET_MANAGERS = "secret_managers"
    LOGGING_INTEGRATIONS = "logging_integrations"
    GUARDRAILS = "guardrails"
    SPEND_REPORTS = "spend_reports"
    BUDGETS = "budgets"
    ACCESS_CONTROL = "access_control"
    AUDIT_LOGS = "audit_logs"
    EMAIL_BRANDING = "email_branding"
    FINE_TUNING = "fine_tuning"
    JWT_AUTH = "jwt_auth"


class _LicenseClaims(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    sub: str = Field(min_length=1)
    jti: str = Field(min_length=1)
    exp: int
    features: tuple[str, ...] = ()
    max_users: int | None = Field(default=None, ge=0)
    max_teams: int | None = Field(default=None, ge=0)


@dataclass(frozen=True, slots=True)
class Entitlements:
    license_id: str
    customer: str
    expires_at: datetime
    features: frozenset[str]
    max_users: int | None
    max_teams: int | None

    def grants(self, feature: str) -> bool:
        return feature in self.features or LICENSE_ALL_FEATURES in self.features

    def as_license_data(self) -> EnterpriseLicenseData:
        claims: Final[EnterpriseLicenseData] = {
            "expiration_date": self.expires_at.date().isoformat(),
            "user_id": self.customer,
            "allowed_features": sorted(self.features),
        }
        user_limit: Final[EnterpriseLicenseData] = {"max_users": self.max_users} if self.max_users is not None else {}
        team_limit: Final[EnterpriseLicenseData] = {"max_teams": self.max_teams} if self.max_teams is not None else {}
        return {**claims, **user_limit, **team_limit}


@dataclass(frozen=True, slots=True)
class NoLicense:
    pass


@dataclass(frozen=True, slots=True)
class InvalidLicense:
    reason: str


@dataclass(frozen=True, slots=True)
class ExpiredLicense:
    expired_at: datetime


@dataclass(frozen=True, slots=True)
class ValidLicense:
    entitlements: Entitlements


LicenseStatus: TypeAlias = NoLicense | InvalidLicense | ExpiredLicense | ValidLicense


def verify_license(token: str | None, public_key: Ed25519PublicKey | None, now: datetime) -> LicenseStatus:
    if not token:
        return NoLicense()
    if public_key is None:
        return InvalidLicense("no Agami license public key is bundled with this build")
    try:
        raw_claims: Final = jwt.decode(
            token,
            key=public_key,
            algorithms=[LICENSE_ALGORITHM],
            issuer=LICENSE_ISSUER,
            options={
                "require": ["exp", "iss", "sub", "jti"],
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
        claims: Final = _LicenseClaims.model_validate(raw_claims)
    except (jwt.PyJWTError, ValidationError) as e:
        return InvalidLicense(f"{type(e).__name__}: {e}")
    expires_at: Final = datetime.fromtimestamp(claims.exp, tz=timezone.utc)
    if expires_at <= now:
        return ExpiredLicense(expired_at=expires_at)
    return ValidLicense(
        Entitlements(
            license_id=claims.jti,
            customer=claims.sub,
            expires_at=expires_at,
            features=frozenset(claims.features),
            max_users=claims.max_users,
            max_teams=claims.max_teams,
        )
    )


def load_public_key(path: Path) -> Ed25519PublicKey | None:
    try:
        key: Final = load_pem_public_key(path.read_bytes())
    except (OSError, ValueError) as e:
        verbose_proxy_logger.error("Could not load the Agami license public key from %s: %s", path, e)
        return None
    if isinstance(key, Ed25519PublicKey):
        return key
    verbose_proxy_logger.error("The Agami license public key at %s is not an Ed25519 key", path)
    return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EntitlementService:
    def __init__(self, public_key: Ed25519PublicKey | None, clock: Clock = _utc_now) -> None:
        self._public_key: Final = public_key
        self._clock: Final = clock
        self._token: str | None = None
        self._status: LicenseStatus = NoLicense()

    @classmethod
    def from_environment(cls) -> EntitlementService:
        service: Final = cls(public_key=load_public_key(BUNDLED_PUBLIC_KEY_PATH))
        service.load(os.getenv(LICENSE_ENV_VAR))
        return service

    def load(self, token: str | None) -> LicenseStatus:
        self._token = token or None
        self._status = verify_license(self._token, self._public_key, self._clock())
        match self._status:
            case InvalidLicense(reason=reason):
                verbose_proxy_logger.warning("Ignoring %s: %s", LICENSE_ENV_VAR, reason)
            case ExpiredLicense(expired_at=expired_at):
                verbose_proxy_logger.warning("Ignoring %s: it expired at %s", LICENSE_ENV_VAR, expired_at.isoformat())
            case NoLicense() | ValidLicense():
                pass
        return self._status

    @property
    def status(self) -> LicenseStatus:
        return self._status

    @property
    def has_license(self) -> bool:
        return self._token is not None

    @property
    def entitlements(self) -> Entitlements | None:
        match self._status:
            case ValidLicense(entitlements=entitlements) if entitlements.expires_at > self._clock():
                return entitlements
            case _:
                return None

    @property
    def license_data(self) -> EnterpriseLicenseData | None:
        entitlements: Final = self.entitlements
        return entitlements.as_license_data() if entitlements is not None else None

    def is_premium(self) -> bool:
        return self.entitlements is not None

    def grants_feature(self, feature: str) -> bool:
        entitlements: Final = self.entitlements
        return entitlements is not None and entitlements.grants(feature)

    def is_over_limit(self, total_users: int) -> bool:
        entitlements: Final = self.entitlements
        return entitlements is not None and entitlements.max_users is not None and total_users > entitlements.max_users

    def is_team_count_over_limit(self, team_count: int) -> bool:
        entitlements: Final = self.entitlements
        return entitlements is not None and entitlements.max_teams is not None and team_count > entitlements.max_teams

    def auto_router_capability_limit(self) -> int | None:
        """Unlimited auto-routers per gated capability only when the license grants ``auto_router``."""
        return None if self.grants_feature(AUTO_ROUTER_LICENSE_FEATURE) else 1


@functools.cache
def get_entitlement_service() -> EntitlementService:
    return EntitlementService.from_environment()


def is_licensed(feature: LicenseFeature, entitlements: EntitlementService | None = None) -> bool:
    return (entitlements or get_entitlement_service()).grants_feature(feature.value)
