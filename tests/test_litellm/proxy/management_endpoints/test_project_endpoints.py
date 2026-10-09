import json
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from litellm.proxy._types import LiteLLM_BudgetTable, LiteLLM_ProjectTable, LiteLLM_TeamTable, Member, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.management_endpoints.project_endpoints import (
    CallerScope,
    get_caller_scope,
    get_project_store,
    prisma_data,
    project_access,
    router,
)
from tests.test_litellm.proxy.auth.license_test_helpers import (
    install_entitlements,
    licensed_entitlements,
    unlicensed_entitlements,
)

ORG_A: Final = "org-a"
ORG_B: Final = "org-b"
TEAM_A: Final = LiteLLM_TeamTable(
    team_id="team-a",
    organization_id=ORG_A,
    models=["gpt-4o", "claude"],
    members_with_roles=[Member(user_id="alice", role="admin"), Member(user_id="bob", role="user")],
)
TEAM_B: Final = LiteLLM_TeamTable(team_id="team-b", organization_id=ORG_B, models=["all-proxy-models"])


def _scope(
    user_id: str | None = "someone",
    is_proxy_admin: bool = False,
    team_ids: frozenset[str] = frozenset(),
    admin_org_ids: frozenset[str] = frozenset(),
    readable_org_ids: frozenset[str] = frozenset(),
    team_admins_manage_projects: bool = False,
) -> CallerScope:
    return CallerScope(user_id, is_proxy_admin, team_ids, admin_org_ids, readable_org_ids, team_admins_manage_projects)


PROXY_ADMIN: Final = _scope(user_id="root", is_proxy_admin=True)
ORG_A_ADMIN: Final = _scope(user_id="carol", admin_org_ids=frozenset({ORG_A}), readable_org_ids=frozenset({ORG_A}))
ORG_A_VIEWER: Final = _scope(user_id="vic", readable_org_ids=frozenset({ORG_A}))
TEAM_A_ADMIN: Final = _scope(user_id="alice", team_ids=frozenset({"team-a"}))
TEAM_A_MEMBER: Final = _scope(user_id="bob", team_ids=frozenset({"team-a"}))
OUTSIDER: Final = _scope(user_id="eve", team_ids=frozenset({"team-b"}))


class FakeProjectStore:
    def __init__(self) -> None:
        self.teams: dict[str, LiteLLM_TeamTable] = {t.team_id: t for t in (TEAM_A, TEAM_B)}
        self.projects_by_id: dict[str, LiteLLM_ProjectTable] = {}
        self.budgets: dict[str, dict[str, object]] = {"shared-budget": {"max_budget": 5.0}}
        self.keys_per_project: dict[str, int] = {}
        self.forgotten: list[str] = []

    async def team(self, team_id: str) -> LiteLLM_TeamTable | None:
        return self.teams.get(team_id)

    async def team_ids_in_orgs(self, org_ids: frozenset[str]) -> frozenset[str]:
        return frozenset(t.team_id for t in self.teams.values() if t.organization_id in org_ids)

    async def project(self, project_id: str) -> LiteLLM_ProjectTable | None:
        return self.projects_by_id.get(project_id)

    async def projects(self, team_ids: frozenset[str] | None) -> tuple[LiteLLM_ProjectTable, ...]:
        return tuple(p for p in self.projects_by_id.values() if team_ids is None or p.team_id in team_ids)

    async def budget_exists(self, budget_id: str) -> bool:
        return budget_id in self.budgets

    async def create_budget(self, fields: Mapping[str, object], actor: str) -> str:
        budget_id: Final = f"budget-{len(self.budgets)}"
        self.budgets[budget_id] = dict(fields)
        return budget_id

    async def update_budget(self, budget_id: str, fields: Mapping[str, object], actor: str) -> None:
        self.budgets[budget_id] = {**self.budgets[budget_id], **fields}

    def _with_budget(self, project: LiteLLM_ProjectTable) -> LiteLLM_ProjectTable:
        budget: Final = self.budgets.get(project.budget_id) if project.budget_id else None
        return project.model_copy(
            update={
                "litellm_budget_table": None
                if budget is None
                else LiteLLM_BudgetTable.model_validate({**budget, "budget_id": project.budget_id})
            }
        )

    async def create_project(self, fields: Mapping[str, object]) -> LiteLLM_ProjectTable:
        project: Final = self._with_budget(
            LiteLLM_ProjectTable.model_validate({"project_id": f"project-{len(self.projects_by_id)}", **fields})
        )
        self.projects_by_id[project.project_id] = project
        return project

    async def update_project(self, project_id: str, fields: Mapping[str, object]) -> LiteLLM_ProjectTable:
        current: Final = self.projects_by_id[project_id]
        project: Final = self._with_budget(
            LiteLLM_ProjectTable.model_validate({**current.model_dump(exclude={"litellm_budget_table"}), **fields})
        )
        self.projects_by_id[project_id] = project
        return project

    async def attached_key_count(self, project_ids: tuple[str, ...]) -> int:
        return sum(self.keys_per_project.get(p, 0) for p in project_ids)

    async def delete_projects(self, project_ids: tuple[str, ...]) -> None:
        for project_id in project_ids:
            del self.projects_by_id[project_id]

    async def forget_cached(self, project_id: str) -> None:
        self.forgotten.append(project_id)


class Harness:
    def __init__(self) -> None:
        self.store: Final = FakeProjectStore()
        self.scope: CallerScope = PROXY_ADMIN
        app: Final = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_project_store] = lambda: self.store
        app.dependency_overrides[get_caller_scope] = lambda: self.scope
        app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(user_id=self.scope.user_id)
        self.client: Final = TestClient(app)

    def as_caller(self, scope: CallerScope) -> "Harness":
        self.scope = scope
        return self

    def seed(self, project_id: str, team_id: str = "team-a", **fields: object) -> LiteLLM_ProjectTable:
        project: Final = LiteLLM_ProjectTable.model_validate({"project_id": project_id, "team_id": team_id, **fields})
        self.store.projects_by_id[project_id] = project
        return project


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    install_entitlements(monkeypatch, licensed_entitlements(features=("projects", "advanced_keys", "budgets")))
    yield Harness()


@pytest.mark.parametrize(
    ("scope", "team", "expected"),
    [
        (PROXY_ADMIN, TEAM_B, "write"),
        (ORG_A_ADMIN, TEAM_A, "write"),
        (ORG_A_ADMIN, TEAM_B, "none"),
        (ORG_A_VIEWER, TEAM_A, "read"),
        (TEAM_A_ADMIN, TEAM_A, "read"),
        (_scope(user_id="bob"), TEAM_A, "read"),
        (_scope(user_id="alice", team_admins_manage_projects=True), TEAM_A, "write"),
        (_scope(user_id="bob", team_admins_manage_projects=True), TEAM_A, "read"),
        (_scope(user_id="key-only", team_ids=frozenset({"team-a"})), TEAM_A, "read"),
        (OUTSIDER, TEAM_A, "none"),
        (
            _scope(user_id=None),
            LiteLLM_TeamTable(team_id="t", members_with_roles=[Member(user_email="x@y.z", role="admin")]),
            "none",
        ),
    ],
)
def test_project_access_follows_tenant_scope(scope: CallerScope, team: LiteLLM_TeamTable, expected: str) -> None:
    assert project_access(scope, team) == expected


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/project/new", {"team_id": "team-a"}),
        ("post", "/project/update", {"project_id": "p1"}),
        ("delete", "/project/delete", {"project_ids": ["p1"]}),
        ("get", "/project/info?project_id=p1", None),
        ("get", "/project/list", None),
    ],
)
def test_every_route_requires_the_projects_licence(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: dict[str, object] | None
) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=("budgets", "advanced_keys")))
    h: Final = Harness()
    h.seed("p1")
    response: Final = h.client.request(method, path, json=body)
    assert response.status_code == 403
    assert "p1" in h.store.projects_by_id
    assert h.store.projects_by_id["p1"].project_alias is None


def test_unlicensed_proxy_cannot_create_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entitlements(monkeypatch, unlicensed_entitlements())
    h: Final = Harness()
    assert h.client.post("/project/new", json={"team_id": "team-a"}).status_code == 403
    assert h.store.projects_by_id == {}


def test_org_admin_creates_project_with_its_own_budget_and_tags(harness: Harness) -> None:
    response: Final = harness.as_caller(ORG_A_ADMIN).client.post(
        "/project/new",
        json={
            "team_id": "team-a",
            "project_alias": "search",
            "description": "search backend",
            "models": ["gpt-4o"],
            "max_budget": 12.5,
            "rpm_limit": 30,
            "budget_duration": "30d",
            "tags": ["prod"],
            "metadata": {"owner": "search-team"},
        },
    )
    assert response.status_code == 200, response.text
    body: Final = response.json()
    stored: Final = harness.store.projects_by_id[body["project_id"]]
    assert (stored.team_id, stored.project_alias, stored.description, stored.models, stored.blocked) == (
        "team-a",
        "search",
        "search backend",
        ["gpt-4o"],
        False,
    )
    assert stored.metadata == {"owner": "search-team", "tags": ["prod"]}
    assert stored.created_by == "carol"
    assert harness.store.budgets[stored.budget_id or ""] == {
        "max_budget": 12.5,
        "rpm_limit": 30,
        "budget_duration": "30d",
    }
    assert body["litellm_budget_table"]["max_budget"] == 12.5


def test_create_links_an_existing_budget_without_creating_one(harness: Harness) -> None:
    response: Final = harness.client.post("/project/new", json={"team_id": "team-a", "budget_id": "shared-budget"})
    assert response.status_code == 200, response.text
    assert harness.store.projects_by_id[response.json()["project_id"]].budget_id == "shared-budget"
    assert set(harness.store.budgets) == {"shared-budget"}


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"team_id": "team-a", "budget_id": "shared-budget", "max_budget": 3}, 400),
        ({"team_id": "team-a", "budget_id": "missing"}, 404),
        ({"team_id": "team-a", "max_budget": -1}, 400),
        ({"team_id": "team-a", "soft_budget": -0.5}, 400),
        ({"team_id": "team-a", "budget_duration": "soon"}, 400),
        ({"team_id": "team-a", "models": ["gpt-4o", "o3"]}, 400),
        ({"team_id": "team-missing"}, 404),
    ],
)
def test_create_rejects_invalid_requests_without_writing(
    harness: Harness, body: dict[str, object], status: int
) -> None:
    response: Final = harness.client.post("/project/new", json=body)
    assert response.status_code == status, response.text
    assert harness.store.projects_by_id == {}
    assert set(harness.store.budgets) == {"shared-budget"}


@pytest.mark.parametrize("value", ["Infinity", "NaN"])
def test_create_rejects_a_non_finite_budget(harness: Harness, value: str) -> None:
    response: Final = harness.client.post(
        "/project/new",
        content=f'{{"team_id": "team-a", "max_budget": {value}}}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert harness.store.projects_by_id == {}


def test_team_with_all_proxy_models_accepts_any_model(harness: Harness) -> None:
    response: Final = harness.client.post("/project/new", json={"team_id": "team-b", "models": ["o3"]})
    assert response.status_code == 200, response.text


def test_duplicate_project_id_is_rejected(harness: Harness) -> None:
    harness.seed("p1", project_alias="original")
    response: Final = harness.client.post("/project/new", json={"team_id": "team-a", "project_id": "p1"})
    assert response.status_code == 400
    assert harness.store.projects_by_id["p1"].project_alias == "original"


def test_tags_need_their_own_licence(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=("projects",)))
    h: Final = Harness()
    assert h.client.post("/project/new", json={"team_id": "team-a", "tags": ["prod"]}).status_code == 403
    assert h.client.post("/project/new", json={"team_id": "team-a"}).status_code == 200
    assert len(h.store.projects_by_id) == 1


@pytest.mark.parametrize(
    ("scope", "status"), [(OUTSIDER, 404), (TEAM_A_MEMBER, 403), (TEAM_A_ADMIN, 403), (ORG_A_VIEWER, 403)]
)
def test_callers_without_write_access_cannot_create(harness: Harness, scope: CallerScope, status: int) -> None:
    assert harness.as_caller(scope).client.post("/project/new", json={"team_id": "team-a"}).status_code == status
    assert harness.store.projects_by_id == {}


def test_team_admin_creates_when_the_proxy_allows_it(harness: Harness) -> None:
    caller: Final = _scope(user_id="alice", team_admins_manage_projects=True)
    assert harness.as_caller(caller).client.post("/project/new", json={"team_id": "team-a"}).status_code == 200


def test_update_merges_metadata_updates_budget_in_place_and_drops_the_cache(harness: Harness) -> None:
    harness.store.budgets["b1"] = {"max_budget": 1.0, "rpm_limit": 5}
    harness.seed("p1", budget_id="b1", project_alias="old", metadata={"owner": "x", "tags": ["dev"]})
    response: Final = harness.as_caller(ORG_A_ADMIN).client.post(
        "/project/update",
        json={"project_id": "p1", "project_alias": "new", "max_budget": 9.0, "tags": ["prod"], "blocked": True},
    )
    assert response.status_code == 200, response.text
    stored: Final = harness.store.projects_by_id["p1"]
    assert (stored.project_alias, stored.blocked, stored.budget_id, stored.updated_by) == ("new", True, "b1", "carol")
    assert stored.metadata == {"owner": "x", "tags": ["prod"]}
    assert harness.store.budgets["b1"] == {"max_budget": 9.0, "rpm_limit": 5}
    assert response.json()["litellm_budget_table"]["max_budget"] == 9.0
    assert harness.store.forgotten == ["p1"]


def test_update_creates_and_links_a_budget_when_the_project_has_none(harness: Harness) -> None:
    harness.seed("p1")
    response: Final = harness.client.post("/project/update", json={"project_id": "p1", "tpm_limit": 1000})
    assert response.status_code == 200, response.text
    budget_id: Final = harness.store.projects_by_id["p1"].budget_id
    assert budget_id is not None and budget_id != "shared-budget"
    assert harness.store.budgets[budget_id] == {"tpm_limit": 1000}


def test_update_leaves_unspecified_fields_alone(harness: Harness) -> None:
    harness.seed("p1", project_alias="keep", models=["claude"], metadata={"owner": "x"})
    assert harness.client.post("/project/update", json={"project_id": "p1", "description": "d"}).status_code == 200
    stored: Final = harness.store.projects_by_id["p1"]
    assert (stored.project_alias, stored.models, stored.metadata, stored.description) == (
        "keep",
        ["claude"],
        {"owner": "x"},
        "d",
    )


@pytest.mark.parametrize(
    ("scope", "body", "status"),
    [
        (PROXY_ADMIN, {"project_id": "p1", "team_id": "team-b"}, 400),
        (PROXY_ADMIN, {"project_id": "p1", "models": ["o3"]}, 400),
        (PROXY_ADMIN, {"project_id": "p1", "budget_id": "missing"}, 404),
        (PROXY_ADMIN, {"project_id": "missing", "project_alias": "x"}, 404),
        (OUTSIDER, {"project_id": "p1", "project_alias": "x"}, 404),
        (TEAM_A_MEMBER, {"project_id": "p1", "project_alias": "x"}, 403),
        (ORG_A_VIEWER, {"project_id": "p1", "project_alias": "x"}, 403),
    ],
)
def test_update_rejections_leave_the_project_unchanged(
    harness: Harness, scope: CallerScope, body: dict[str, object], status: int
) -> None:
    original: Final = harness.seed("p1", project_alias="orig")
    assert harness.as_caller(scope).client.post("/project/update", json=body).status_code == status
    assert harness.store.projects_by_id["p1"] == original
    assert harness.store.forgotten == []


def test_delete_is_refused_while_keys_are_attached(harness: Harness) -> None:
    harness.seed("p1")
    harness.seed("p2")
    harness.store.keys_per_project["p2"] = 1
    response: Final = harness.client.request("DELETE", "/project/delete", json={"project_ids": ["p1", "p2"]})
    assert response.status_code == 400
    assert set(harness.store.projects_by_id) == {"p1", "p2"}
    assert harness.store.forgotten == []


def test_delete_removes_projects_and_drops_their_cache(harness: Harness) -> None:
    harness.seed("p1")
    harness.seed("p2")
    harness.seed("p3")
    response: Final = harness.as_caller(ORG_A_ADMIN).client.request(
        "DELETE", "/project/delete", json={"project_ids": ["p1", "p2", "p1"]}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted_project_ids": ["p1", "p2"]}
    assert set(harness.store.projects_by_id) == {"p3"}
    assert harness.store.forgotten == ["p1", "p2"]


@pytest.mark.parametrize(("scope", "status"), [(OUTSIDER, 404), (TEAM_A_MEMBER, 403), (ORG_A_VIEWER, 403)])
def test_delete_needs_write_access_to_every_project(harness: Harness, scope: CallerScope, status: int) -> None:
    harness.seed("p1")
    response: Final = harness.as_caller(scope).client.request("DELETE", "/project/delete", json={"project_ids": ["p1"]})
    assert response.status_code == status
    assert "p1" in harness.store.projects_by_id


def test_delete_rejects_an_empty_list(harness: Harness) -> None:
    assert harness.client.request("DELETE", "/project/delete", json={"project_ids": []}).status_code == 400


@pytest.mark.parametrize(
    ("scope", "status"), [(TEAM_A_MEMBER, 200), (ORG_A_VIEWER, 200), (OUTSIDER, 404), (ORG_A_ADMIN, 200)]
)
def test_info_is_visible_only_inside_the_tenant(harness: Harness, scope: CallerScope, status: int) -> None:
    harness.seed("p1", project_alias="secret")
    response: Final = harness.as_caller(scope).client.get("/project/info", params={"project_id": "p1"})
    assert response.status_code == status
    assert ("secret" in response.text) == (status == 200)


def test_info_for_a_missing_project_is_404(harness: Harness) -> None:
    assert harness.client.get("/project/info", params={"project_id": "nope"}).status_code == 404


@pytest.mark.parametrize(
    ("scope", "visible"),
    [
        (PROXY_ADMIN, {"pa", "pb"}),
        (ORG_A_VIEWER, {"pa"}),
        (OUTSIDER, {"pb"}),
        (TEAM_A_MEMBER, {"pa"}),
        (_scope(user_id="nobody"), set()),
    ],
)
def test_list_returns_only_projects_in_the_callers_scope(
    harness: Harness, scope: CallerScope, visible: set[str]
) -> None:
    harness.seed("pa", team_id="team-a")
    harness.seed("pb", team_id="team-b")
    response: Final = harness.as_caller(scope).client.get("/project/list")
    assert response.status_code == 200
    assert {p["project_id"] for p in response.json()} == visible


def test_prisma_data_is_a_real_dict_with_json_encoded_mappings():
    nested: Final = MappingProxyType({"tags": ("a",), "inner": MappingProxyType({"k": 1})})
    data: Final = prisma_data(MappingProxyType({"metadata": nested, "models": ["m"], "description": None}))

    assert type(data) is dict
    assert set(data) == {"metadata", "models"}
    assert data["models"] == ["m"]
    assert isinstance(data["metadata"], str)
    assert json.loads(data["metadata"]) == {"tags": ["a"], "inner": {"k": 1}}
