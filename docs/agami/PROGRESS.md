# Agami gateway: progress and checkpoints

Living tracker for the long-running Agami work on branch `agami_remove_enterprise`. Update it in the same commit as the work it describes, so the file and `git log` never disagree

## Requirement

The gateway is multi-tenant: each `LiteLLM_OrganizationTable` row is a separate customer. Two things have to hold

1. Tenant isolation. `PROXY_ADMIN` sees everything. An org admin (`LiteLLM_OrganizationMembership.user_role == "org_admin"`) sees only their own organizations. A plain member sees only their own data and their org read-only. Nobody ever sees another organization's keys, teams, users, organizations, spend, logs, budgets or guardrails
2. Licensing. Upstream's single `premium_user` flag is replaced by the Agami licence (an Ed25519-signed JWT read from `AGAMI_LICENSE`). Each paid capability is unlocked by its own `LicenseFeature`, checked at call time with `is_licensed(LicenseFeature.X)` so an expired licence stops working without a restart

Key code

- `litellm/proxy/auth/entitlements.py`: `LicenseFeature`, `EntitlementService`, `is_licensed`
- `litellm/proxy/utils.py`: `require_license_feature` for HTTP 403 gates
- `litellm/proxy/auth/agami_access.py` and the sibling `agami` package (`agami/auth`): tenant `Actor`, permission matrix, `authorize()`
- `tests/test_litellm/proxy/auth/license_test_helpers.py`: `licensed_entitlements(features=...)` and `install_entitlements(monkeypatch, service)` for tests

## Done

### Phase 1: licence and tenant authorization foundation

- [x] `e0c77f5` replace the LiteLLM license check with the Agami entitlement service
- [x] `fe43215` add the tenant authorization engine and LiteLLM adapter

### Phase 2: organization-owned models

- [x] `b13ec6a` org admins can create organization-owned models
- [x] `0451062` organization models route only to their own organization
- [x] `3e15503` other organizations' models are hidden from listings
- [x] `1872d97` organization models stay consistent on update
- [x] `077c9c8` `/model_group/info` resolves to the caller's organization model
- [x] `b88ad63` passthrough credential lookups are scoped to the caller's organization

### Phase 3: cross-tenant read leaks (from the Oct 1 audit)

The audit ranked 9 leaks. Six are fixed

- [x] Leak 1 and 2, `8ff4d67`: `/spend/tags` and `/global/activity/cache_hits` now require proxy admin view
- [x] Leak 3, `07728f3`: `/user/filter/ui` always scopes to the caller's organizations, and the `scope_user_search_to_org` setting is gone
- [x] Leak 4 and 5, `d1ac512`: an `organization_id` the caller administers no longer opens every admin viewer route, and spend logs, global activity and tag views scope every non-admin caller
- [x] Leak 7, `c2792ff`: `/team/available` and the available-team self-join stay inside the caller's organizations

### Phase 4: per-feature licence gates

- [x] `b5e5a40` secret managers -> `secret_managers`
- [x] `70b1eed` Azure Blob, GCS bucket and GCS Pub/Sub logging -> `logging_integrations`
- [x] `3aaf92b` audit logs -> `audit_logs`, email branding -> `email_branding`, fine-tuning endpoints -> `fine_tuning`
- [x] `528b7df` global and scoped spend reports -> `spend_reports`
- [x] `7f3db22` test fix: the team roster audit tests now grant `audit_logs`
- [x] `3d662e4` every `/organization` route -> `organizations`
- [x] `080d70e` test fix: config and settings audit tests in `test_proxy_server.py` and `test_proxy_setting_endpoints.py` now grant `audit_logs`

## Remaining work

### Checkpoint A: cross-tenant leaks still open

- [x] Leak 6 (steps A1 to A4 below all done Oct 6), `proxy_admin_viewer` is a global super-reader. Decided on Oct 4: the viewer becomes per-organization and sees only the organizations it belongs to. Size: `_user_has_admin_view` / `user_api_key_has_admin_view` have about 80 call sites in 30 source files, about 40 more sites compare against `PROXY_ADMIN_VIEW_ONLY` directly, and about 250 test references pin the old behaviour. Plan
  - [x] A1, close the leak centrally. Done Oct 4 (see the A1 commit on this branch). `user_api_key_has_admin_view`, and so `_user_has_admin_view`, returns True only for `PROXY_ADMIN`. `_check_proxy_admin_viewer_access` returns a bool: it still refuses LLM routes and known writes, and otherwise sends the viewer through the `internal_user_viewer` route checks. The `/user/info` carve-out and the `admin_viewer_routes` list are gone. The tests that pinned global viewer reads now assert the scoped or refused result instead: the viewer is refused proxy-wide config, budgets, prompts, coordination Redis and cost-map source, and it sees only its own keys, memories, workflow runs, managed resources, guardrails and MCP servers. Two `test_user_api_key_auth.py` tests (`test_post_custom_auth_expired_key_returns_unauthorized`, `test_real_jwt_still_requires_license_when_jwt_auth_enabled`) also fail on the base commit, so they are environment failures. Right now a viewer sees only its own data, which is safe but less useful until A2 lands
  - [x] A2, restore org-wide reads. Done Oct 6. `org_wide_read_org_ids(user_role, memberships)` in `common_utils.py` returns the orgs a caller may read in full: the orgs it administers, plus every org it belongs to when its role is `proxy_admin_viewer`. It backs `/team/list`, `/v2/team/list` (`_get_org_wide_read_org_ids`, renamed from `_get_org_admin_org_ids`), `/user/list`, `/organization/daily/activity`, and `/organization/info` (GET and the deprecated POST) through `_verify_org_access(access="read")`. Org writes keep `access="write"`, which ignores the viewer role. `/team/info` uses the new `_can_read_team_org_wide`, and its global `PROXY_ADMIN_VIEW_ONLY` bypass in `validate_membership` is gone. GET `/organization/info` is opened to the viewer through `LiteLLMRoutes.org_member_viewer_routes`. `/organization/list` and the UI user search already scoped to memberships, so they needed no change. Org-scoped spend views (`/team/daily/activity`, `/user/daily/activity`, spend reports) are not wired yet and move to A3
  - [x] A3a, spend views. Done Oct 6 in `3001e86`. `_is_admin_view_safe` now delegates to `user_api_key_has_admin_view`, so the viewer lost its global read on every spend log, spend tag, spend report and `management_v1/spend_logs` path that used it, and the key, team and user spend reports clamp the viewer to its own identity. `/team/daily/activity` (paginated, aggregated and per-user) adds the teams of every org from `org_wide_read_org_ids` to the visible set and gives those teams the unfiltered view. `/user/daily/activity` and its aggregated twin go through `_resolve_user_daily_activity_entity`, which lets a caller read another user only when that user belongs to an org the caller may read in full. `/organization/spend/report` uses `_verify_org_access(access="read")` and joins `org_member_viewer_routes`
  - [x] A3b, direct role checks. Done Oct 6 in `72a83d1`. Every direct `PROXY_ADMIN_VIEW_ONLY` comparison that granted a global read now checks for `PROXY_ADMIN` only: `/customer/list` and customer daily activity, `/key/list` (the viewer is scoped to itself), key aliases, `/key/info`, the `/v2/team/list` fallback (`allowed_route_check_inside_route`), BYOK model search and `teamId` queries, the model list, health routing fields and the webhook and New Relic test alerts, gateway request counts, tool spend, auto-router benchmarks and shadow evals (`_require_admin_viewer` merged into `_require_proxy_admin`), MCP gateway sessions, submissions and per-user credentials, search tool listing, MCP sampling without a real credential, and the agents and vector stores feature toggle. Kept: the checks that stop a non-admin from creating or re-roling an admin user, the model write block, SSO role ranking, UI login access and type literals. The role's UI label is now "Organization Viewer (View Only)". `auth/roles.py` still maps the viewer to `PLATFORM_VIEWER`, but nothing reads `Principal.roles` for authorization
  - [x] A4, proxy-wide config reads. Done Oct 6. After A1 and A3b the central route check already refuses every config read (callbacks, config overrides, coordination Redis, cost map source and reload status, model, alerting, router, cache, budget, SSO, internal user and default team settings, `/settings`, `/debug/asyncio-tasks`) to internal users, internal viewers, `proxy_admin_viewer` and org admins, so no code change was needed. `test_proxy_wide_config_reads_are_refused_to_every_non_admin_role` in `test_route_checks.py` pins that. The only config-style reads non-admins still reach are `/get/ui_settings` and `/sso/get/ui_settings` (UI flags every logged-in user needs), the public model cost map, and `/config/yaml`, which is a stub returning `{"hello": "world"}`. `/sso/get/ui_settings` returns the proxy-wide spend log row count, which is a minor cross-tenant number and left as is
- [x] Leak 8, `/v2/guardrails/list` showed every guardrail with no team to every organization (params are masked). Done Oct 6. `LiteLLM_GuardrailsTable` has a nullable, indexed `organization_id` (migration `20261006000000_add_guardrail_organization_id`, schema only). Proxy admins set it with `organization_id` on the `POST /guardrails` and `PUT /guardrails/{id}` bodies (left out keeps the owner, `null` makes it proxy-wide again). One rule, `_guardrail_visible_to` in `guardrail_endpoints.py`, covers `/v2/guardrails/list` and the config-file `/guardrails/list`: a team guardrail is visible to that team only, an org guardrail to every member of the org, and a guardrail with neither is proxy-wide and only proxy admins list it. That last part is a behaviour change, since non-admins used to see team-less config and DB guardrails. A team key without a user sees its own team's guardrails. Follow-up: the list and info responses do not return `organization_id` yet, so the Admin UI cannot show or edit the owner
- [x] Leak 9, inside one team (not cross-tenant): plain members saw every teammate's key and token hash through `/team/info`, `/team/list` and `/key/info`. Done Oct 6. One rule, `can_see_every_team_key` in `common_utils.py`: team admins and members the team explicitly granted `/key/list` (the same opt-in `/key/list` already honours) see every key in the team. `/team/info` and `/team/list` also show every key to proxy admins and org-wide readers of the team's org (org admins and `proxy_admin_viewer`). Everyone else gets only their own keys, and a team key with no user gets only itself. The owner filter runs in the database query, so `key_limit` caps the caller's own keys. `/key/info` uses the same rule in place of `user_belongs_to_keys_team`, which is deleted. The dead `token` pop and the dead spend fallback in `team_info` are gone. Hashed tokens cannot be used to log in (non `sk-` keys are rejected), so this was visibility only. `tests/proxy_behavior` `test_key_info.py` now expects 403 for plain teammates
- [ ] Gap, not a leak: org admins cannot see org-wide keys unless they are a team member. Users (A2) and team activity (A3a) are covered now. Reuse `_get_org_admin_org_ids` in `team_endpoints.py`. Do not reuse `_user_has_admin_privileges`, which is not org-scoped

### Checkpoint B: licence gates that map to existing features

One commit per feature. Each needs a test that a licence holding only some other feature is refused and a licence holding only this feature is allowed

- [ ] `sso`: `ui_sso.py` `_raise_if_sso_exceeds_free_user_limit` (takes a `premium_user` parameter, drop it like the email branding change did) and the SSO env check around `MICROSOFT_CLIENT_ID` / `GOOGLE_CLIENT_ID` / `GENERIC_CLIENT_ID`
- [ ] `guardrails`: `custom_guardrail.py` `_validate_premium_user`
- [ ] `budgets`: tag budgets in `router_strategy/budget_limiter.py` `_init_tag_budgets`, team `model_max_budget` in `management_endpoints/common_utils.py` `validate_team_model_max_budget` (takes a `premium_user` parameter), key `model_max_budget` in `key_management_endpoints.py`
- [ ] `access_control`: `allowed_routes` and `public_routes` in `auth/auth_utils.py`, `admin_only_routes` in `auth/route_checks.py` `custom_admin_only_route_check`
- [ ] Cleanup: delete the commented-out `_check_if_using_premium_email_feature` call in `litellm/integrations/email_alerting.py`

### Checkpoint C: licence gates that need new features

Each needs a `LicenseFeature` member added and a name agreed (see Open decisions)

- [ ] JWT and OAuth2 auth: `user_api_key_auth.py` (two checks), `auth/oauth2_check.py`, MCP `bridge_token_flow.py` and `idp_token_exchange.py`
- [ ] Request, response and upload size limits: `auth_utils.py` `max_request_size_mb` and `max_response_size_mb`, `RequestSizeLimitMiddleware` in `proxy_server.py`, `max_file_size_mb` in `common_utils/http_parsing_utils.py`
- [ ] Enforced params: `proxy_server.py` config load and `litellm_pre_call_utils.py`
- [ ] Team admin roles: `team_endpoints.py` "Assigning team admins is a premium feature" (two checks)
- [ ] Key features: key tags, wildcard model access groups, `get_spend_routes` permission, key regeneration (all in `key_management_endpoints.py`)
- [ ] Team-scoped models and team metadata: `model_management_endpoints.py`, `management_helpers/team_metadata_validation.py`
- [ ] Auto-router permissions: `management_helpers/auto_router_permissions.py` (the licence already has an `auto_router` feature string, so this may only need a `LicenseFeature` member)
- [ ] Worker registry and model audit fields (`created_at` / `created_by` on model info): `proxy_server.py`

### Checkpoint D: remove `premium_user`

- [ ] When no gate reads it, delete the `premium_user` global in `proxy_server.py`, `_premium_user_check` in `proxy/utils.py`, and the `premium_user` monkeypatches in tests. Check `rg "premium_user" litellm` returns only `CommonProxyErrors.not_premium_user` (the user-facing message) before closing

### Checkpoint E: ship

- [ ] Run `make check` and fix anything new. Do not edit the lint budget files
- [ ] Proof of fix against a live proxy on `localhost:4000`: one request per gated feature with a licence that lacks it (403) and one that has it (200), shown as curl commands and output
- [ ] Open the PR against the default branch from `python3 scripts/default_branch.py --branch`, following `.github/pull_request_template.md`

## Open decisions

- Decided Oct 4: `proxy_admin_viewer` is per-organization (leak 6 plan above). This matches how `agami/adapters/litellm_compat.py` already maps it to an org-scoped `VIEWER`
- Decided Oct 6: leak 8 adds a nullable `organization_id` column to `LiteLLM_GuardrailsTable` (schema-only migration, no row rewrites) so organizations can own guardrails. Team-less, org-less guardrails stay proxy-admin only
- Decided Oct 6: leak 9, plain team members see only their own keys. Team admins see every key in the team
- Decided Oct 6: A4, proxy-wide config reads are proxy-admin only
- Decided Oct 6: Checkpoint C uses one `jwt_auth` feature for JWT, OAuth2 and the MCP token flows, and grouped features for the rest: `request_limits`, `enforced_params`, `team_admin_roles`, `advanced_keys`, `team_models`, `auto_router`, `model_audit`

## Inputs for Checkpoint E

All ready as of Oct 6. Test licences are issued locally with `scripts/agami_license.py issue --private-key ~/.agami/license_signing_key.pem ...`, and that key matches the bundled public key. The provider key in `.env` is a Groq key (`groq/` models, read from `GROQ_API_KEY`). `DATABASE_URL` in `.env` points at a local PostgreSQL 18 database that the proxy migrates on first boot. Never commit or print any of these

## Local test setup (Windows)

The editable install builds the Rust bridge through maturin, which fails here because the MSVC linker (`link.exe`) is missing. Install dependencies only and run from source

```powershell
uv sync --no-install-project --extra proxy --extra extra_proxy
uv pip install --python .venv\Scripts\python.exe pytest==9.0.3 pytest-asyncio==1.3.0 pytest-mock==3.15.1 respx
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"; prisma generate --schema litellm/proxy/schema.prisma
$env:PYTHONPATH = "."; .venv\Scripts\python.exe -m pytest <paths> -q -p no:cacheprovider
```

These fail on the base commit too, so they are environment issues, not regressions

- `tests/test_litellm/proxy/spend_tracking/test_spend_event_producer.py` needs `uvloop`, which has no Windows build
- `test_spend_management_endpoints.py::TestSpendLogsPayload` (2 tests) and `test_budget_reservation.py::test_tiktoken_o200k_models_are_counted_by_rust`
- `tests/test_litellm/integrations/open_telemetry/test_otel_admin_endpoints.py` failure-span tests
- `test_proxy_server.py::test_get_config_from_file`, `::test_get_image_non_root_fallback_to_default_logo`, `::test_model_info_v1_oci_secrets_not_leaked`, and `test_auth_checks.py::test_model_has_no_cost_mapping_non_token_priced_model_is_false[vertex_ai/imagen-3.0-generate-001]` (Windows paths and local model map)
- `tests/test_litellm/proxy/hooks/test_prompt_cache_observer.py`, `test_batch_rate_limiter.py::test_cumulative_batch_tokens_over_tpd_returns_429_with_remaining_daily_window`, `test_proxy_track_cost_callback.py::test_async_log_success_event_hands_the_sidecar_a_compact_event_and_skips_the_pipeline`, `test_key_management_endpoints.py::test_ghsa_q775_ui_session_token_team_key_exempt_from_budget_ceiling`

When a gate moves off `premium_user`, search the whole `tests/` tree for helpers that set `premium_user` to switch that feature on. `3aaf92b` missed `_wire_audit_log_callback` in `test_team_endpoints.py`, which `7f3db22` fixed

## Next step

The org-wide keys gap (org admins reading keys of every team in their org), then Checkpoint B. Checkpoint B is small and mechanical and can run alongside or right after it

## Session log

Oct 4 end of day: branch `agami_remove_enterprise` is clean and pushed at `7272e80` (A1). To resume, pull the branch, rerun the local test setup above if the venv is gone, then start A2 by reading `_get_org_admin_org_ids` in `team_endpoints.py` and the `/team/list` and `/user/list` handlers that already use it. Write the A2 tests first: a viewer in org A reads org A's teams, users and spend, and gets nothing from org B

Oct 6: A2 landed (see its commit). The `tests/proxy_behavior` suite needs a live database and errors locally, so it is not part of the local check

Oct 6, later: A3a landed in `3001e86`. `TestSpendLogsPayload::test_spend_logs_payload_success_log_with_api_base` and `::test_spend_logs_payload_success_log_with_router` fail locally because the local model map cannot resolve the provider for `claude-4-sonnet-20250514`. That is unrelated to auth

Oct 6, A3b: running all of `tests/test_litellm/proxy` in one go dies at about 13% on this machine with no summary, so run it per directory. Now that `.env` has `DATABASE_URL`, tests that boot the proxy reach the empty local database, which breaks `test_cors_exposes_cache_key_header_to_browser_js`. Set `$env:DATABASE_URL=""` in the test shell to get the old behaviour. Also failing on the base commit and so not ours: `test_health_backlog_includes_admission_control_stats`, `test_semantic_tool_filter.py` (no `semantic_router` module), `test_key_rotation_e2e.py::test_deprecated_key_grace_period_cache_hit_path` (Windows path in a regex), `test_budget_reservation.py::test_tiktoken_o200k_models_are_counted_by_rust` (no Rust bridge), `test_discoverable_endpoints.py::test_static_root_path_authorization_discovery_preserves_issuer`, and `test_team_default_params.py::test_nonexistent_default_organization_returns_400`, which only fails under `-n 8`
