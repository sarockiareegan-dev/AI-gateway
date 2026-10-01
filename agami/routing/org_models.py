from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, cast

from agami.auth.context import Actor

ORG_MODEL_NAME_PREFIX: Final = "agami-org"


def org_model_name(organization_id: str, public_model_name: str) -> str:
    """Every deployment of one organization's public model shares this name, so the router load-balances them
    together and never with another organization's model of the same public name."""
    return f"{ORG_MODEL_NAME_PREFIX}/{organization_id}/{public_model_name}"


def _model_info_str(deployment: Mapping[str, object], key: str) -> str | None:
    model_info: Final = deployment.get("model_info")
    if not isinstance(model_info, Mapping):
        return None
    value: Final = cast("Mapping[str, object]", model_info).get(key)  # cast-ok: model_info is a JSON object
    return value if isinstance(value, str) and value else None


def deployment_organization_id(deployment: Mapping[str, object]) -> str | None:
    return _model_info_str(deployment, "organization_id")


def deployment_usable_by(
    deployment: Mapping[str, object], request_organization_id: str | None, is_super_admin: bool
) -> bool:
    return ModelVisibility.for_key(request_organization_id, is_super_admin).allows_deployment(deployment)


@dataclass(frozen=True, slots=True)
class OrgModelName:
    organization_id: str
    public_name: str


def deployment_org_model_name(deployment: Mapping[str, object]) -> OrgModelName | None:
    organization_id: Final = deployment_organization_id(deployment)
    public_name: Final = _model_info_str(deployment, "organization_public_model_name")
    if organization_id is None or public_name is None:
        return None
    if deployment.get("model_name") != org_model_name(organization_id, public_name):
        return None
    return OrgModelName(organization_id, public_name)


def org_model_names(deployments: Iterable[Mapping[str, object]]) -> Mapping[str, OrgModelName]:
    """Internal routing name -> owner and public name, for every organization deployment."""
    return MappingProxyType(
        {
            org_model_name(owner.organization_id, owner.public_name): owner
            for deployment in deployments
            if (owner := deployment_org_model_name(deployment)) is not None
        }
    )


@dataclass(frozen=True, slots=True)
class ModelVisibility:
    organization_ids: frozenset[str]
    sees_every_organization: bool

    @classmethod
    def for_key(cls, key_organization_id: str | None, is_super_admin: bool) -> "ModelVisibility":
        """What a key can call: global models and its own organization's."""
        return cls(frozenset({key_organization_id} if key_organization_id else ()), is_super_admin)

    @classmethod
    def for_actor(cls, actor: Actor, key_organization_id: str | None) -> "ModelVisibility":
        """What a person can see in admin views: every organization they or their key belong to."""
        organization_ids: Final = frozenset(
            (
                *actor.organization_roles,
                *(membership.organization_id for membership in actor.team_memberships.values()),
                *((key_organization_id,) if key_organization_id else ()),
            )
        )
        return cls(organization_ids, actor.is_super_admin)

    def allows_owner(self, organization_id: str | None) -> bool:
        return organization_id is None or self.sees_every_organization or organization_id in self.organization_ids

    def allows_deployment(self, deployment: Mapping[str, object]) -> bool:
        return self.allows_owner(deployment_organization_id(deployment))

    def visible_model_names(self, names: Sequence[str], owners: Mapping[str, OrgModelName]) -> list[str]:
        return [
            name for name in names if self.allows_owner(owner.organization_id if (owner := owners.get(name)) else None)
        ]
