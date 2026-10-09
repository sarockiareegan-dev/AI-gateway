"""Project management: /project/new, /project/update, /project/delete, /project/info, /project/list"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from litellm.proxy._types import (
    CommonProxyErrors,
    DeleteProjectRequest,
    LiteLLM_ProjectTable,
    LiteLLM_TeamTable,
    LiteLLM_UserTable,
    LitellmUserRoles,
    NewProjectRequest,
    UpdateProjectRequest,
    UserAPIKeyAuth,
)
from litellm.proxy.auth.entitlements import LicenseFeature
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.management_endpoints.common_utils import (
    get_caller_user,
    org_wide_read_org_ids,
    require_metadata_field_licence,
    validate_budget_duration,
)
from litellm.proxy.management_endpoints.team_admin_field_permissions import team_admin_may_manage_projects
from litellm.proxy.utils import require_license_feature
from litellm.repositories.budget_repository import BudgetRepository
from litellm.repositories.project_repository import ProjectRepository
from litellm.repositories.team_repository import TeamRepository
from litellm.repositories.verification_token_repository import VerificationTokenRepository

if TYPE_CHECKING:
    from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
    from litellm.proxy.utils import PrismaClient

router: Final = APIRouter()

ProjectAccess = Literal["none", "read", "write"]

BUDGET_FIELDS: Final = (
    "max_budget",
    "soft_budget",
    "max_parallel_requests",
    "tpm_limit",
    "rpm_limit",
    "tpd_limit",
    "model_max_budget",
    "budget_duration",
)
METADATA_LIST_FIELDS: Final = ("tags", "guardrails", "policies")
ALL_PROXY_MODELS: Final = "all-proxy-models"
ProjectRequest = NewProjectRequest | UpdateProjectRequest


class _UntypedFields(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    metadata: dict[str, object] | None = None
    model_max_budget: dict[str, object] | None = None
    general_settings: dict[str, object] | None = None


def _untyped(source: object) -> _UntypedFields:
    return _UntypedFields.model_validate(source, from_attributes=True)


@dataclass(frozen=True, slots=True)
class CallerScope:
    user_id: str | None
    is_proxy_admin: bool
    team_ids: frozenset[str]
    admin_org_ids: frozenset[str]
    readable_org_ids: frozenset[str]
    team_admins_manage_projects: bool


class ProjectStore(Protocol):
    async def team(self, team_id: str) -> LiteLLM_TeamTable | None: ...

    async def team_ids_in_orgs(self, org_ids: frozenset[str]) -> frozenset[str]: ...

    async def project(self, project_id: str) -> LiteLLM_ProjectTable | None: ...

    async def projects(self, team_ids: frozenset[str] | None) -> tuple[LiteLLM_ProjectTable, ...]: ...

    async def budget_exists(self, budget_id: str) -> bool: ...

    async def create_budget(self, fields: Mapping[str, object], actor: str) -> str: ...

    async def update_budget(self, budget_id: str, fields: Mapping[str, object], actor: str) -> None: ...

    async def create_project(self, fields: Mapping[str, object]) -> LiteLLM_ProjectTable: ...

    async def update_project(self, project_id: str, fields: Mapping[str, object]) -> LiteLLM_ProjectTable: ...

    async def attached_key_count(self, project_ids: tuple[str, ...]) -> int: ...

    async def delete_projects(self, project_ids: tuple[str, ...]) -> None: ...

    async def forget_cached(self, project_id: str) -> None: ...


def project_access(scope: CallerScope, team: LiteLLM_TeamTable) -> ProjectAccess:
    if scope.is_proxy_admin or team.organization_id in scope.admin_org_ids:
        return "write"
    members: Final = tuple(m for m in team.members_with_roles if m.user_id is not None and m.user_id == scope.user_id)
    if scope.team_admins_manage_projects and any(m.role == "admin" for m in members):
        return "write"
    if members or team.team_id in scope.team_ids or team.organization_id in scope.readable_org_ids:
        return "read"
    return "none"


def budget_fields(data: ProjectRequest) -> Mapping[str, object]:
    return MappingProxyType({f: getattr(data, f) for f in BUDGET_FIELDS if getattr(data, f) is not None})


def budget_problem(fields: Mapping[str, object]) -> str | None:
    for name in ("max_budget", "soft_budget"):
        value: object = fields.get(name)
        if isinstance(value, float | int) and (not math.isfinite(value) or value < 0):
            return f"{name} must be a non-negative finite number. Received: {value}"
    return None


def models_outside_team(models: list[str] | None, team: LiteLLM_TeamTable) -> tuple[str, ...]:
    if not models or not team.models or ALL_PROXY_MODELS in team.models:
        return ()
    return tuple(m for m in models if m not in team.models)


def submitted_metadata(data: ProjectRequest) -> Mapping[str, object]:
    return MappingProxyType(
        {
            **(_untyped(data).metadata or {}),
            **{f: getattr(data, f) for f in METADATA_LIST_FIELDS if getattr(data, f) is not None},
        }
    )


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": message})


def _not_found(what: str, identifier: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"error": f"{what} '{identifier}' not found"})


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "error": "Only proxy admins, org admins of the team's organization and, when "
            "team_admin_editable_team_fields allows 'projects', team admins can change projects"
        },
    )


def _validate(data: ProjectRequest, team: LiteLLM_TeamTable) -> Mapping[str, object]:
    budget: Final = budget_fields(data)
    problem: Final = budget_problem(budget)
    if problem is not None:
        raise _bad_request(problem)
    validate_budget_duration(data.budget_duration)
    model_max_budget: Final = _untyped(data).model_max_budget
    if model_max_budget:
        from litellm.proxy.management_endpoints.key_management_endpoints import (
            validate_model_max_budget,  # pyright: ignore[reportUnknownVariableType] # untyped dict parameter upstream
        )

        try:
            validate_model_max_budget(model_max_budget)
        except ValueError as e:
            raise _bad_request(str(e)) from e
    outside: Final = models_outside_team(data.models, team)
    if outside:
        raise _bad_request(f"Models {list(outside)} are not available to team '{team.team_id}'")
    metadata: Final = submitted_metadata(data)
    for name, value in metadata.items():
        require_metadata_field_licence(name, value)
    return budget


async def _readable(
    store: ProjectStore, scope: CallerScope, project_id: str
) -> tuple[LiteLLM_ProjectTable, LiteLLM_TeamTable, ProjectAccess]:
    project: Final = await store.project(project_id)
    team: Final = await store.team(project.team_id) if project is not None and project.team_id else None
    access: Final = project_access(scope, team) if team is not None else "none"
    if project is None or team is None or access == "none":
        raise _not_found("Project", project_id)
    return project, team, access


def get_project_store() -> ProjectStore:
    from litellm.proxy.proxy_server import prisma_client, user_api_key_cache

    if prisma_client is None:
        raise HTTPException(status_code=500, detail={"error": CommonProxyErrors.db_not_connected_error.value})
    return PrismaProjectStore(prisma_client, user_api_key_cache)


async def _caller_user(user_api_key_dict: UserAPIKeyAuth) -> LiteLLM_UserTable | None:
    if user_api_key_dict.user_role == LitellmUserRoles.PROXY_ADMIN.value:
        return None
    try:
        return await get_caller_user(user_api_key_dict)
    except ValueError:
        return None


async def get_caller_scope(user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth)) -> CallerScope:
    from litellm.proxy import proxy_server

    general_settings: Final = _untyped(proxy_server).general_settings or {}
    user: Final = await _caller_user(user_api_key_dict)
    memberships: Final = user.organization_memberships if user is not None else None
    key_team_ids: Final = (user_api_key_dict.team_id,) if user_api_key_dict.team_id else ()
    return CallerScope(
        user_id=user_api_key_dict.user_id,
        is_proxy_admin=user_api_key_dict.user_role == LitellmUserRoles.PROXY_ADMIN.value,
        team_ids=frozenset((*(user.teams if user is not None else ()), *key_team_ids)),
        admin_org_ids=frozenset(
            m.organization_id for m in memberships or () if m.user_role == LitellmUserRoles.ORG_ADMIN.value
        ),
        readable_org_ids=frozenset(org_wide_read_org_ids(user_api_key_dict.user_role, memberships)),
        team_admins_manage_projects=team_admin_may_manage_projects(general_settings),
    )


def _actor(user_api_key_dict: UserAPIKeyAuth) -> str:
    from litellm.proxy.proxy_server import litellm_proxy_admin_name

    return user_api_key_dict.user_id or litellm_proxy_admin_name


def _require_projects_licence() -> None:
    require_license_feature(LicenseFeature.PROJECTS, "Projects")


@router.post("/project/new", tags=["project management"], response_model=LiteLLM_ProjectTable)
async def new_project(
    data: NewProjectRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    scope: CallerScope = Depends(get_caller_scope),
    store: ProjectStore = Depends(get_project_store),
) -> LiteLLM_ProjectTable:
    """Create a project inside a team. Budget fields create a budget for the project unless budget_id is given."""
    _require_projects_licence()
    team: Final = await store.team(data.team_id)
    access: Final = project_access(scope, team) if team is not None else "none"
    if team is None or access == "none":
        raise _not_found("Team", data.team_id)
    if access != "write":
        raise _forbidden()
    budget: Final = _validate(data, team)
    if data.budget_id is not None and budget:
        raise _bad_request("Pass either budget_id or budget fields, not both")
    if data.budget_id is not None and not await store.budget_exists(data.budget_id):
        raise _not_found("Budget", data.budget_id)
    if data.project_id is not None and await store.project(data.project_id) is not None:
        raise _bad_request(f"Project '{data.project_id}' already exists")
    actor: Final = _actor(user_api_key_dict)
    budget_id: Final = data.budget_id or (await store.create_budget(budget, actor) if budget else None)
    return await store.create_project(
        MappingProxyType(
            {
                **({"project_id": data.project_id} if data.project_id else {}),
                "project_alias": data.project_alias,
                "description": data.description,
                "team_id": team.team_id,
                "budget_id": budget_id,
                "metadata": dict(submitted_metadata(data)),
                "models": data.models,
                "blocked": data.blocked,
                "created_by": actor,
                "updated_by": actor,
            }
        )
    )


@router.post("/project/update", tags=["project management"], response_model=LiteLLM_ProjectTable)
async def update_project(
    data: UpdateProjectRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    scope: CallerScope = Depends(get_caller_scope),
    store: ProjectStore = Depends(get_project_store),
) -> LiteLLM_ProjectTable:
    """Update a project. Metadata keys are merged; budget fields update the project's budget or create one."""
    _require_projects_licence()
    project, team, access = await _readable(store, scope, data.project_id)
    if access != "write":
        raise _forbidden()
    if data.team_id is not None and data.team_id != project.team_id:
        raise _bad_request("Moving a project to another team is not supported")
    budget: Final = _validate(data, team)
    if (
        data.budget_id is not None
        and data.budget_id != project.budget_id
        and not await store.budget_exists(data.budget_id)
    ):
        raise _not_found("Budget", data.budget_id)
    actor: Final = _actor(user_api_key_dict)
    target_budget_id: Final = data.budget_id or project.budget_id
    if budget and target_budget_id is not None:
        await store.update_budget(target_budget_id, budget, actor)
    budget_id: Final = (
        target_budget_id if target_budget_id is not None or not budget else await store.create_budget(budget, actor)
    )
    metadata: Final = submitted_metadata(data)
    candidates: Final[tuple[tuple[str, object], ...]] = (
        ("project_alias", data.project_alias),
        ("description", data.description),
        ("models", data.models),
        ("blocked", data.blocked),
        ("budget_id", budget_id if budget_id != project.budget_id else None),
        ("metadata", {**(_untyped(project).metadata or {}), **metadata} if metadata else None),
    )
    changes: Final = {name: value for name, value in candidates if value is not None}
    updated: Final = await store.update_project(project.project_id, MappingProxyType({**changes, "updated_by": actor}))
    await store.forget_cached(project.project_id)
    return updated


@router.delete("/project/delete", tags=["project management"])
async def delete_project(
    data: DeleteProjectRequest,
    scope: CallerScope = Depends(get_caller_scope),
    store: ProjectStore = Depends(get_project_store),
) -> dict[str, list[str]]:
    """Delete projects. Refused while any key is still attached to one of them."""
    _require_projects_licence()
    project_ids: Final = tuple(dict.fromkeys(data.project_ids))
    if not project_ids:
        raise _bad_request("project_ids must not be empty")
    for project_id in project_ids:
        _, _, access = await _readable(store, scope, project_id)
        if access != "write":
            raise _forbidden()
    attached: Final = await store.attached_key_count(project_ids)
    if attached:
        raise _bad_request(f"{attached} key(s) are still attached to these projects. Delete or detach them first")
    await store.delete_projects(project_ids)
    for project_id in project_ids:
        await store.forget_cached(project_id)
    return {"deleted_project_ids": list(project_ids)}


@router.get("/project/info", tags=["project management"], response_model=LiteLLM_ProjectTable)
async def project_info(
    project_id: str = Query(...),
    scope: CallerScope = Depends(get_caller_scope),
    store: ProjectStore = Depends(get_project_store),
) -> LiteLLM_ProjectTable:
    _require_projects_licence()
    project, _, _ = await _readable(store, scope, project_id)
    return project


@router.get("/project/list", tags=["project management"], response_model=list[LiteLLM_ProjectTable])
async def list_projects(
    scope: CallerScope = Depends(get_caller_scope),
    store: ProjectStore = Depends(get_project_store),
) -> list[LiteLLM_ProjectTable]:
    """Projects of every team the caller belongs to or reads through its organizations; all projects for admins."""
    _require_projects_licence()
    if scope.is_proxy_admin:
        return list(await store.projects(None))
    org_team_ids: Final = await store.team_ids_in_orgs(scope.readable_org_ids)
    return list(await store.projects(scope.team_ids | org_team_ids))


class PrismaProjectStore:
    def __init__(self, prisma_client: "PrismaClient", user_api_key_cache: "UserApiKeyCache") -> None:
        self._prisma_client = prisma_client
        self._user_api_key_cache = user_api_key_cache

    async def team(self, team_id: str) -> LiteLLM_TeamTable | None:
        row: Final = await TeamRepository(self._prisma_client).table.find_unique(where={"team_id": team_id})
        return None if row is None else LiteLLM_TeamTable.model_validate(row.model_dump())

    async def team_ids_in_orgs(self, org_ids: frozenset[str]) -> frozenset[str]:
        if not org_ids:
            return frozenset()
        rows: Final = await TeamRepository(self._prisma_client).table.find_many(
            where={"organization_id": {"in": sorted(org_ids)}}
        )
        return frozenset(row.team_id for row in rows)

    async def project(self, project_id: str) -> LiteLLM_ProjectTable | None:
        row: Final = await ProjectRepository(self._prisma_client).table.find_unique(
            where={"project_id": project_id}, include={"litellm_budget_table": True}
        )
        return None if row is None else LiteLLM_ProjectTable.model_validate(row.model_dump())

    async def projects(self, team_ids: frozenset[str] | None) -> tuple[LiteLLM_ProjectTable, ...]:
        if team_ids is not None and not team_ids:
            return ()
        rows: Final = await ProjectRepository(self._prisma_client).table.find_many(
            where=None if team_ids is None else {"team_id": {"in": sorted(team_ids)}},
            include={"litellm_budget_table": True},
            order={"created_at": "desc"},
        )
        return tuple(LiteLLM_ProjectTable.model_validate(row.model_dump()) for row in rows)

    async def budget_exists(self, budget_id: str) -> bool:
        return await BudgetRepository(self._prisma_client).table.find_unique(where={"budget_id": budget_id}) is not None

    async def create_budget(self, fields: Mapping[str, object], actor: str) -> str:
        row: Final = await BudgetRepository(self._prisma_client).table.create(
            data={**_json_columns(fields), "created_by": actor, "updated_by": actor}
        )
        return row.budget_id

    async def update_budget(self, budget_id: str, fields: Mapping[str, object], actor: str) -> None:
        await BudgetRepository(self._prisma_client).table.update(
            where={"budget_id": budget_id}, data={**_json_columns(fields), "updated_by": actor}
        )

    async def create_project(self, fields: Mapping[str, object]) -> LiteLLM_ProjectTable:
        row: Final = await ProjectRepository(self._prisma_client).table.create(
            data=_json_columns(fields), include={"litellm_budget_table": True}
        )
        return LiteLLM_ProjectTable.model_validate(row.model_dump())

    async def update_project(self, project_id: str, fields: Mapping[str, object]) -> LiteLLM_ProjectTable:
        row: Final = await ProjectRepository(self._prisma_client).table.update(
            where={"project_id": project_id}, data=_json_columns(fields), include={"litellm_budget_table": True}
        )
        if row is None:
            raise _not_found("Project", project_id)
        return LiteLLM_ProjectTable.model_validate(row.model_dump())

    async def attached_key_count(self, project_ids: tuple[str, ...]) -> int:
        return await VerificationTokenRepository(self._prisma_client).table.count(
            where={"project_id": {"in": list(project_ids)}}
        )

    async def delete_projects(self, project_ids: tuple[str, ...]) -> None:
        await ProjectRepository(self._prisma_client).table.delete_many(where={"project_id": {"in": list(project_ids)}})

    async def forget_cached(self, project_id: str) -> None:
        from litellm.proxy.auth.auth_checks import delete_cached_project_object

        await delete_cached_project_object(project_id, self._user_api_key_cache)


def _json_columns(fields: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {
            name: json.dumps(value) if isinstance(value, dict) else value
            for name, value in fields.items()
            if value is not None
        }
    )
