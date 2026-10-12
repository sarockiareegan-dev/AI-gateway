"""
VERIA-43 regression tests:

- /team/daily/activity must require admin/permission on EVERY requested
  team for the unfiltered "full team view" path. The earlier code set a
  global flag if the caller was admin of any one of the requested teams.
- /agent/daily/activity must scope non-admin callers to the agents they
  may actually see, instead of returning the entire proxy's agent rows.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.agent_endpoints.auth.agent_permission_handler import (
    RestrictedAgentAccess,
    UnrestrictedAgentAccess,
)


# ---------------------------------------------------------------------------
# /team/daily/activity — per-team admin/permission requirement
# ---------------------------------------------------------------------------


def _make_team(team_id: str, admin_user_ids: list):
    """Build a Prisma-compatible team row. `admin_user_ids` are inserted as
    `members_with_roles[*].role == "admin"` because that's what
    `_is_user_team_admin` checks."""
    members_with_roles = [{"user_id": uid, "role": "admin"} for uid in admin_user_ids]
    row = MagicMock()
    row.team_id = team_id
    row.team_alias = team_id
    row.admins = admin_user_ids
    row.members_with_roles = members_with_roles
    row.model_dump = MagicMock(
        return_value={
            "team_id": team_id,
            "team_alias": team_id,
            "admins": admin_user_ids,
            "members_with_roles": members_with_roles,
            "members": [],
        }
    )
    return row


@pytest.mark.asyncio
async def test_team_activity_requires_admin_on_every_requested_team():
    """If the caller is admin of one team but only a member of another in
    the same request, the response MUST be filtered down to their own
    keys — the previous code returned a full breakdown."""
    from litellm.proxy.management_endpoints import team_endpoints

    user = UserAPIKeyAuth(
        user_id="alice",
        user_role=LitellmUserRoles.INTERNAL_USER.value,
    )

    # Mock prisma client
    prisma = MagicMock()
    prisma.db.litellm_teamtable.find_many = AsyncMock(
        return_value=[
            _make_team("team-A", admin_user_ids=["alice"]),
            _make_team("team-B", admin_user_ids=["bob"]),
        ]
    )
    user_keys = MagicMock(token="alice-key-1")
    prisma.db.litellm_verificationtoken.find_many = AsyncMock(return_value=[user_keys])

    # Mock get_user_object so the non-admin branch passes
    user_info = MagicMock()
    user_info.teams = ["team-A", "team-B"]

    captured = {}

    async def _fake_get_daily_activity(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    with (
        patch.object(team_endpoints, "prisma_client", prisma, create=True),
        patch(
            "litellm.proxy.management_endpoints.team_endpoints.get_user_object",
            new=AsyncMock(return_value=user_info),
        ),
        patch(
            "litellm.proxy.management_endpoints.team_endpoints.get_daily_activity",
            new=AsyncMock(side_effect=_fake_get_daily_activity),
        ),
        patch("litellm.proxy.proxy_server.prisma_client", prisma),
        patch("litellm.proxy.proxy_server.user_api_key_cache", MagicMock()),
        patch("litellm.proxy.proxy_server.proxy_logging_obj", MagicMock()),
    ):
        await team_endpoints.get_team_daily_activity(
            team_ids="team-A,team-B",
            start_date="2026-01-01",
            end_date="2026-01-02",
            user_api_key_dict=user,
        )

    # Caller is only admin of team-A; team-B forces fallback to user-key
    # filtering for the entire request.
    assert captured["api_key"] == ["alice-key-1"]


@pytest.mark.asyncio
async def test_team_activity_full_view_when_admin_of_all_requested_teams():
    """When the caller is admin of *every* team requested, no api_key
    filter is forced — they're allowed the unfiltered breakdown."""
    from litellm.proxy.management_endpoints import team_endpoints

    user = UserAPIKeyAuth(
        user_id="alice",
        user_role=LitellmUserRoles.INTERNAL_USER.value,
    )

    prisma = MagicMock()
    prisma.db.litellm_teamtable.find_many = AsyncMock(
        return_value=[
            _make_team("team-A", admin_user_ids=["alice"]),
            _make_team("team-B", admin_user_ids=["alice"]),
        ]
    )

    user_info = MagicMock()
    user_info.teams = ["team-A", "team-B"]

    captured = {}

    async def _fake_get_daily_activity(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    with (
        patch.object(team_endpoints, "prisma_client", prisma, create=True),
        patch(
            "litellm.proxy.management_endpoints.team_endpoints.get_user_object",
            new=AsyncMock(return_value=user_info),
        ),
        patch(
            "litellm.proxy.management_endpoints.team_endpoints.get_daily_activity",
            new=AsyncMock(side_effect=_fake_get_daily_activity),
        ),
        patch("litellm.proxy.proxy_server.prisma_client", prisma),
        patch("litellm.proxy.proxy_server.user_api_key_cache", MagicMock()),
        patch("litellm.proxy.proxy_server.proxy_logging_obj", MagicMock()),
    ):
        await team_endpoints.get_team_daily_activity(
            team_ids="team-A,team-B",
            start_date="2026-01-01",
            end_date="2026-01-02",
            user_api_key_dict=user,
        )

    assert captured["api_key"] is None


def _org_teams_prisma():
    rows = [_make_team("team-A", admin_user_ids=["bob"]), _make_team("team-B", admin_user_ids=["bob"])]
    for row, org_id in zip(rows, ("org-a", "org-b")):
        row.organization_id = org_id
        row.model_dump.return_value["organization_id"] = org_id

    async def find_many(where):
        if "organization_id" in where:
            return [r for r in rows if r.organization_id in where["organization_id"]["in"]]
        if "team_id" in where:
            return [r for r in rows if r.team_id in where["team_id"]["in"]]
        return rows

    prisma = MagicMock()
    prisma.db.litellm_teamtable.find_many = AsyncMock(side_effect=find_many)
    prisma.db.litellm_verificationtoken.find_many = AsyncMock(return_value=[MagicMock(token="alice-key-1")])
    return prisma


async def _team_activity_as(user_role, org_role, team_ids=None):
    from litellm.proxy._types import LiteLLM_OrganizationMembershipTable, LiteLLM_UserTable
    from litellm.proxy.management_endpoints import team_endpoints

    user_info = LiteLLM_UserTable(
        user_id="alice",
        teams=[],
        organization_memberships=[
            LiteLLM_OrganizationMembershipTable(
                user_id="alice",
                organization_id="org-a",
                user_role=org_role.value,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
        ],
    )
    prisma = _org_teams_prisma()
    fake_get_daily = AsyncMock(return_value=MagicMock())
    with (
        patch(
            "litellm.proxy.management_endpoints.team_endpoints.get_user_object", new=AsyncMock(return_value=user_info)
        ),
        patch("litellm.proxy.management_endpoints.team_endpoints.get_daily_activity", new=fake_get_daily),
        patch("litellm.proxy.proxy_server.prisma_client", prisma),
        patch("litellm.proxy.proxy_server.user_api_key_cache", MagicMock()),
        patch("litellm.proxy.proxy_server.proxy_logging_obj", MagicMock()),
    ):
        await team_endpoints.get_team_daily_activity(
            team_ids=team_ids,
            start_date="2026-01-01",
            end_date="2026-01-02",
            user_api_key_dict=UserAPIKeyAuth(user_id="alice", user_role=user_role.value),
        )
    return fake_get_daily.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_role", "org_role"),
    [
        (LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.ORG_ADMIN),
        (LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY, LitellmUserRoles.INTERNAL_USER),
    ],
)
async def test_team_activity_org_wide_reader_sees_full_view_of_own_org_teams_only(user_role, org_role):
    kwargs = await _team_activity_as(user_role, org_role)

    assert kwargs["entity_id"] == ["team-A"]
    assert kwargs["api_key"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_role", "org_role"),
    [
        (LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.ORG_ADMIN),
        (LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY, LitellmUserRoles.INTERNAL_USER),
    ],
)
async def test_team_activity_org_wide_reader_cannot_request_another_orgs_team(user_role, org_role):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await _team_activity_as(user_role, org_role, team_ids="team-B")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_team_activity_plain_org_member_sees_no_org_teams():
    kwargs = await _team_activity_as(LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.INTERNAL_USER)

    assert kwargs["entity_id"] == []


# ---------------------------------------------------------------------------
# /agent/daily/activity — non-admin tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_activity_admin_unscoped():
    """Proxy admin: agent_ids omitted → no scoping (existing behavior)."""
    from litellm.proxy.agent_endpoints import endpoints

    admin = UserAPIKeyAuth(user_id="root", user_role=LitellmUserRoles.PROXY_ADMIN.value)

    prisma = MagicMock()
    prisma.db.litellm_agentstable.find_many = AsyncMock(return_value=[])

    captured = {}

    async def _fake_get_daily_activity(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    with (
        patch.object(endpoints, "prisma_client", prisma, create=True),
        patch("litellm.proxy.proxy_server.prisma_client", prisma),
        patch(
            "litellm.proxy.agent_endpoints.endpoints.check_feature_access_for_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "litellm.proxy.agent_endpoints.endpoints.get_daily_activity",
            new=AsyncMock(side_effect=_fake_get_daily_activity),
        ),
    ):
        await endpoints.get_agent_daily_activity(
            agent_ids=None,
            start_date="2026-01-01",
            end_date="2026-01-02",
            user_api_key_dict=admin,
        )

    assert captured["entity_id"] is None  # no agent_id scoping for admin


@pytest.mark.asyncio
async def test_agent_activity_non_admin_no_perms_falls_back_to_owned():
    """Non-admin without explicit agent permissions: scope to agents they
    created. An empty `agent_ids` query must NOT return everyone's agents."""
    from litellm.proxy.agent_endpoints import endpoints

    user = UserAPIKeyAuth(
        user_id="alice",
        user_role=LitellmUserRoles.INTERNAL_USER.value,
    )

    owned = [MagicMock(agent_id="agent-alice-1"), MagicMock(agent_id="agent-alice-2")]
    prisma = MagicMock()
    # First call: lookup of owned agents (created_by=alice).
    # Second call: agent_metadata fetch for the resolved set.
    prisma.db.litellm_agentstable.find_many = AsyncMock(side_effect=[owned, owned])

    captured = {}

    async def _fake_get_daily_activity(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    with (
        patch.object(endpoints, "prisma_client", prisma, create=True),
        patch("litellm.proxy.proxy_server.prisma_client", prisma),
        patch(
            "litellm.proxy.agent_endpoints.endpoints.check_feature_access_for_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "litellm.proxy.agent_endpoints.auth.agent_permission_handler.AgentRequestHandler.resolve_agent_access",
            new=AsyncMock(return_value=UnrestrictedAgentAccess()),  # no explicit agent permissions
        ),
        patch(
            "litellm.proxy.agent_endpoints.endpoints.get_daily_activity",
            new=AsyncMock(side_effect=_fake_get_daily_activity),
        ),
    ):
        await endpoints.get_agent_daily_activity(
            agent_ids=None,
            start_date="2026-01-01",
            end_date="2026-01-02",
            user_api_key_dict=user,
        )

    # entity_id must be the user's owned agents, not None (which would
    # match every row).
    assert sorted(captured["entity_id"]) == ["agent-alice-1", "agent-alice-2"]


@pytest.mark.asyncio
async def test_agent_activity_non_admin_intersects_explicit_agent_ids():
    """When the caller passes `agent_ids`, the result is intersected with
    their permitted set rather than trusting the request."""
    from litellm.proxy.agent_endpoints import endpoints

    user = UserAPIKeyAuth(
        user_id="alice",
        user_role=LitellmUserRoles.INTERNAL_USER.value,
    )

    prisma = MagicMock()
    prisma.db.litellm_agentstable.find_many = AsyncMock(return_value=[])

    captured = {}

    async def _fake_get_daily_activity(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    with (
        patch.object(endpoints, "prisma_client", prisma, create=True),
        patch("litellm.proxy.proxy_server.prisma_client", prisma),
        patch(
            "litellm.proxy.agent_endpoints.endpoints.check_feature_access_for_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "litellm.proxy.agent_endpoints.auth.agent_permission_handler.AgentRequestHandler.resolve_agent_access",
            new=AsyncMock(return_value=RestrictedAgentAccess(frozenset({"agent-permitted"}))),
        ),
        patch(
            "litellm.proxy.agent_endpoints.endpoints.get_daily_activity",
            new=AsyncMock(side_effect=_fake_get_daily_activity),
        ),
    ):
        await endpoints.get_agent_daily_activity(
            agent_ids="agent-permitted,agent-someone-elses",
            start_date="2026-01-01",
            end_date="2026-01-02",
            user_api_key_dict=user,
        )

    # Only the permitted agent survives the intersection.
    assert captured["entity_id"] == ["agent-permitted"]


@pytest.mark.asyncio
async def test_agent_activity_keyless_caller_does_not_query_created_by_null():
    """Guard against the `WHERE created_by IS NULL` pitfall: a non-admin
    caller without a user_id (e.g. a service-account key with no
    explicit agent allowlist) must NOT trigger a fallback DB query that
    would expose every ownerless agent."""
    from litellm.proxy.agent_endpoints import endpoints

    user = UserAPIKeyAuth(
        api_key="sk-svc",
        # NOTE: no user_id
        user_role=LitellmUserRoles.INTERNAL_USER.value,
    )

    prisma = MagicMock()
    prisma.db.litellm_agentstable.find_many = AsyncMock(return_value=[])

    fake_get_daily = AsyncMock()

    with (
        patch.object(endpoints, "prisma_client", prisma, create=True),
        patch("litellm.proxy.proxy_server.prisma_client", prisma),
        patch(
            "litellm.proxy.agent_endpoints.endpoints.check_feature_access_for_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "litellm.proxy.agent_endpoints.auth.agent_permission_handler.AgentRequestHandler.resolve_agent_access",
            new=AsyncMock(return_value=UnrestrictedAgentAccess()),
        ),
        patch(
            "litellm.proxy.agent_endpoints.endpoints.get_daily_activity",
            new=fake_get_daily,
        ),
    ):
        result = await endpoints.get_agent_daily_activity(
            agent_ids=None,
            start_date="2026-01-01",
            end_date="2026-01-02",
            user_api_key_dict=user,
        )

    # Empty page; the owned-agents fallback must NOT have been queried.
    assert result.results == []
    prisma.db.litellm_agentstable.find_many.assert_not_called()
    fake_get_daily.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_activity_non_admin_no_access_returns_empty_page():
    """Non-admin with no permitted agents and no owned agents must get an
    empty paginated response without an unscoped DB query."""
    from litellm.proxy.agent_endpoints import endpoints

    user = UserAPIKeyAuth(
        user_id="alice",
        user_role=LitellmUserRoles.INTERNAL_USER.value,
    )

    prisma = MagicMock()
    prisma.db.litellm_agentstable.find_many = AsyncMock(return_value=[])

    fake_get_daily = AsyncMock()

    with (
        patch.object(endpoints, "prisma_client", prisma, create=True),
        patch("litellm.proxy.proxy_server.prisma_client", prisma),
        patch(
            "litellm.proxy.agent_endpoints.endpoints.check_feature_access_for_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "litellm.proxy.agent_endpoints.auth.agent_permission_handler.AgentRequestHandler.resolve_agent_access",
            new=AsyncMock(return_value=UnrestrictedAgentAccess()),
        ),
        patch(
            "litellm.proxy.agent_endpoints.endpoints.get_daily_activity",
            new=fake_get_daily,
        ),
    ):
        result = await endpoints.get_agent_daily_activity(
            agent_ids=None,
            start_date="2026-01-01",
            end_date="2026-01-02",
            user_api_key_dict=user,
        )

    assert result.results == []
    fake_get_daily.assert_not_awaited()
