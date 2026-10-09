from types import MappingProxyType
from typing import Final

import pytest

from agami.auth.context import Actor, TeamMembership
from agami.auth.roles import TenantRole
from agami.routing.org_models import (
    ModelVisibility,
    OrgModelName,
    deployment_org_model_name,
    org_model_name,
    org_model_names,
)


def _org_deployment(organization_id: str, public_name: str) -> dict[str, object]:
    return {
        "model_name": org_model_name(organization_id, public_name),
        "model_info": {"organization_id": organization_id, "organization_public_model_name": public_name},
    }


ORG_A_MODEL: Final = _org_deployment("org-a", "gpt-4o")
ORG_B_MODEL: Final = _org_deployment("org-b", "gpt-4o")
GLOBAL_MODEL: Final = {"model_name": "gpt-4o", "model_info": {}}


def test_org_model_name_is_distinct_per_organization():
    assert org_model_name("org-a", "gpt-4o") != org_model_name("org-b", "gpt-4o")


@pytest.mark.parametrize(
    "deployment",
    [
        GLOBAL_MODEL,
        {
            "model_name": "gpt-4o",
            "model_info": {"organization_id": "org-a", "organization_public_model_name": "gpt-4o"},
        },
        {"model_name": org_model_name("org-a", "gpt-4o"), "model_info": {"organization_id": "org-a"}},
        {"model_name": org_model_name("org-a", "gpt-4o"), "model_info": "not-a-mapping"},
    ],
)
def test_only_consistently_named_deployments_are_organization_models(deployment: dict[str, object]):
    assert deployment_org_model_name(deployment) is None


def test_org_model_names_maps_internal_name_to_owner():
    assert dict(org_model_names([ORG_A_MODEL, ORG_B_MODEL, GLOBAL_MODEL])) == {
        org_model_name("org-a", "gpt-4o"): OrgModelName("org-a", "gpt-4o"),
        org_model_name("org-b", "gpt-4o"): OrgModelName("org-b", "gpt-4o"),
    }


def test_key_sees_global_and_own_organization_models_only():
    visibility: Final = ModelVisibility.for_key("org-a", is_super_admin=False)

    assert visibility.allows_deployment(ORG_A_MODEL)
    assert visibility.allows_deployment(GLOBAL_MODEL)
    assert not visibility.allows_deployment(ORG_B_MODEL)


def test_key_without_organization_sees_only_global_models():
    visibility: Final = ModelVisibility.for_key(None, is_super_admin=False)

    assert visibility.allows_deployment(GLOBAL_MODEL)
    assert not visibility.allows_deployment(ORG_A_MODEL)


def test_super_admin_sees_every_organization():
    visibility: Final = ModelVisibility.for_key(None, is_super_admin=True)

    assert visibility.allows_deployment(ORG_A_MODEL)
    assert visibility.allows_deployment(ORG_B_MODEL)


def test_actor_sees_organizations_from_memberships_teams_and_key():
    actor: Final = Actor(
        user_id="u",
        is_super_admin=False,
        is_active=True,
        organization_roles=MappingProxyType({"org-a": frozenset({TenantRole.ORG_ADMIN})}),
        team_memberships=MappingProxyType({"team-1": TeamMembership("org-b", frozenset({TenantRole.VIEWER}))}),
    )
    visibility: Final = ModelVisibility.for_actor(actor, key_organization_id="org-c")

    assert visibility.organization_ids == frozenset({"org-a", "org-b", "org-c"})
    assert not visibility.allows_owner("org-d")


def test_visible_model_names_drops_other_organizations_internal_names():
    names: Final = [org_model_name("org-a", "gpt-4o"), org_model_name("org-b", "gpt-4o"), "gpt-4o"]
    owners: Final = org_model_names([ORG_A_MODEL, ORG_B_MODEL])

    assert ModelVisibility.for_key("org-a", is_super_admin=False).visible_model_names(names, owners) == [
        org_model_name("org-a", "gpt-4o"),
        "gpt-4o",
    ]
