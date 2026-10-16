"""Virtual key requests: a team member asks for a key, a team admin, org admin or proxy admin decides"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from litellm.proxy._types import (
    CommonProxyErrors,
    GenerateKeyRequest,
    LiteLLM_TeamTable,
    LitellmUserRoles,
    ProxyErrorDetail,
    UserAPIKeyAuth,
)
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.management_endpoints.common_utils import get_caller_user
from litellm.proxy.management_endpoints.project_endpoints import models_outside_team
from litellm.repositories.table_repositories import KeyRequestRepository
from litellm.repositories.team_repository import TeamRepository

if TYPE_CHECKING:
    from prisma.types import LiteLLM_TeamTableWhereInput

    from litellm.proxy.utils import PrismaClient

router: Final = APIRouter(tags=["key requests"])
AuthDep = Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)]

KeyRequestStatus = Literal["pending", "approved", "rejected", "cancelled"]
MAX_PENDING_PER_TEAM: Final = 10
VIEW_ONLY_ROLES: Final = frozenset(
    {LitellmUserRoles.INTERNAL_USER_VIEW_ONLY.value, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY.value}
)


class NewKeyRequest(BaseModel):
    team_id: str
    key_alias: str | None = Field(default=None, max_length=256)
    models: list[str] = Field(default_factory=list)
    max_budget: float | None = None
    duration: str | None = None
    reason: str | None = Field(default=None, max_length=1000)


class ApproveKeyRequest(BaseModel):
    key_alias: str | None = Field(default=None, max_length=256)
    models: list[str] | None = None
    max_budget: float | None = None
    duration: str | None = None
    note: str | None = Field(default=None, max_length=1000)


class RejectKeyRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class KeyRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: str
    team_id: str
    user_id: str
    key_alias: str | None = None
    models: list[str] = Field(default_factory=list)
    max_budget: float | None = None
    duration: str | None = None
    reason: str | None = None
    status: KeyRequestStatus = "pending"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    key_token: str | None = None
    key_ready: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CollectedKey(BaseModel):
    key: str
    key_token: str | None


@dataclass(frozen=True, slots=True)
class IssuedKey:
    key: str
    token_id: str | None


@dataclass(frozen=True, slots=True)
class CallerScope:
    user_id: str | None
    user_role: str | None
    admin_org_ids: frozenset[str]

    @property
    def is_proxy_admin(self) -> bool:
        return self.user_role == LitellmUserRoles.PROXY_ADMIN.value


class KeyRequestStore(Protocol):
    async def team(self, team_id: str) -> LiteLLM_TeamTable | None: ...

    async def create(self, fields: Mapping[str, object]) -> KeyRequest: ...

    async def get(self, request_id: str) -> KeyRequest | None: ...

    async def find(
        self, team_id: str | None, user_id: str | None, status: KeyRequestStatus | None
    ) -> tuple[KeyRequest, ...]: ...

    async def transition(
        self, request_id: str, from_status: KeyRequestStatus, fields: Mapping[str, object]
    ) -> KeyRequest | None:
        """Apply fields only while the request is still in from_status; None when another call got there first"""
        ...

    async def take_key(self, request_id: str) -> str | None:
        """Return the stored encrypted key and clear it, so it can be collected once"""
        ...


class KeyIssuer(Protocol):
    async def issue(self, request: KeyRequest, approver: str) -> IssuedKey: ...


def _http_error(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=ProxyErrorDetail(error=message))


def _not_found(request_id: str) -> HTTPException:
    return _http_error(404, f"Key request '{request_id}' not found")


def is_member(scope: CallerScope, team: LiteLLM_TeamTable) -> bool:
    return scope.user_id is not None and any(m.user_id == scope.user_id for m in team.members_with_roles)


def is_reviewer(scope: CallerScope, team: LiteLLM_TeamTable) -> bool:
    if scope.is_proxy_admin or (team.organization_id is not None and team.organization_id in scope.admin_org_ids):
        return True
    return scope.user_id is not None and any(
        m.user_id == scope.user_id and m.role == "admin" for m in team.members_with_roles
    )


def _validate_terms(terms: "KeyTerms", team: LiteLLM_TeamTable) -> None:
    if terms.max_budget is not None and (not math.isfinite(terms.max_budget) or terms.max_budget < 0):
        raise _http_error(400, f"max_budget must be a non-negative finite number. Received: {terms.max_budget}")
    if terms.duration is not None:
        from litellm.litellm_core_utils.duration_parser import duration_in_seconds

        try:
            duration_in_seconds(terms.duration)
        except ValueError as e:
            raise _http_error(400, f"Invalid duration '{terms.duration}'. Use values like 30d, 12h or 1mo") from e
    outside: Final = models_outside_team(list(terms.models), team)
    if outside:
        raise _http_error(400, f"Models {', '.join(outside)} are not available to team '{team.team_id}'")


@dataclass(frozen=True, slots=True)
class KeyTerms:
    key_alias: str | None
    models: tuple[str, ...]
    max_budget: float | None
    duration: str | None

    def fields(self) -> dict[str, object]:
        return {
            "key_alias": self.key_alias,
            "models": list(self.models),
            "max_budget": self.max_budget,
            "duration": self.duration,
        }


def requested_terms(request: KeyRequest) -> KeyTerms:
    return KeyTerms(request.key_alias, tuple(request.models), request.max_budget, request.duration)


def approved_terms(request: KeyRequest, data: ApproveKeyRequest) -> KeyTerms:
    models: Final = tuple(data.models if data.models is not None else request.models)
    if request.models and (not models or set(models) - set(request.models)):
        raise _http_error(400, "Approval can only narrow the requested models")
    max_budget: Final = data.max_budget if data.max_budget is not None else request.max_budget
    if request.max_budget is not None and max_budget is not None and max_budget > request.max_budget:
        raise _http_error(400, f"Approval can only lower the requested budget of {request.max_budget}")
    return KeyTerms(
        key_alias=data.key_alias if data.key_alias is not None else request.key_alias,
        models=models,
        max_budget=max_budget,
        duration=data.duration if data.duration is not None else request.duration,
    )


def _actor(scope: CallerScope) -> str:
    from litellm.proxy.proxy_server import litellm_proxy_admin_name

    return scope.user_id or litellm_proxy_admin_name


def get_key_request_store() -> KeyRequestStore:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise _http_error(500, CommonProxyErrors.db_not_connected_error.value)
    return PrismaKeyRequestStore(prisma_client)


def get_key_issuer() -> KeyIssuer:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise _http_error(500, CommonProxyErrors.db_not_connected_error.value)
    return GenerateKeyIssuer(prisma_client)


async def get_caller_scope(user_api_key_dict: AuthDep) -> CallerScope:
    memberships = None
    if user_api_key_dict.user_role != LitellmUserRoles.PROXY_ADMIN.value:
        try:
            user: Final = await get_caller_user(user_api_key_dict)
            memberships = user.organization_memberships if user is not None else None
        except ValueError:
            memberships = None
    return CallerScope(
        user_id=user_api_key_dict.user_id,
        user_role=user_api_key_dict.user_role,
        admin_org_ids=frozenset(
            m.organization_id for m in memberships or () if m.user_role == LitellmUserRoles.ORG_ADMIN.value
        ),
    )


ScopeDep = Annotated[CallerScope, Depends(get_caller_scope)]
StoreDep = Annotated[KeyRequestStore, Depends(get_key_request_store)]
IssuerDep = Annotated[KeyIssuer, Depends(get_key_issuer)]


async def _for_review(
    store: KeyRequestStore, scope: CallerScope, request_id: str
) -> tuple[KeyRequest, LiteLLM_TeamTable]:
    request: Final = await store.get(request_id)
    team: Final = await store.team(request.team_id) if request is not None else None
    if request is None or team is None:
        raise _not_found(request_id)
    if not is_reviewer(scope, team):
        if request.user_id == scope.user_id:
            raise _http_error(
                403, "Only a team admin, an org admin of the team's organization or a proxy admin can decide"
            )
        raise _not_found(request_id)
    if request.status != "pending":
        raise _http_error(409, f"Key request is already {request.status}")
    return request, team


def _review_fields(scope: CallerScope, status: KeyRequestStatus, note: str | None) -> dict[str, object]:
    return {
        "status": status,
        "reviewed_by": _actor(scope),
        "reviewed_at": datetime.now(timezone.utc),
        "review_note": note,
    }


@router.post("/key_request/new", response_model=KeyRequest)
async def new_key_request(data: NewKeyRequest, scope: ScopeDep, store: StoreDep) -> KeyRequest:
    """Ask the admins of a team you belong to for a virtual key"""
    team: Final = await store.team(data.team_id)
    if team is None or not is_member(scope, team):
        raise _http_error(404, f"Team '{data.team_id}' not found")
    if scope.user_role in VIEW_ONLY_ROLES:
        raise _http_error(403, "View-only users cannot request keys")
    _validate_terms(KeyTerms(data.key_alias, tuple(data.models), data.max_budget, data.duration), team)
    pending: Final = await store.find(team.team_id, scope.user_id, "pending")
    if len(pending) >= MAX_PENDING_PER_TEAM:
        raise _http_error(429, f"You already have {len(pending)} pending key requests for this team")
    return await store.create(
        MappingProxyType(
            {
                "team_id": team.team_id,
                "user_id": scope.user_id,
                "key_alias": data.key_alias,
                "models": data.models,
                "max_budget": data.max_budget,
                "duration": data.duration,
                "reason": data.reason,
            }
        )
    )


@router.get("/key_request/list", response_model=list[KeyRequest])
async def list_key_requests(
    scope: ScopeDep,
    store: StoreDep,
    team_id: Annotated[str | None, Query()] = None,
    status: Annotated[KeyRequestStatus | None, Query()] = None,
) -> tuple[KeyRequest, ...]:
    """A team's requests for its reviewers, otherwise the caller's own requests"""
    if team_id is None:
        return await store.find(None, scope.user_id, status) if scope.user_id else ()
    team: Final = await store.team(team_id)
    if team is None:
        raise _http_error(404, f"Team '{team_id}' not found")
    if is_reviewer(scope, team):
        return await store.find(team_id, None, status)
    if is_member(scope, team):
        return await store.find(team_id, scope.user_id, status)
    raise _http_error(404, f"Team '{team_id}' not found")


@router.post("/key_request/{request_id}/approve", response_model=KeyRequest)
async def approve_key_request(
    request_id: str,
    data: ApproveKeyRequest,
    scope: ScopeDep,
    store: StoreDep,
    issuer: IssuerDep,
) -> KeyRequest:
    """Create the key for the requester. The approver may narrow the models, lower the budget or change the expiry"""
    request, team = await _for_review(store, scope, request_id)
    terms: Final = approved_terms(request, data)
    _validate_terms(terms, team)
    claimed: Final = await store.transition(
        request_id, "pending", {**_review_fields(scope, "approved", data.note), **terms.fields()}
    )
    if claimed is None:
        raise _http_error(409, "Key request was decided by someone else")
    try:
        issued: Final = await issuer.issue(claimed, _actor(scope))
    except Exception:
        reopened: Final[dict[str, object]] = {
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "review_note": None,
        }
        await store.transition(request_id, "approved", {**reopened, **requested_terms(request).fields()})
        raise
    from litellm.proxy.common_utils.encrypt_decrypt_utils import encrypt_value_helper

    done: Final = await store.transition(
        request_id,
        "approved",
        {"key_token": issued.token_id, "encrypted_key": encrypt_value_helper(issued.key)},
    )
    return done or claimed


@router.post("/key_request/{request_id}/reject", response_model=KeyRequest)
async def reject_key_request(request_id: str, data: RejectKeyRequest, scope: ScopeDep, store: StoreDep) -> KeyRequest:
    """Turn the request down. The note is shown to the requester"""
    await _for_review(store, scope, request_id)
    rejected: Final = await store.transition(request_id, "pending", _review_fields(scope, "rejected", data.note))
    if rejected is None:
        raise _http_error(409, "Key request was decided by someone else")
    return rejected


async def _own(store: KeyRequestStore, scope: CallerScope, request_id: str) -> KeyRequest:
    request: Final = await store.get(request_id)
    if request is None or scope.user_id is None or request.user_id != scope.user_id:
        raise _not_found(request_id)
    return request


@router.post("/key_request/{request_id}/cancel", response_model=KeyRequest)
async def cancel_key_request(request_id: str, scope: ScopeDep, store: StoreDep) -> KeyRequest:
    request: Final = await _own(store, scope, request_id)
    cancelled: Final = (
        await store.transition(request_id, "pending", {"status": "cancelled"}) if request.status == "pending" else None
    )
    if cancelled is None:
        raise _http_error(409, f"Only pending requests can be cancelled. This one is {request.status}")
    return cancelled


@router.post("/key_request/{request_id}/key", response_model=CollectedKey)
async def collect_requested_key(request_id: str, scope: ScopeDep, store: StoreDep) -> CollectedKey:
    """Return the approved key to its requester. It is shown once and then removed"""
    request: Final = await _own(store, scope, request_id)
    if request.status != "approved":
        raise _http_error(409, f"Key request is {request.status}")
    encrypted: Final = await store.take_key(request_id)
    if encrypted is None:
        raise _http_error(410, "This key was already collected. Ask a team admin to regenerate it if it is lost")
    from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper

    key: Final = decrypt_value_helper(encrypted, "key_request")
    if key is None:
        raise _http_error(500, "Stored key could not be decrypted")
    return CollectedKey(key=key, key_token=request.key_token)


def _to_key_request(row: object) -> KeyRequest:
    request: Final = KeyRequest.model_validate(row, from_attributes=True)
    return request.model_copy(update={"key_ready": getattr(row, "encrypted_key", None) is not None})


class PrismaKeyRequestStore:
    def __init__(self, prisma_client: "PrismaClient") -> None:
        self._prisma_client = prisma_client

    @property
    def _table(self):
        return KeyRequestRepository(self._prisma_client).table

    async def team(self, team_id: str) -> LiteLLM_TeamTable | None:
        where: Final[LiteLLM_TeamTableWhereInput] = {"team_id": team_id}
        row: Final = await TeamRepository(self._prisma_client).table.find_unique(where=where)
        return None if row is None else LiteLLM_TeamTable.model_validate(row.model_dump())

    async def create(self, fields: Mapping[str, object]) -> KeyRequest:
        return _to_key_request(await self._table.create(data=dict(fields)))  # type: ignore[arg-type]

    async def get(self, request_id: str) -> KeyRequest | None:
        row: Final = await self._table.find_unique(where={"request_id": request_id})
        return None if row is None else _to_key_request(row)

    async def find(
        self, team_id: str | None, user_id: str | None, status: KeyRequestStatus | None
    ) -> tuple[KeyRequest, ...]:
        candidates: Final = (("team_id", team_id), ("user_id", user_id), ("status", status))
        rows: Final = await self._table.find_many(
            where={name: value for name, value in candidates if value is not None},  # type: ignore[arg-type]
            order={"created_at": "desc"},
        )
        return tuple(_to_key_request(row) for row in rows)

    async def transition(
        self, request_id: str, from_status: KeyRequestStatus, fields: Mapping[str, object]
    ) -> KeyRequest | None:
        count: Final = await self._table.update_many(
            where={"request_id": request_id, "status": from_status},
            data=dict(fields),  # type: ignore[arg-type]
        )
        return await self.get(request_id) if count else None

    async def take_key(self, request_id: str) -> str | None:
        row: Final = await self._table.find_unique(where={"request_id": request_id})
        encrypted: Final = None if row is None else row.encrypted_key
        if encrypted is None:
            return None
        count: Final = await self._table.update_many(
            where={"request_id": request_id, "encrypted_key": encrypted}, data={"encrypted_key": None}
        )
        return encrypted if count else None


class GenerateKeyIssuer:
    """Creates the key through /key/generate as the requester, so every team and budget check still applies"""

    def __init__(self, prisma_client: "PrismaClient") -> None:
        self._prisma_client = prisma_client

    async def issue(self, request: KeyRequest, approver: str) -> IssuedKey:
        from litellm.proxy.management_endpoints.key_management_endpoints import (
            approved_key_request,
            generate_key_fn,
        )

        requester: Final = await self._prisma_client.db.litellm_usertable.find_unique(
            where={"user_id": request.user_id}
        )
        if requester is None:
            raise _http_error(400, f"Requester '{request.user_id}' no longer exists")
        token: Final = approved_key_request.set(True)
        try:
            response: Final = await generate_key_fn(
                data=GenerateKeyRequest(
                    team_id=request.team_id,
                    user_id=request.user_id,
                    key_alias=request.key_alias,
                    models=request.models,
                    max_budget=request.max_budget,
                    duration=request.duration,
                    metadata={"key_request_id": request.request_id, "approved_by": approver},
                ),
                user_api_key_dict=UserAPIKeyAuth(user_id=request.user_id, user_role=requester.user_role),
                litellm_changed_by=approver,
            )
        finally:
            approved_key_request.reset(token)
        return IssuedKey(key=response.key, token_id=response.token_id)
