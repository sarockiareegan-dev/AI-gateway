"""
Handler-level checks that PROXY_ADMIN_VIEW_ONLY, which is scoped to its own organizations, cannot read
proxy-wide settings even when the route gate is bypassed (the auth dependency is overridden here).
"""

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import litellm.proxy.proxy_server as ps
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.proxy_server import app


def _client_as(monkeypatch: pytest.MonkeyPatch, role: LitellmUserRoles) -> TestClient:
    mock_budget_table = MagicMock()
    mock_budget_table.find_many = AsyncMock(return_value=[])
    mock_budget_table.find_first = AsyncMock(return_value=None)
    mock_invitation_table = MagicMock()
    mock_invitation_table.find_unique = AsyncMock(return_value=None)
    mock_config_table = MagicMock()
    mock_config_table.find_first = AsyncMock(return_value=None)
    mock_prisma = MagicMock()
    mock_prisma.db = types.SimpleNamespace(
        litellm_budgettable=mock_budget_table,
        litellm_invitationlink=mock_invitation_table,
        litellm_config=mock_config_table,
        query_raw=AsyncMock(side_effect=[[{"count": 0}], []]),
    )
    monkeypatch.setattr(ps, "prisma_client", mock_prisma)
    app.dependency_overrides[ps.user_api_key_auth] = lambda: UserAPIKeyAuth(user_id="viewer_user", user_role=role)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


PROXY_WIDE_READS = [
    ("/budget/list", {}),
    ("/management/v1/budgets", {}),
    ("/budget/settings", {"budget_id": "b1"}),
    ("/alerting/settings", {}),
    ("/config/field/info", {"field_name": "alerting"}),
    ("/config/list", {"config_type": "general_settings"}),
    ("/invitation/info", {"invitation_id": "nonexistent"}),
    ("/schedule/model_cost_map_reload/status", {}),
    ("/model/cost_map/source", {}),
]


@pytest.mark.parametrize(("path", "params"), PROXY_WIDE_READS)
def test_proxy_wide_read_handlers_refuse_the_viewer(monkeypatch, path, params):
    response = _client_as(monkeypatch, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY).get(path, params=params)

    assert response.status_code in (400, 401, 403), response.text


@pytest.mark.parametrize(("path", "params"), [("/budget/list", {}), ("/management/v1/budgets", {})])
def test_proxy_wide_read_handlers_still_serve_the_proxy_admin(monkeypatch, path, params):
    response = _client_as(monkeypatch, LitellmUserRoles.PROXY_ADMIN).get(path, params=params)

    assert response.status_code == 200, response.text
