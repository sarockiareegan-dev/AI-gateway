"""Invite-user rules for the Agami hierarchy: proxy admin > org admin > team admin > member.

View-only actors (VIEWER everywhere) cannot invite. Until LiteLLM RBAC is removed, invitees
still store a LiteLLM global role; this module decides who may invite where and which roles
they may assign.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from agami.auth.context import Actor, Resource
from agami.auth.permissions import Action, ResourceType
from agami.auth.roles import TenantRole
from agami.auth.service import Allowed, authorize

TeamMemberRole = Literal["admin", "user"]

PROXY_GLOBAL_ROLES: Final = frozenset({"proxy_admin", "proxy_admin_viewer", "internal_user", "internal_user_viewer"})
SCOPED_GLOBAL_ROLES: Final = frozenset({"internal_user", "internal_user_viewer"})
ORG_MEMBER_ROLES: Final = frozenset({"org_admin", "internal_user"})


@dataclass(frozen=True, slots=True)
class TeamRef:
    team_id: str
    organization_id: str | None


@dataclass(frozen=True, slots=True)
class InvitePlan:
    """What the caller asked to create. Empty org/team lists mean proxy-wide user only."""

    organization_ids: tuple[str, ...] = ()
    team_ids: tuple[str, ...] = ()
    global_role: str = "internal_user"
    team_role: TeamMemberRole = "user"
    org_role: str = "internal_user"


@dataclass(frozen=True, slots=True)
class InviteOptions:
    """Org/team targets and role choices the caller may use in Invite User."""

    organization_ids: tuple[str, ...]
    team_ids: tuple[str, ...]
    global_roles: tuple[str, ...]
    team_roles: tuple[TeamMemberRole, ...]
    org_roles: tuple[str, ...]
    can_invite: bool


class InviteDenied(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def is_view_only(actor: Actor) -> bool:
    if actor.is_super_admin:
        return False
    org_roles: Final = frozenset(role for roles in actor.organization_roles.values() for role in roles)
    team_roles: Final = frozenset(role for membership in actor.team_memberships.values() for role in membership.roles)
    held: Final = org_roles | team_roles
    return bool(held) and held <= frozenset({TenantRole.VIEWER})


def _can_manage_org(actor: Actor, organization_id: str) -> bool:
    return isinstance(
        authorize(actor, Action.MANAGE_MEMBERS, Resource(ResourceType.ORGANIZATION, organization_id)),
        Allowed,
    )


def _can_manage_team(actor: Actor, team: TeamRef) -> bool:
    if team.organization_id is None:
        return actor.is_super_admin
    return isinstance(
        authorize(
            actor,
            Action.MANAGE_MEMBERS,
            Resource(ResourceType.TEAM, team.organization_id, team_id=team.team_id),
        ),
        Allowed,
    )


def admin_org_ids(actor: Actor) -> frozenset[str]:
    return frozenset(org_id for org_id, roles in actor.organization_roles.items() if TenantRole.ORG_ADMIN in roles)


def admin_team_ids(actor: Actor) -> frozenset[str]:
    return frozenset(
        team_id for team_id, membership in actor.team_memberships.items() if TenantRole.TEAM_ADMIN in membership.roles
    )


def invite_options(
    actor: Actor,
    known_teams: Mapping[str, TeamRef],
    known_organizations: Sequence[str] | None = None,
) -> InviteOptions:
    if not actor.is_active or is_view_only(actor):
        return InviteOptions((), (), (), (), (), can_invite=False)
    if actor.is_super_admin:
        all_orgs: Final = frozenset(known_organizations or ()) | frozenset(
            t.organization_id for t in known_teams.values() if t.organization_id
        )
        return InviteOptions(
            organization_ids=tuple(sorted(all_orgs)),
            team_ids=tuple(sorted(known_teams)),
            global_roles=tuple(sorted(PROXY_GLOBAL_ROLES)),
            team_roles=("admin", "user"),
            org_roles=tuple(sorted(ORG_MEMBER_ROLES)),
            can_invite=True,
        )
    orgs: Final = admin_org_ids(actor)
    teams: Final = frozenset(
        team_id
        for team_id, team in known_teams.items()
        if team_id in admin_team_ids(actor) or (team.organization_id is not None and team.organization_id in orgs)
    )
    if not orgs and not teams:
        return InviteOptions((), (), (), (), (), can_invite=False)
    team_roles: Final[tuple[TeamMemberRole, ...]] = ("admin", "user") if orgs else ("user",)
    return InviteOptions(
        organization_ids=tuple(sorted(orgs)),
        team_ids=tuple(sorted(teams)),
        global_roles=tuple(sorted(SCOPED_GLOBAL_ROLES)),
        team_roles=team_roles,
        org_roles=tuple(sorted(ORG_MEMBER_ROLES)) if orgs else (),
        can_invite=True,
    )


def validate_invite(actor: Actor, plan: InvitePlan, known_teams: Mapping[str, TeamRef]) -> None:
    """Raise InviteDenied when the caller cannot create this user under Agami RBAC."""
    if not actor.is_active:
        raise InviteDenied("Inactive users cannot invite")
    if is_view_only(actor):
        raise InviteDenied("View-only roles cannot invite users")

    if plan.global_role not in PROXY_GLOBAL_ROLES:
        raise InviteDenied(f"Unknown global role '{plan.global_role}'")
    if plan.org_role not in ORG_MEMBER_ROLES:
        raise InviteDenied(f"Unknown organization role '{plan.org_role}'")
    if plan.team_role not in ("admin", "user"):
        raise InviteDenied(f"Unknown team role '{plan.team_role}'")

    if actor.is_super_admin:
        for team_id in plan.team_ids:
            if team_id not in known_teams:
                raise InviteDenied(f"Team '{team_id}' not found")
        return

    if plan.global_role not in SCOPED_GLOBAL_ROLES:
        raise InviteDenied("Only a proxy admin can assign proxy admin or organization viewer global roles")

    if not plan.organization_ids and not plan.team_ids:
        raise InviteDenied("Org admins and team admins must invite into an organization or team they manage")

    only_team_admin: Final = bool(admin_team_ids(actor)) and not admin_org_ids(actor) and not actor.is_super_admin
    if only_team_admin:
        if plan.organization_ids:
            raise InviteDenied("Team admins invite to their team only, not to an organization")
        if plan.team_role != "user":
            raise InviteDenied("Team admins can only invite members to their team, not other team admins")
        if not plan.team_ids or not set(plan.team_ids) <= admin_team_ids(actor):
            raise InviteDenied("Team admins can only invite users to teams they administer")
        if plan.org_role == "org_admin":
            raise InviteDenied("Team admins cannot grant organization admin")

    for org_id in plan.organization_ids:
        if not _can_manage_org(actor, org_id):
            raise InviteDenied(f"Not allowed to invite into organization '{org_id}'")

    for team_id in plan.team_ids:
        team: Final = known_teams.get(team_id)
        if team is None:
            raise InviteDenied(f"Team '{team_id}' not found")
        if not _can_manage_team(actor, team):
            raise InviteDenied(f"Not allowed to invite into team '{team_id}'")
        if plan.organization_ids and team.organization_id and team.organization_id not in plan.organization_ids:
            raise InviteDenied(f"Team '{team_id}' is not in the selected organization(s)")

