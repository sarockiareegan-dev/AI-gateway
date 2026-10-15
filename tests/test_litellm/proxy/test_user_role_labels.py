import pytest

from litellm.proxy._types import LitellmUserRoles


@pytest.mark.parametrize(
    "role",
    [
        LitellmUserRoles.PROXY_ADMIN,
        LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY,
        LitellmUserRoles.ORG_ADMIN,
        LitellmUserRoles.INTERNAL_USER,
        LitellmUserRoles.INTERNAL_USER_VIEW_ONLY,
    ],
)
def test_ui_roles_have_unbranded_label_and_description(role: LitellmUserRoles) -> None:
    assert role.ui_label
    assert role.description
    assert "litellm" not in f"{role.ui_label} {role.description}".lower()
