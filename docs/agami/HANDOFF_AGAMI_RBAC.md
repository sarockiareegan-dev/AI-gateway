# Handoff: Agami RBAC (post LiteLLM removal)

Paste context for the next agent. Read `docs/agami/PROGRESS.md` and `docs/agami/WORK_REPORT.md` first.

## Status (Oct 27)

Agami hierarchy is enforced for invite + org/team management. LiteLLM RBAC is removed from Invite User and from org/team mutation gates (compat mapping remains for DB fields).

## Product rules

| Role | Scope | Hard denials |
|------|-------|----------------|
| Proxy admin | Everything | Not listed as org/team member; never auto-added |
| Org admin | Their org(s) | No org create / delete / rename |
| Team admin | Their team(s) | No team create / delete / rename |
| Member | Personal + assigned | No invite |
| Viewer | Same scope read-only | No edit / invite / approve |

Invites: org **then** team; team must belong to selected org; non–proxy invites need both.

## Key modules

- `agami/auth/roles.py`, `invite.py`, `service.py`
- `litellm/proxy/auth/agami_tenant.py`, `agami_access.py`
- `ui/.../CreateUserButton.tsx`, `MemberTable.tsx`

## Still optional

- Route remaining non-tenant `user_role == PROXY_ADMIN` checks through `actor.is_super_admin`
- DB schema migration away from LiteLLM membership role strings
- Rebuild Admin UI bundle into `litellm/proxy/_experimental/out` after UI merges
