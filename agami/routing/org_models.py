from collections.abc import Mapping
from typing import Final, cast

ORG_MODEL_NAME_PREFIX: Final = "agami-org"


def org_model_name(organization_id: str, public_model_name: str) -> str:
    """Every deployment of one organization's public model shares this name, so the router load-balances them
    together and never with another organization's model of the same public name."""
    return f"{ORG_MODEL_NAME_PREFIX}/{organization_id}/{public_model_name}"


def deployment_organization_id(deployment: Mapping[str, object]) -> str | None:
    model_info: Final = deployment.get("model_info")
    if not isinstance(model_info, Mapping):
        return None
    organization_id: Final = cast("Mapping[str, object]", model_info).get(  # cast-ok: model_info is a JSON object
        "organization_id"
    )
    return organization_id if isinstance(organization_id, str) and organization_id else None


def deployment_usable_by(
    deployment: Mapping[str, object], request_organization_id: str | None, is_super_admin: bool
) -> bool:
    owner: Final = deployment_organization_id(deployment)
    return owner is None or is_super_admin or owner == request_organization_id
