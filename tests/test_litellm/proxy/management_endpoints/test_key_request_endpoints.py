from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import Final

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from litellm.proxy._types import (
    GenerateKeyRequest,
    GenerateKeyResponse,
    KeyManagementRoutes,
    LiteLLM_TeamTable,
    LiteLLM_TeamTableCachedObj,
    LitellmUserRoles,
    Member,
    UserAPIKeyAuth,
)
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.management_endpoints.key_management_endpoints import (
    _team_key_operation_team_member_check,
    approved_key_request,
)
from litellm.proxy.management_endpoints.key_request_endpoints import (
    CallerScope,
    GenerateKeyIssuer,
    IssuedKey,
    KeyRequest,
    KeyRequestStatus,
    get_caller_scope,
    get_key_issuer,
    get_key_request_store,
    router,
)

TEAM: Final = LiteLLM_TeamTable(
    team_id="team-a",
    organization_id="org-a",
    models=["gpt-4o", "claude", "mini"],
    members_with_roles=[
        Member(user_id="alice", role="admin"),
        Member(user_id="bob", role="user"),
        Member(user_id="vera", role="user"),
    ],
)
OTHER_TEAM: Final = LiteLLM_TeamTable(team_id="team-b", organization_id="org-b", members_with_roles=[])

BOB: Final = CallerScope("bob", LitellmUserRoles.INTERNAL_USER.value, frozenset())
VERA_VIEWER: Final = CallerScope("vera", LitellmUserRoles.INTERNAL_USER_VIEW_ONLY.value, frozenset())
ALICE_TEAM_ADMIN: Final = CallerScope("alice", LitellmUserRoles.INTERNAL_USER.value, frozenset())
CAROL_ORG_ADMIN: Final = CallerScope("carol", LitellmUserRoles.INTERNAL_USER.value, frozenset({"org-a"}))
DAN_OTHER_ORG_ADMIN: Final = CallerScope("dan", LitellmUserRoles.INTERNAL_USER.value, frozenset({"org-b"}))
ROOT: Final = CallerScope("root", LitellmUserRoles.PROXY_ADMIN.value, frozenset())
EVE: Final = CallerScope("eve", LitellmUserRoles.INTERNAL_USER.value, frozenset())


class FakeStore:
    def __init__(self) -> None:
        self.teams: dict[str, LiteLLM_TeamTable] = {t.team_id: t for t in (TEAM, OTHER_TEAM)}
        self.rows: dict[str, dict[str, object]] = {}

    def _view(self, row: Mapping[str, object]) -> KeyRequest:
        return KeyRequest.model_validate({**row, "key_ready": row.get("encrypted_key") is not None})

    async def team(self, team_id: str) -> LiteLLM_TeamTable | None:
        return self.teams.get(team_id)

    async def create(self, fields: Mapping[str, object]) -> KeyRequest:
        request_id: Final = f"req-{len(self.rows)}"
        self.rows[request_id] = {"request_id": request_id, "status": "pending", **fields}
        return self._view(self.rows[request_id])

    async def get(self, request_id: str) -> KeyRequest | None:
        row: Final = self.rows.get(request_id)
        return None if row is None else self._view(row)

    async def find(
        self, team_id: str | None, user_id: str | None, status: KeyRequestStatus | None
    ) -> tuple[KeyRequest, ...]:
        return tuple(
            self._view(r)
            for r in self.rows.values()
            if (team_id is None or r["team_id"] == team_id)
            and (user_id is None or r["user_id"] == user_id)
            and (status is None or r["status"] == status)
        )

    async def transition(
        self, request_id: str, from_status: KeyRequestStatus, fields: Mapping[str, object]
    ) -> KeyRequest | None:
        row: Final = self.rows.get(request_id)
        if row is None or row["status"] != from_status:
            return None
        row.update(fields)
        return self._view(row)

    async def take_key(self, request_id: str) -> str | None:
        row: Final = self.rows.get(request_id)
        encrypted: Final = None if row is None else row.get("encrypted_key")
        if row is None or encrypted is None:
            return None
        row["encrypted_key"] = None
        return str(encrypted)


class FakeIssuer:
    def __init__(self) -> None:
        self.issued: list[tuple[KeyRequest, str]] = []
        self.fail = False

    async def issue(self, request: KeyRequest, approver: str) -> IssuedKey:
        if self.fail:
            raise HTTPException(status_code=400, detail="team budget exceeded")
        self.issued.append((request, approver))
        return IssuedKey(key=f"sk-issued-{request.request_id}", token_id=f"tok-{request.request_id}")


class Harness:
    def __init__(self) -> None:
        self.store: Final = FakeStore()
        self.issuer: Final = FakeIssuer()
        self.scope: CallerScope = BOB
        app: Final = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_key_request_store] = lambda: self.store
        app.dependency_overrides[get_key_issuer] = lambda: self.issuer
        app.dependency_overrides[get_caller_scope] = lambda: self.scope
        app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(user_id=self.scope.user_id)
        self.client: Final = TestClient(app, raise_server_exceptions=False)

    def as_caller(self, scope: CallerScope) -> TestClient:
        self.scope = scope
        return self.client

    def request_key(self, **body: object) -> str:
        response: Final = self.as_caller(BOB).post(
            "/key_request/new", json={"team_id": "team-a", "key_alias": "bob-dev", **body}
        )
        assert response.status_code == 200, response.text
        return response.json()["request_id"]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("LITELLM_SALT_KEY", "sk-test-salt-for-key-requests")
    yield Harness()


def test_member_requests_a_key_and_sees_it_pending(harness: Harness) -> None:
    request_id: Final = harness.request_key(models=["gpt-4o"], max_budget=5, duration="30d", reason="prototype")
    own: Final = harness.client.get("/key_request/list").json()
    assert [(r["request_id"], r["status"], r["models"], r["reason"]) for r in own] == [
        (request_id, "pending", ["gpt-4o"], "prototype")
    ]


@pytest.mark.parametrize(
    ("scope", "team_id", "status"),
    [(EVE, "team-a", 404), (VERA_VIEWER, "team-a", 403), (BOB, "missing", 404), (CAROL_ORG_ADMIN, "team-a", 404)],
)
def test_only_non_viewer_team_members_can_request(
    harness: Harness, scope: CallerScope, team_id: str, status: int
) -> None:
    response: Final = harness.as_caller(scope).post("/key_request/new", json={"team_id": team_id})
    assert response.status_code == status


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"models": ["gpt-5"]}, "not available to team"),
        ({"max_budget": -1}, "non-negative"),
        ({"duration": "soon"}, "Invalid duration"),
    ],
)
def test_request_validation(harness: Harness, body: dict[str, object], message: str) -> None:
    response: Final = harness.as_caller(BOB).post("/key_request/new", json={"team_id": "team-a", **body})
    assert response.status_code == 400
    assert message in response.text


def test_pending_requests_are_capped(harness: Harness) -> None:
    for _ in range(10):
        harness.request_key()
    assert harness.client.post("/key_request/new", json={"team_id": "team-a"}).status_code == 429


@pytest.mark.parametrize(
    ("scope", "sees_all"),
    [(ALICE_TEAM_ADMIN, True), (CAROL_ORG_ADMIN, True), (ROOT, True), (BOB, False)],
)
def test_team_list_shows_everything_to_reviewers_only(harness: Harness, scope: CallerScope, sees_all: bool) -> None:
    harness.request_key()
    harness.store.rows["req-x"] = {"request_id": "req-x", "team_id": "team-a", "user_id": "vera", "status": "pending"}
    listed: Final = harness.as_caller(scope).get("/key_request/list", params={"team_id": "team-a"}).json()
    assert {r["user_id"] for r in listed} == ({"bob", "vera"} if sees_all else {"bob"})


def test_outsiders_cannot_list_a_team(harness: Harness) -> None:
    assert (
        harness.as_caller(DAN_OTHER_ORG_ADMIN).get("/key_request/list", params={"team_id": "team-a"}).status_code == 404
    )


@pytest.mark.parametrize("reviewer", [ALICE_TEAM_ADMIN, CAROL_ORG_ADMIN, ROOT])
def test_reviewer_approves_and_requester_collects_once(harness: Harness, reviewer: CallerScope) -> None:
    request_id: Final = harness.request_key(models=["gpt-4o", "claude"], max_budget=10)
    approved: Final = harness.as_caller(reviewer).post(
        f"/key_request/{request_id}/approve", json={"models": ["gpt-4o"], "max_budget": 4, "note": "ok"}
    )
    assert approved.status_code == 200, approved.text
    body: Final = approved.json()
    assert (body["status"], body["reviewed_by"], body["models"], body["max_budget"], body["key_ready"]) == (
        "approved",
        reviewer.user_id,
        ["gpt-4o"],
        4,
        True,
    )
    assert "sk-issued" not in approved.text
    issued_request, approver = harness.issuer.issued[0]
    assert (issued_request.user_id, issued_request.models, approver) == ("bob", ["gpt-4o"], reviewer.user_id)

    assert harness.as_caller(ALICE_TEAM_ADMIN).post(f"/key_request/{request_id}/key").status_code == 404
    collected: Final = harness.as_caller(BOB).post(f"/key_request/{request_id}/key")
    assert collected.json() == {"key": f"sk-issued-{request_id}", "key_token": f"tok-{request_id}"}
    assert harness.client.post(f"/key_request/{request_id}/key").status_code == 410
    assert harness.store.rows[request_id]["encrypted_key"] is None


@pytest.mark.parametrize("scope", [BOB, DAN_OTHER_ORG_ADMIN, EVE])
def test_non_reviewers_cannot_decide(harness: Harness, scope: CallerScope) -> None:
    request_id: Final = harness.request_key()
    client: Final = harness.as_caller(scope)
    assert client.post(f"/key_request/{request_id}/approve", json={}).status_code == (403 if scope is BOB else 404)
    assert client.post(f"/key_request/{request_id}/reject", json={}).status_code == (403 if scope is BOB else 404)
    assert harness.issuer.issued == []


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"models": ["mini"]}, "only narrow"),
        ({"max_budget": 20}, "only lower"),
    ],
)
def test_approval_can_only_tighten(harness: Harness, override: dict[str, object], message: str) -> None:
    request_id: Final = harness.request_key(models=["gpt-4o"], max_budget=10)
    response: Final = harness.as_caller(ALICE_TEAM_ADMIN).post(f"/key_request/{request_id}/approve", json=override)
    assert response.status_code == 400
    assert message in response.text
    assert harness.store.rows[request_id]["status"] == "pending"


def test_failed_issue_reopens_the_request(harness: Harness) -> None:
    request_id: Final = harness.request_key(models=["gpt-4o", "claude"])
    harness.issuer.fail = True
    response: Final = harness.as_caller(ALICE_TEAM_ADMIN).post(
        f"/key_request/{request_id}/approve", json={"models": ["claude"]}
    )
    assert response.status_code == 400
    row: Final = harness.store.rows[request_id]
    assert (row["status"], row["reviewed_by"], row["models"]) == ("pending", None, ["gpt-4o", "claude"])


def test_reject_records_reviewer_and_note(harness: Harness) -> None:
    request_id: Final = harness.request_key()
    rejected: Final = harness.as_caller(CAROL_ORG_ADMIN).post(
        f"/key_request/{request_id}/reject", json={"note": "use the shared key"}
    )
    assert (rejected.json()["status"], rejected.json()["reviewed_by"], rejected.json()["review_note"]) == (
        "rejected",
        "carol",
        "use the shared key",
    )
    assert harness.client.post(f"/key_request/{request_id}/approve", json={}).status_code == 409
    assert harness.as_caller(BOB).post(f"/key_request/{request_id}/key").status_code == 409


def test_requester_cancels_only_pending(harness: Harness) -> None:
    request_id: Final = harness.request_key()
    assert harness.as_caller(EVE).post(f"/key_request/{request_id}/cancel").status_code == 404
    assert harness.as_caller(BOB).post(f"/key_request/{request_id}/cancel").json()["status"] == "cancelled"
    assert harness.client.post(f"/key_request/{request_id}/cancel").status_code == 409


class _Users:
    async def find_unique(self, where: Mapping[str, str]) -> SimpleNamespace | None:
        return SimpleNamespace(user_role="internal_user") if where["user_id"] == "bob" else None


async def test_issuer_generates_the_key_as_the_requester_under_the_approval_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litellm.proxy.management_endpoints import key_management_endpoints

    seen: Final[dict[str, object]] = {}

    async def fake_generate_key_fn(**kwargs: object) -> GenerateKeyResponse:
        seen.update(kwargs, flag=approved_key_request.get())
        return GenerateKeyResponse(key="sk-new", token_id="tok-new")

    monkeypatch.setattr(key_management_endpoints, "generate_key_fn", fake_generate_key_fn)
    issuer: Final = GenerateKeyIssuer(SimpleNamespace(db=SimpleNamespace(litellm_usertable=_Users())))  # type: ignore[arg-type]
    request: Final = KeyRequest(request_id="req-1", team_id="team-a", user_id="bob", models=["gpt-4o"], max_budget=3)

    issued: Final = await issuer.issue(request, "alice")

    caller: Final = seen["user_api_key_dict"]
    data: Final = seen["data"]
    assert issued == IssuedKey(key="sk-new", token_id="tok-new")
    assert seen["flag"] is True and approved_key_request.get() is False
    assert isinstance(caller, UserAPIKeyAuth) and (caller.user_id, caller.user_role) == ("bob", "internal_user")
    assert isinstance(data, GenerateKeyRequest)
    assert (data.team_id, data.user_id, data.models, data.max_budget) == ("team-a", "bob", ["gpt-4o"], 3)
    assert data.metadata == {"key_request_id": "req-1", "approved_by": "alice"}
    assert seen["litellm_changed_by"] == "alice"


def _team_with_admin_only_keys() -> LiteLLM_TeamTableCachedObj:
    return LiteLLM_TeamTableCachedObj(
        team_id="team-a", members_with_roles=[Member(user_id="alice", role="admin"), Member(user_id="bob", role="user")]
    )


def _check(user_id: str) -> bool:
    return _team_key_operation_team_member_check(
        assigned_user_id=user_id,
        team_table=_team_with_admin_only_keys(),
        user_api_key_dict=UserAPIKeyAuth(user_id=user_id, user_role=LitellmUserRoles.INTERNAL_USER),
        team_key_generation={"allowed_team_member_roles": ["admin"]},
        route=KeyManagementRoutes.KEY_GENERATE,
    )


def test_approved_request_lifts_only_the_member_role_restriction() -> None:
    with pytest.raises(HTTPException):
        _check("bob")
    token: Final = approved_key_request.set(True)
    try:
        assert _check("bob") is True
        with pytest.raises(HTTPException):
            _check("eve")
    finally:
        approved_key_request.reset(token)
