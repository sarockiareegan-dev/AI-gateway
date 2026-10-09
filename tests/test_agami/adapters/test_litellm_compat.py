from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Final, Literal

import pytest

from agami.adapters.litellm_compat import actor_from_litellm, load_actor
from agami.auth.context import TeamMembership
from agami.auth.roles import TenantRole
from litellm.models.organization_membership import LiteLLM_OrganizationMembershipTable
from litellm.models.team import LiteLLM_TeamTable, Member
from litellm.models.user import LiteLLM_UserTable

USER: Final = "user-1"
NOW: Final = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _user(
    user_role: str = "internal_user",
    memberships: tuple[tuple[str, str | None], ...] = (),
    metadata: dict[str, object] | None = None,
) -> LiteLLM_UserTable:
    return LiteLLM_UserTable(
        user_id=USER,
        user_role=user_role,
        metadata=metadata,
        organization_memberships=[
            LiteLLM_OrganizationMembershipTable(
                user_id=USER, organization_id=org_id, user_role=role, created_at=NOW, updated_at=NOW
            )
            for org_id, role in memberships
        ],
    )


def _team(
    team_id: str, organization_id: str | None, members: tuple[tuple[str, Literal["admin", "user"]], ...]
) -> LiteLLM_TeamTable:
    return LiteLLM_TeamTable(
        team_id=team_id,
        organization_id=organization_id,
        members_with_roles=[Member(user_id=user_id, role=role) for user_id, role in members],
    )


@pytest.mark.parametrize(
    ("user_role", "is_super_admin"),
    [("proxy_admin", True), ("proxy_admin_viewer", False), ("org_admin", False), ("internal_user", False)],
)
def test_only_proxy_admin_is_super_admin(user_role: str, is_super_admin: bool) -> None:
    assert actor_from_litellm(_user(user_role=user_role), teams=()).is_super_admin is is_super_admin


@pytest.mark.parametrize(
    ("membership_role", "expected"),
    [("org_admin", TenantRole.ORG_ADMIN), ("internal_user", TenantRole.VIEWER), (None, TenantRole.VIEWER)],
)
def test_org_membership_role_mapping(membership_role: str | None, expected: TenantRole) -> None:
    actor: Final = actor_from_litellm(_user(memberships=(("org-a", membership_role),)), teams=())

    assert actor.organization_roles == {"org-a": frozenset({expected})}


def test_team_member_roles_map_and_keep_their_organization() -> None:
    teams: Final = (
        _team("team-1", "org-a", ((USER, "admin"), ("other", "user"))),
        _team("team-2", "org-b", ((USER, "user"),)),
        _team("team-3", "org-a", (("other", "admin"),)),
    )

    actor: Final = actor_from_litellm(_user(), teams=teams)

    assert actor.team_memberships == {
        "team-1": TeamMembership(organization_id="org-a", roles=frozenset({TenantRole.TEAM_ADMIN})),
        "team-2": TeamMembership(organization_id="org-b", roles=frozenset({TenantRole.TEAM_MEMBER})),
    }


def test_teams_outside_any_organization_grant_no_tenant_role() -> None:
    actor: Final = actor_from_litellm(_user(), teams=(_team("orphan", None, ((USER, "admin"),)),))

    assert actor.team_memberships == {}


@pytest.mark.parametrize("user_role", ["internal_user_viewer", "proxy_admin_viewer"])
def test_view_only_users_are_viewers_everywhere(user_role: str) -> None:
    actor: Final = actor_from_litellm(
        _user(user_role=user_role, memberships=(("org-a", "org_admin"),)),
        teams=(_team("team-1", "org-a", ((USER, "admin"),)),),
    )

    assert actor.organization_roles == {"org-a": frozenset({TenantRole.VIEWER})}
    assert actor.team_memberships["team-1"].roles == frozenset({TenantRole.VIEWER})


@pytest.mark.parametrize(
    ("metadata", "is_active"),
    [(None, True), ({}, True), ({"scim_active": True}, True), ({"scim_active": False}, False)],
)
def test_scim_deactivation_marks_actor_inactive(metadata: dict[str, object] | None, is_active: bool) -> None:
    assert actor_from_litellm(_user(metadata=metadata), teams=()).is_active is is_active


async def _no_user(_user_id: str) -> LiteLLM_UserTable | None:
    return None


async def _no_teams(_team_ids: Sequence[str]) -> Sequence[LiteLLM_TeamTable]:
    raise AssertionError("teams must not be fetched without a user row")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "key_user_role", "is_super_admin"),
    [(None, "proxy_admin", True), ("svc", "proxy_admin", True), ("svc", "internal_user", False), (None, None, False)],
)
async def test_keys_without_a_user_row_act_only_with_their_key_role(
    user_id: str | None, key_user_role: str | None, is_super_admin: bool
) -> None:
    actor: Final = await load_actor(user_id, key_user_role, fetch_user=_no_user, fetch_teams=_no_teams)

    assert actor.is_super_admin is is_super_admin
    assert actor.organization_roles == {}
    assert actor.team_memberships == {}


@pytest.mark.asyncio
async def test_load_actor_resolves_the_users_teams() -> None:
    user: Final = _user(memberships=(("org-a", "org_admin"),)).model_copy(update={"teams": ["team-1"]})

    async def fetch_user(user_id: str) -> LiteLLM_UserTable | None:
        return user if user_id == USER else None

    async def fetch_teams(team_ids: Sequence[str]) -> Sequence[LiteLLM_TeamTable]:
        assert tuple(team_ids) == ("team-1",)
        return (_team("team-1", "org-a", ((USER, "user"),)),)

    actor: Final = await load_actor(USER, "proxy_admin", fetch_user=fetch_user, fetch_teams=fetch_teams)

    assert actor.is_super_admin is False
    assert actor.organization_roles == {"org-a": frozenset({TenantRole.ORG_ADMIN})}
    assert actor.team_memberships["team-1"].roles == frozenset({TenantRole.TEAM_MEMBER})
