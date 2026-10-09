import pytest

from .actors import Actor

pytestmark = pytest.mark.asyncio(loop_scope="session")


# (id, actor, target_actor, expected_status). Targets are 3 fixed seeded keys
# representing the canonical relations: own, OWNER (same org_a/team_alpha),
# and CROSS_ORG_USER (org_b/team_beta).
#
# Notable pinned behaviors (intentionally surfaced, not endorsed):
#   - Org admins read every key in their own org and none outside it.
#   - Plain same-team members (internal_user, unrelated_same_org,
#     service_account) do NOT see each other's keys. Only the team admin does.
_SCENARIOS = [
    ("own/proxy_admin", Actor.PROXY_ADMIN, Actor.PROXY_ADMIN, 200),
    ("own/org_admin", Actor.ORG_ADMIN, Actor.ORG_ADMIN, 200),
    ("own/team_admin", Actor.TEAM_ADMIN, Actor.TEAM_ADMIN, 200),
    ("own/internal_user", Actor.INTERNAL_USER, Actor.INTERNAL_USER, 200),
    ("own/owner", Actor.OWNER, Actor.OWNER, 200),
    ("own/unrelated_same_org", Actor.UNRELATED_SAME_ORG, Actor.UNRELATED_SAME_ORG, 200),
    ("own/cross_org_user", Actor.CROSS_ORG_USER, Actor.CROSS_ORG_USER, 200),
    ("own/service_account", Actor.SERVICE_ACCOUNT, Actor.SERVICE_ACCOUNT, 200),
    ("owner_key/proxy_admin", Actor.PROXY_ADMIN, Actor.OWNER, 200),
    ("owner_key/org_admin", Actor.ORG_ADMIN, Actor.OWNER, 200),
    ("owner_key/org_b_admin", Actor.ORG_B_ADMIN, Actor.OWNER, 403),
    ("owner_key/team_admin", Actor.TEAM_ADMIN, Actor.OWNER, 200),
    ("owner_key/internal_user", Actor.INTERNAL_USER, Actor.OWNER, 403),
    ("owner_key/owner", Actor.OWNER, Actor.OWNER, 200),
    ("owner_key/unrelated_same_org", Actor.UNRELATED_SAME_ORG, Actor.OWNER, 403),
    ("owner_key/cross_org_user", Actor.CROSS_ORG_USER, Actor.OWNER, 403),
    ("owner_key/service_account", Actor.SERVICE_ACCOUNT, Actor.OWNER, 403),
    ("cross_org/proxy_admin", Actor.PROXY_ADMIN, Actor.CROSS_ORG_USER, 200),
    ("cross_org/org_admin", Actor.ORG_ADMIN, Actor.CROSS_ORG_USER, 403),
    ("cross_org/org_b_admin", Actor.ORG_B_ADMIN, Actor.CROSS_ORG_USER, 200),
    ("cross_org/team_admin", Actor.TEAM_ADMIN, Actor.CROSS_ORG_USER, 403),
    ("cross_org/internal_user", Actor.INTERNAL_USER, Actor.CROSS_ORG_USER, 403),
    ("cross_org/owner", Actor.OWNER, Actor.CROSS_ORG_USER, 403),
    (
        "cross_org/unrelated_same_org",
        Actor.UNRELATED_SAME_ORG,
        Actor.CROSS_ORG_USER,
        403,
    ),
    ("cross_org/cross_org_user", Actor.CROSS_ORG_USER, Actor.CROSS_ORG_USER, 200),
    ("cross_org/service_account", Actor.SERVICE_ACCOUNT, Actor.CROSS_ORG_USER, 403),
]


@pytest.mark.parametrize(
    "actor,target_actor,expected_status",
    [(a, t, s) for (_id, a, t, s) in _SCENARIOS],
    ids=[s[0] for s in _SCENARIOS],
)
async def test_key_info_authz_matrix(
    actor: Actor, target_actor: Actor, expected_status: int, proxy_client, world
):
    caller = world.keys[actor]
    target = world.keys[target_actor]

    resp = await proxy_client.get(
        f"/key/info?key={target.cleartext}",
        headers={"Authorization": f"Bearer {caller.cleartext}"},
    )
    assert (
        resp.status_code == expected_status
    ), f"{actor.value} → {target_actor.value}: {resp.status_code} {resp.text}"

    if expected_status == 200:
        body = resp.json()
        # The handler echoes back whatever ?key was passed (cleartext here),
        # so accept either form — info.user_id is the canonical identity check.
        assert body.get("key") in (target.cleartext, target.hashed)
        assert body["info"].get("user_id") == target.user_id
