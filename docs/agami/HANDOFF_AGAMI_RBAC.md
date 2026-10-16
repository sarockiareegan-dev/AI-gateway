# Handoff: complete Agami RBAC, then remove LiteLLM RBAC

Paste the prompt at the bottom into Antigravity (or Cursor). Read `docs/agami/PROGRESS.md` and `docs/agami/WORK_REPORT.md` first; they are the source of truth.

## Repo facts

- Repo: `sarockiareegan-dev/AI-gateway`, base branch `dev`, PRs squash-merged
- Company/product: Iagami
- Worktree used for recent work: `D:\personal\AI-gateway\wt_roles` (main checkout may be on another branch with local WIP; do not disturb it)
- Update `PROGRESS.md` and `WORK_REPORT.md` in the same PR as the code
- Conventional commits, no AI attribution, no `/` in branch names
- Windows PowerShell; use `required_permissions: ["all"]` when needed
- Tests: `$env:LITELLM_MODE='PRODUCTION'; $env:PYTHONPATH='.'; $env:DATABASE_URL=''; <venv> -m pytest -p no:cacheprovider -q -W ignore --timeout 300 <paths>`
- Never print or commit secrets from `.env`

## Already shipped (do not redo)

| Item | Merge / note |
|---|---|
| Tenant engine | `agami/auth/roles.py`, `service.py`, `context.py`, `permissions.py` |
| LiteLLM → Actor | `agami/adapters/litellm_compat.py` (`admin`→`team_admin`, `org_admin`→`org_admin`, view-only→`viewer`) |
| Request actor helper | `litellm/proxy/auth/agami_access.py` (`load_request_actor`) |
| Role picker text only | PR #16 → `b3d8c17` (LiteLLM descriptions/labels; org_admin still not in global picker) |
| Virtual key requests | PR #17 → `2cc9f72` (`/key_request/*`, Key Requests tab, team/org/proxy can decide) |
| Partial invite helper | `admin_can_invite_user` in `common_utils.py` (existing-user invitations; not full `/user/new` Agami scope) |

## Product requirements (user, Oct 16)

Hierarchy high → low: **proxy admin → org admin → team admin → member**

Invite User (all admins, scoped):

- Proxy admin: any org, any team
- Org admin: any team within their org
- Team admin: their team only

Every admin has a **view-only** twin: same scope, cannot edit, change, invite, or approve. Member is last: personal resources only; can **request** a team virtual key; **team admin** approves; member uses the key.

After Agami RBAC is complete and enforced live: **remove LiteLLM RBAC** from Invite User and from live authorization checks that duplicate tenant scope. Keep storage/compat mapping only while DB still uses LiteLLM membership fields (`org_admin`, team `admin`/`user`, `LitellmUserRoles` on the user row).

## Gap vs today

- Invite User UI still lists LiteLLM globals: `proxy_admin`, `proxy_admin_viewer`, `internal_user`, `internal_user_viewer`
- Org Admin / Team Admin are set under Organization → Members and Team → Members (`admin`), not in that picker
- Most routes still authorize with `LitellmUserRoles`, not `agami.auth.service.authorize`
- Key requests already match member → admin approve; tighten later if product wants team-admin-only (today org/proxy can also decide)

## Implementation order

1. **Contract** — keep `PROGRESS` / this handoff accurate (done in the docs PR)
2. **Invite API** — pure Agami invite-scope module (e.g. `agami/auth/invite.py`); wire `/user/new` and bulk create; refuse out-of-scope org/team and forbidden role assignments; tests for proxy/org/team admin and view-only cannot invite
3. **Invite UI** — scoped org/team pickers; role choices that match Agami hierarchy; optional `GET` invite-scope endpoint for the UI
4. **View-only twins** — enforce read-only under each admin scope (proxy/org/team viewers)
5. **Align** — member personal scope + key requests vs hierarchy; sweep leftover LiteLLM checks
6. **Remove LiteLLM RBAC** — Invite User and live auth no longer driven by the old global role model; docs tick both Agami RBAC and remove-LiteLLM items

Ship **2 → 3** as the first code PR if possible.

## Key files

- `agami/auth/roles.py`, `service.py`, `context.py`, `permissions.py`
- `agami/adapters/litellm_compat.py`
- `litellm/proxy/auth/agami_access.py`
- `litellm/proxy/management_endpoints/internal_user_endpoints.py` (`/user/new`, `/user/available_roles`)
- `litellm/proxy/management_endpoints/common_utils.py` (`admin_can_invite_user`, …)
- `litellm/proxy/management_endpoints/team_endpoints.py`, `organization_endpoints.py`
- `ui/litellm-dashboard/src/components/CreateUserButton.tsx`
- `ui/litellm-dashboard/src/app/(dashboard)/users/_components/view_users.tsx`
- `docs/agami/PROGRESS.md`, `docs/agami/WORK_REPORT.md`

## Local run note (Windows)

Recent local test used worktree on `origin/dev` with `agami_demo_config.yaml` on port **4000**. An older process on **15866** still served pre-#16 role text; use `http://localhost:4000/ui/` for current `dev`.

---

## Prompt for Antigravity

```
You are continuing Iagami (AI-gateway) work on GitHub sarockiareegan-dev/AI-gateway, base branch dev (squash-merge PRs).

Read first and follow:
- docs/agami/PROGRESS.md (especially "Agami RBAC (next)", Remaining work, Next step)
- docs/agami/WORK_REPORT.md (sections 9.11–9.13, section 16)
- docs/agami/HANDOFF_AGAMI_RBAC.md (this handoff)

Goal: complete Agami RBAC, then remove LiteLLM RBAC.

Product rules:
1. Hierarchy: proxy admin > org admin > team admin > member
2. Invite User for all admins, scoped: proxy any org/team; org admin teams in their org; team admin own team only
3. Every admin has a view-only twin (same scope, no edits/invites/approvals)
4. Member: personal resources only; request team virtual key; team admin approves; member uses key
5. When Agami RBAC is fully wired and tested, remove LiteLLM RBAC from Invite User UI and live authorization paths that duplicate tenant scope. Keep DB/compat mapping only while storage still uses LiteLLM membership fields

Already done (do not redo): agami/auth matrix + litellm_compat Actor mapping; PR #16 role label text only; PR #17 virtual key requests (2cc9f72). Gap: Invite User and most routes still use LitellmUserRoles.

Implement in this order:
1. Invite authorization API for /user/new (and bulk) using Agami Actor/authorize or a pure invite-scope module; tests for the matrix
2. Invite User UI with scoped org/team pickers and Agami-aligned role choices
3. View-only enforcement under each admin scope
4. Align member + key requests; sweep leftover LiteLLM checks
5. Remove LiteLLM RBAC surface from invite + live auth; tick PROGRESS and update WORK_REPORT in the same PR(s)

Conventions: conventional commits; no AI attribution; no "/" in branch names; update PROGRESS.md and WORK_REPORT.md in the same PR; Windows PowerShell; LITELLM_MODE=PRODUCTION and empty DATABASE_URL for pytest; never print .env secrets; do not disturb unrelated local WIP on other branches (prefer a clean worktree from origin/dev).

Act decisively. Prefer small reviewable PRs (invite API+UI first). Report PR URLs when done.
```
