from types import MappingProxyType
from typing import Final

import pytest

from agami.auth.context import Actor, TeamMembership
from agami.auth.invite import InviteDenied, InvitePlan, TeamRef, invite_options, validate_invite
from agami.auth.roles import TenantRole

ORG: Final = "org-a"
OTHER: Final = "org-b"
TEAM: Final = "team-a"
OTHER_TEAM: Final = "team-b"
TEAMS: Final = MappingProxyType(
    {
        TEAM: TeamRef(TEAM, ORG),
        OTHER_TEAM: TeamRef(OTHER_TEAM, OTHER),
    }
)


def _actor(
    *,
    is_super_admin: bool = False,
    org_roles: frozenset[TenantRole] = frozenset(),
    team_roles: frozenset[TenantRole] = frozenset(),
    team_id: str = TEAM,
    team_org: str = ORG,
) -> Actor:
    return Actor(
        user_id="caller",
        is_super_admin=is_super_admin,
        is_active=True,
        organization_roles=MappingProxyType({ORG: org_roles} if org_roles else {}),
        team_memberships=MappingProxyType(
            {team_id: TeamMembership(organization_id=team_org, roles=team_roles)} if team_roles else {}
        ),
    )


def test_proxy_admin_can_invite_anywhere() -> None:
    validate_invite(
        _actor(is_super_admin=True),
        InvitePlan(organization_ids=(OTHER,), team_ids=(OTHER_TEAM,), global_role="proxy_admin", team_role="admin"),
        TEAMS,
    )


def test_org_admin_invites_within_org_only() -> None:
    actor: Final = _actor(org_roles=frozenset({TenantRole.ORG_ADMIN}))
    validate_invite(actor, InvitePlan(organization_ids=(ORG,), team_ids=(TEAM,), team_role="admin"), TEAMS)
    with pytest.raises(InviteDenied, match="organization"):
        validate_invite(actor, InvitePlan(organization_ids=(OTHER,)), TEAMS)
    with pytest.raises(InviteDenied, match="team"):
        validate_invite(actor, InvitePlan(team_ids=(OTHER_TEAM,)), TEAMS)


def test_team_admin_invites_member_to_own_team_only() -> None:
    actor: Final = _actor(team_roles=frozenset({TenantRole.TEAM_ADMIN}))
    validate_invite(actor, InvitePlan(team_ids=(TEAM,), team_role="user"), TEAMS)
    with pytest.raises(InviteDenied, match="team admins"):
        validate_invite(actor, InvitePlan(team_ids=(TEAM,), team_role="admin"), TEAMS)
    with pytest.raises(InviteDenied, match="organization"):
        validate_invite(actor, InvitePlan(organization_ids=(ORG,), team_ids=(TEAM,)), TEAMS)
    with pytest.raises(InviteDenied, match="Team admins can only invite users to teams they administer"):
        validate_invite(actor, InvitePlan(team_ids=(OTHER_TEAM,), team_role="user"), TEAMS)



def test_view_only_and_member_cannot_invite() -> None:
    with pytest.raises(InviteDenied, match="View-only"):
        validate_invite(
            _actor(org_roles=frozenset({TenantRole.VIEWER}), team_roles=frozenset({TenantRole.VIEWER})),
            InvitePlan(team_ids=(TEAM,)),
            TEAMS,
        )
    with pytest.raises(InviteDenied, match="Not allowed to invite into team"):
        validate_invite(_actor(team_roles=frozenset({TenantRole.TEAM_MEMBER})), InvitePlan(team_ids=(TEAM,)), TEAMS)


def test_scoped_admins_cannot_assign_proxy_global_roles() -> None:
    with pytest.raises(InviteDenied, match="proxy admin"):
        validate_invite(
            _actor(org_roles=frozenset({TenantRole.ORG_ADMIN})),
            InvitePlan(organization_ids=(ORG,), global_role="proxy_admin"),
            TEAMS,
        )


def test_invite_options_match_hierarchy() -> None:
    proxy: Final = invite_options(_actor(is_super_admin=True), TEAMS)
    assert proxy.can_invite and "proxy_admin" in proxy.global_roles and OTHER_TEAM in proxy.team_ids

    org: Final = invite_options(_actor(org_roles=frozenset({TenantRole.ORG_ADMIN})), TEAMS)
    assert org.organization_ids == (ORG,) and TEAM in org.team_ids and OTHER_TEAM not in org.team_ids
    assert org.team_roles == ("admin", "user") and "proxy_admin" not in org.global_roles
    assert "org_admin" in org.org_roles

    team: Final = invite_options(_actor(team_roles=frozenset({TenantRole.TEAM_ADMIN})), TEAMS)
    assert team.team_ids == (TEAM,) and team.team_roles == ("user",) and team.organization_ids == ()
    assert team.org_roles == ()


def test_invite_options_inactive_and_view_only() -> None:
    from dataclasses import replace

    inactive: Final = replace(_actor(is_super_admin=True), is_active=False)
    assert not invite_options(inactive, TEAMS).can_invite

    viewer: Final = _actor(org_roles=frozenset({TenantRole.VIEWER}))
    assert not invite_options(viewer, TEAMS).can_invite


def test_invite_options_known_organizations() -> None:
    proxy: Final = invite_options(
        _actor(is_super_admin=True), TEAMS, known_organizations=("org-extra-1", "org-extra-2")
    )
    assert "org-extra-1" in proxy.organization_ids and "org-extra-2" in proxy.organization_ids


def test_org_admin_can_assign_team_admin_and_org_admin() -> None:
    actor: Final = _actor(org_roles=frozenset({TenantRole.ORG_ADMIN}))
    validate_invite(
        actor,
        InvitePlan(organization_ids=(ORG,), team_ids=(TEAM,), team_role="admin", org_role="org_admin"),
        TEAMS,
    )


def test_team_admin_restrictions() -> None:
    actor: Final = _actor(team_roles=frozenset({TenantRole.TEAM_ADMIN}))
    with pytest.raises(InviteDenied, match="Team admins cannot grant organization admin"):
        validate_invite(actor, InvitePlan(team_ids=(TEAM,), org_role="org_admin"), TEAMS)
    with pytest.raises(InviteDenied, match="Team admins invite to their team only"):
        validate_invite(actor, InvitePlan(organization_ids=(ORG,), team_ids=(TEAM,)), TEAMS)
    with pytest.raises(InviteDenied, match="Team admins can only invite members"):
        validate_invite(actor, InvitePlan(team_ids=(TEAM,), team_role="admin"), TEAMS)


def test_inactive_actor_cannot_invite() -> None:
    from dataclasses import replace

    inactive: Final = replace(_actor(is_super_admin=True), is_active=False)
    with pytest.raises(InviteDenied, match="Inactive users cannot invite"):
        validate_invite(inactive, InvitePlan(), TEAMS)

