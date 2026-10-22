from unittest.mock import AsyncMock, MagicMock

import pytest

from litellm.caching.caching import DualCache
from litellm.proxy._types import (
    LiteLLM_OrganizationMembershipTable,
    LiteLLM_OrganizationTable,
    LiteLLM_TeamTable,
    LiteLLM_UserTable,
    LitellmUserRoles,
    Member,
    NewUserRequest,
    ProxyException,
    UserAPIKeyAuth,
)
from litellm.proxy.management_endpoints.internal_user_endpoints import (
    get_invite_options,
    new_user,
)
from litellm.proxy.management_helpers.bulk_user_creation import bulk_create_users
from litellm.types.proxy.management_endpoints.internal_user_endpoints import BulkNewUserItem


class _MockLicense:
    def is_over_limit(self, total_users: int) -> bool:
        return False


@pytest.fixture
def rbac_setup(mocker):
    """
    Sets up a mocked environment with:
    - org-a (with team-a1 and team-a2)
    - org-b (with team-b1)
    - proxy_admin
    - proxy_viewer
    - org_admin_a (admin of org-a)
    - team_admin_a1 (admin of team-a1 in org-a)
    - member_user (plain member in team-a1)
    - viewer_user (internal_user_viewer)
    """
    team_a1 = LiteLLM_TeamTable(
        team_id="team-a1",
        organization_id="org-a",
        members_with_roles=[
            Member(user_id="team-admin-a1", role="admin"),
            Member(user_id="member-user", role="user"),
            Member(user_id="viewer-user", role="user"),
        ],
    )
    team_a2 = LiteLLM_TeamTable(
        team_id="team-a2",
        organization_id="org-a",
        members_with_roles=[],
    )
    team_b1 = LiteLLM_TeamTable(
        team_id="team-b1",
        organization_id="org-b",
        members_with_roles=[],
    )

    all_teams = {
        "team-a1": team_a1,
        "team-a2": team_a2,
        "team-b1": team_b1,
    }

    org_a = LiteLLM_OrganizationTable(
        organization_id="org-a",
        organization_name="Org A",
        budget_id="b-a",
        created_by="proxy-admin",
        updated_by="proxy-admin",
    )
    org_b = LiteLLM_OrganizationTable(
        organization_id="org-b",
        organization_name="Org B",
        budget_id="b-b",
        created_by="proxy-admin",
        updated_by="proxy-admin",
    )
    all_orgs = {
        "org-a": org_a,
        "org-b": org_b,
    }

    user_proxy_admin = LiteLLM_UserTable(
        user_id="proxy-admin",
        user_role=LitellmUserRoles.PROXY_ADMIN.value,
    )
    user_proxy_viewer = LiteLLM_UserTable(
        user_id="proxy-viewer",
        user_role=LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY.value,
    )
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    user_org_admin_a = LiteLLM_UserTable(
        user_id="org-admin-a",
        user_role=LitellmUserRoles.INTERNAL_USER.value,
        organization_memberships=[
            LiteLLM_OrganizationMembershipTable(
                organization_id="org-a",
                user_role=LitellmUserRoles.ORG_ADMIN.value,
                user_id="org-admin-a",
                created_at=now,
                updated_at=now,
            )
        ],
    )
    user_team_admin_a1 = LiteLLM_UserTable(
        user_id="team-admin-a1",
        user_role=LitellmUserRoles.INTERNAL_USER.value,
        teams=["team-a1"],
    )
    user_member = LiteLLM_UserTable(
        user_id="member-user",
        user_role=LitellmUserRoles.INTERNAL_USER.value,
        teams=["team-a1"],
    )
    user_viewer = LiteLLM_UserTable(
        user_id="viewer-user",
        user_role=LitellmUserRoles.INTERNAL_USER_VIEW_ONLY.value,
        teams=["team-a1"],
    )

    all_users = {
        "proxy-admin": user_proxy_admin,
        "proxy-viewer": user_proxy_viewer,
        "org-admin-a": user_org_admin_a,
        "team-admin-a1": user_team_admin_a1,
        "member-user": user_member,
        "viewer-user": user_viewer,
    }

    mock_prisma = MagicMock()

    async def mock_user_find_unique(where, include=None):
        uid = where.get("user_id")
        return all_users.get(uid)

    from contextlib import asynccontextmanager

    mock_prisma.db.litellm_usertable.find_unique = AsyncMock(side_effect=mock_user_find_unique)
    mock_prisma.db.litellm_usertable.count = AsyncMock(return_value=len(all_users))
    mock_prisma.db.litellm_usertable.find_many = AsyncMock(return_value=list(all_users.values()))
    mock_prisma.db.litellm_usertable.create_many = AsyncMock(return_value=1)
    mock_prisma.db.litellm_usertable.create = AsyncMock(return_value=MagicMock())
    mock_prisma.db.litellm_usertable.update = AsyncMock(return_value=MagicMock())

    async def mock_team_find_many(where=None):
        if where and "team_id" in where and "in" in where["team_id"]:
            wanted = set(where["team_id"]["in"])
            return [t for tid, t in all_teams.items() if tid in wanted]
        return list(all_teams.values())

    mock_prisma.db.litellm_teamtable.find_many = AsyncMock(side_effect=mock_team_find_many)
    mock_prisma.db.litellm_teamtable.update = AsyncMock(return_value=MagicMock())
    mock_prisma.db.litellm_teammembership.create_many = AsyncMock(return_value=1)

    @asynccontextmanager
    async def mock_tx():
        tx_mock = MagicMock()
        tx_mock.litellm_teamtable = mock_prisma.db.litellm_teamtable
        tx_mock.litellm_teammembership = mock_prisma.db.litellm_teammembership
        tx_mock.query_raw = AsyncMock(return_value=[{"members_with_roles": []}])
        yield tx_mock

    mock_prisma.tx = mock_tx

    async def mock_org_find_many(where=None):
        return list(all_orgs.values())

    mock_prisma.db.litellm_organizationtable.find_many = AsyncMock(side_effect=mock_org_find_many)

    mocker.patch("litellm.proxy.proxy_server.prisma_client", mock_prisma)
    mocker.patch("litellm.proxy.proxy_server._license_check", _MockLicense())
    mocker.patch(
        "litellm.proxy.management_endpoints.internal_user_endpoints._check_duplicate_user_email",
        AsyncMock(return_value=None),
    )
    mocker.patch(
        "litellm.proxy.management_endpoints.internal_user_endpoints._check_duplicate_user_id",
        AsyncMock(return_value=None),
    )
    mocker.patch(
        "litellm.proxy.management_endpoints.internal_user_endpoints.generate_key_helper_fn",
        AsyncMock(return_value={"user_id": "created-user", "token": "sk-1234"}),
    )
    mocker.patch(
        "litellm.proxy.management_endpoints.internal_user_endpoints._add_user_to_team",
        AsyncMock(return_value=None),
    )
    mocker.patch(
        "litellm.proxy.management_endpoints.internal_user_endpoints._add_user_to_organizations",
        AsyncMock(return_value=None),
    )
    mocker.patch(
        "litellm.proxy.management_endpoints.internal_user_endpoints.UserManagementEventHooks.async_user_created_hook",
        AsyncMock(return_value=None),
    )

    return {
        "prisma": mock_prisma,
        "all_teams": all_teams,
        "all_orgs": all_orgs,
        "all_users": all_users,
    }


# =====================================================================
# Tests for GET /user/invite_options
# =====================================================================

@pytest.mark.asyncio
async def test_invite_options_proxy_admin(rbac_setup):
    auth = UserAPIKeyAuth(user_id="proxy-admin", user_role=LitellmUserRoles.PROXY_ADMIN)
    opts = await get_invite_options(user_api_key_dict=auth)

    assert opts.can_invite is True
    assert set(opts.organization_ids) == {"org-a", "org-b"}
    assert set(opts.team_ids) == {"team-a1", "team-a2", "team-b1"}
    assert set(opts.global_roles) == {
        "proxy_admin",
        "proxy_admin_viewer",
        "internal_user",
        "internal_user_viewer",
    }
    assert set(opts.team_roles) == {"admin", "user"}
    assert set(opts.org_roles) == {"org_admin", "internal_user"}


@pytest.mark.asyncio
async def test_invite_options_org_admin(rbac_setup):
    auth = UserAPIKeyAuth(user_id="org-admin-a", user_role=LitellmUserRoles.INTERNAL_USER)
    opts = await get_invite_options(user_api_key_dict=auth)

    assert opts.can_invite is True
    assert opts.organization_ids == ["org-a"]
    # teams within org-a only
    assert set(opts.team_ids) == {"team-a1", "team-a2"}
    assert set(opts.global_roles) == {"internal_user", "internal_user_viewer"}
    assert set(opts.team_roles) == {"admin", "user"}
    assert set(opts.org_roles) == {"org_admin", "internal_user"}


@pytest.mark.asyncio
async def test_invite_options_team_admin(rbac_setup):
    auth = UserAPIKeyAuth(user_id="team-admin-a1", user_role=LitellmUserRoles.INTERNAL_USER)
    opts = await get_invite_options(user_api_key_dict=auth)

    assert opts.can_invite is True
    assert opts.organization_ids == []
    # team-a1 only
    assert opts.team_ids == ["team-a1"]
    assert set(opts.global_roles) == {"internal_user", "internal_user_viewer"}
    # team admin can only invite members, not other team admins
    assert opts.team_roles == ["user"]
    # team admin cannot assign organization roles
    assert opts.org_roles == []


@pytest.mark.asyncio
async def test_invite_options_view_only_users(rbac_setup):
    # Proxy admin viewer
    pv_auth = UserAPIKeyAuth(user_id="proxy-viewer", user_role=LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY)
    opts_pv = await get_invite_options(user_api_key_dict=pv_auth)
    assert opts_pv.can_invite is False
    assert opts_pv.organization_ids == []
    assert opts_pv.team_ids == []

    # Internal user viewer
    uv_auth = UserAPIKeyAuth(user_id="viewer-user", user_role=LitellmUserRoles.INTERNAL_USER_VIEW_ONLY)
    opts_uv = await get_invite_options(user_api_key_dict=uv_auth)
    assert opts_uv.can_invite is False
    assert opts_uv.organization_ids == []
    assert opts_uv.team_ids == []


@pytest.mark.asyncio
async def test_invite_options_plain_member(rbac_setup):
    auth = UserAPIKeyAuth(user_id="member-user", user_role=LitellmUserRoles.INTERNAL_USER)
    opts = await get_invite_options(user_api_key_dict=auth)
    assert opts.can_invite is False
    assert opts.organization_ids == []
    assert opts.team_ids == []


# =====================================================================
# Tests for POST /user/new
# =====================================================================

@pytest.mark.asyncio
async def test_new_user_proxy_admin_can_invite_anywhere(rbac_setup):
    auth = UserAPIKeyAuth(user_id="proxy-admin", user_role=LitellmUserRoles.PROXY_ADMIN)

    # Proxy admin invites user to any org and any team
    req = NewUserRequest(
        user_email="new-user@example.com",
        organizations=["org-b"],
        teams=["team-b1"],
        team_role="admin",
        org_role="org_admin",
    )
    res = await new_user(data=req, user_api_key_dict=auth)
    assert res is not None

    # Proxy admin invites global user without org or team
    req_global = NewUserRequest(
        user_email="global-user@example.com",
        user_role="proxy_admin_viewer",
    )
    res_global = await new_user(data=req_global, user_api_key_dict=auth)
    assert res_global is not None


@pytest.mark.asyncio
async def test_new_user_org_admin_scope(rbac_setup):
    auth = UserAPIKeyAuth(user_id="org-admin-a", user_role=LitellmUserRoles.INTERNAL_USER)

    # Allowed: invite into own org-a and team in org-a
    req_ok = NewUserRequest(
        user_email="new-org-user@example.com",
        organizations=["org-a"],
        teams=["team-a1"],
        team_role="admin",
        org_role="internal_user",
    )
    res_ok = await new_user(data=req_ok, user_api_key_dict=auth)
    assert res_ok is not None

    # Denied: invite into org-b (not their org)
    req_bad_org = NewUserRequest(
        user_email="bad-org@example.com",
        organizations=["org-b"],
    )
    with pytest.raises(ProxyException) as exc:
        await new_user(data=req_bad_org, user_api_key_dict=auth)
    assert exc.value.code == 403 or exc.value.code == "403"
    assert "Not allowed to invite into organization 'org-b'" in str(exc.value.message)

    # Denied: invite into team-b1 (in org-b)
    req_bad_team = NewUserRequest(
        user_email="bad-team@example.com",
        teams=["team-b1"],
    )
    with pytest.raises(ProxyException) as exc:
        await new_user(data=req_bad_team, user_api_key_dict=auth)
    assert exc.value.code == 403 or exc.value.code == "403"

    # Denied: assign proxy_admin role
    req_bad_role = NewUserRequest(
        user_email="bad-role@example.com",
        organizations=["org-a"],
        user_role="proxy_admin",
    )
    with pytest.raises(ProxyException) as exc:
        await new_user(data=req_bad_role, user_api_key_dict=auth)
    assert exc.value.code == 403 or exc.value.code == "403"

    # Denied: invite with no org and no team
    req_no_scope = NewUserRequest(
        user_email="no-scope@example.com",
    )
    with pytest.raises(ProxyException) as exc:
        await new_user(data=req_no_scope, user_api_key_dict=auth)
    assert exc.value.code == 403 or exc.value.code == "403"
    assert "Org admins and team admins must invite into an organization or team they manage" in str(exc.value.message)


@pytest.mark.asyncio
async def test_new_user_team_admin_scope(rbac_setup):
    auth = UserAPIKeyAuth(user_id="team-admin-a1", user_role=LitellmUserRoles.INTERNAL_USER)

    # Allowed: invite into team-a1 with role "user"
    req_ok = NewUserRequest(
        user_email="team-member@example.com",
        teams=["team-a1"],
        team_role="user",
    )
    res_ok = await new_user(data=req_ok, user_api_key_dict=auth)
    assert res_ok is not None

    # Denied: invite into team-a1 with role "admin"
    req_admin_role = NewUserRequest(
        user_email="team-admin2@example.com",
        teams=["team-a1"],
        team_role="admin",
    )
    with pytest.raises(ProxyException) as exc:
        await new_user(data=req_admin_role, user_api_key_dict=auth)
    assert exc.value.code == 403 or exc.value.code == "403"
    assert "Team admins can only invite members to their team, not other team admins" in str(exc.value.message)

    # Denied: invite into another team (team-a2)
    req_other_team = NewUserRequest(
        user_email="other-team@example.com",
        teams=["team-a2"],
        team_role="user",
    )
    with pytest.raises(ProxyException) as exc:
        await new_user(data=req_other_team, user_api_key_dict=auth)
    assert exc.value.code == 403 or exc.value.code == "403"
    assert "Team admins can only invite users to teams they administer" in str(exc.value.message)

    # Denied: invite into an organization
    req_org = NewUserRequest(
        user_email="org-target@example.com",
        organizations=["org-a"],
        teams=["team-a1"],
    )
    with pytest.raises(ProxyException) as exc:
        await new_user(data=req_org, user_api_key_dict=auth)
    assert exc.value.code == 403 or exc.value.code == "403"
    assert "Team admins invite to their team only, not to an organization" in str(exc.value.message)

    # Denied: grant org_admin
    req_grant_org = NewUserRequest(
        user_email="grant-org@example.com",
        teams=["team-a1"],
        org_role="org_admin",
    )
    with pytest.raises(ProxyException) as exc:
        await new_user(data=req_grant_org, user_api_key_dict=auth)
    assert exc.value.code == 403 or exc.value.code == "403"
    assert "Team admins cannot grant organization admin" in str(exc.value.message)


@pytest.mark.asyncio
async def test_new_user_member_and_viewers_denied(rbac_setup):
    # Member cannot invite
    member_auth = UserAPIKeyAuth(user_id="member-user", user_role=LitellmUserRoles.INTERNAL_USER)
    req = NewUserRequest(user_email="m-invite@example.com", teams=["team-a1"])
    with pytest.raises(ProxyException) as exc:
        await new_user(data=req, user_api_key_dict=member_auth)
    assert exc.value.code == 403 or exc.value.code == "403"

    # Viewer cannot invite
    viewer_auth = UserAPIKeyAuth(user_id="viewer-user", user_role=LitellmUserRoles.INTERNAL_USER_VIEW_ONLY)
    with pytest.raises(ProxyException) as exc:
        await new_user(data=req, user_api_key_dict=viewer_auth)
    assert exc.value.code == 403 or exc.value.code == "403"
    assert "View-only roles cannot invite users" in str(exc.value.message)


# =====================================================================
# Tests for Bulk User Creation /management/v1/users/bulk
# =====================================================================

@pytest.mark.asyncio
async def test_bulk_create_users_team_admin_matrix(rbac_setup):
    prisma = rbac_setup["prisma"]
    auth = UserAPIKeyAuth(user_id="team-admin-a1", user_role=LitellmUserRoles.INTERNAL_USER)

    items = [
        # row 0: valid invite to team-a1 with team_role="user"
        BulkNewUserItem(user_id="bulk-u1", user_email="b1@example.com", teams=["team-a1"], team_role="user"),
        # row 1: invalid team_role="admin"
        BulkNewUserItem(user_id="bulk-u2", user_email="b2@example.com", teams=["team-a1"], team_role="admin"),
        # row 2: invalid team (team-a2)
        BulkNewUserItem(user_id="bulk-u3", user_email="b3@example.com", teams=["team-a2"], team_role="user"),
        # row 3: invalid global role proxy_admin
        BulkNewUserItem(user_id="bulk-u4", user_email="b4@example.com", teams=["team-a1"], user_role="proxy_admin"),
    ]

    res = await bulk_create_users(
        users=items,
        user_api_key_dict=auth,
        prisma_client=prisma,
        license_check=_MockLicense(),
        litellm_proxy_admin_name="default_admin",
        user_api_key_cache=DualCache(),
    )

    assert res.meta.total_requested == 4
    assert res.meta.created == 1
    assert res.meta.failed == 3

    assert res.data[0].success is True
    assert res.data[0].user_id == "bulk-u1"

    assert res.data[1].success is False
    assert "Team admins can only invite members to their team, not other team admins" in (res.data[1].error or "")

    assert res.data[2].success is False
    assert "team-a2" in (res.data[2].error or "")

    assert res.data[3].success is False
    assert "Only proxy admins can create administrative users" in (res.data[3].error or "")
