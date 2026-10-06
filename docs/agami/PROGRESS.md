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

- [ ] Leak 6, `proxy_admin_viewer` is a global super-reader. Decided on Oct 4: the viewer becomes per-organization and sees only the organizations it belongs to. Size: `_user_has_admin_view` / `user_api_key_has_admin_view` have about 80 call sites in 30 source files, about 40 more sites compare against `PROXY_ADMIN_VIEW_ONLY` directly, and about 250 test references pin the old behaviour. Plan
  - [x] A1, close the leak centrally. Done Oct 4 (see the A1 commit on this branch). `user_api_key_has_admin_view`, and so `_user_has_admin_view`, returns True only for `PROXY_ADMIN`. `_check_proxy_admin_viewer_access` returns a bool: it still refuses LLM routes and known writes, and otherwise sends the viewer through the `internal_user_viewer` route checks. The `/user/info` carve-out and the `admin_viewer_routes` list are gone. The tests that pinned global viewer reads now assert the scoped or refused result instead: the viewer is refused proxy-wide config, budgets, prompts, coordination Redis and cost-map source, and it sees only its own keys, memories, workflow runs, managed resources, guardrails and MCP servers. Two `test_user_api_key_auth.py` tests (`test_post_custom_auth_expired_key_returns_unauthorized`, `test_real_jwt_still_requires_license_when_jwt_auth_enabled`) also fail on the base commit, so they are environment failures. Right now a viewer sees only its own data, which is safe but less useful until A2 lands
  - [x] A2, restore org-wide reads. Done Oct 6. `org_wide_read_org_ids(user_role, memberships)` in `common_utils.py` returns the orgs a caller may read in full: the orgs it administers, plus every org it belongs to when its role is `proxy_admin_viewer`. It backs `/team/list`, `/v2/team/list` (`_get_org_wide_read_org_ids`, renamed from `_get_org_admin_org_ids`), `/user/list`, `/organization/daily/activity`, and `/organization/info` (GET and the deprecated POST) through `_verify_org_access(access="read")`. Org writes keep `access="write"`, which ignores the viewer role. `/team/info` uses the new `_can_read_team_org_wide`, and its global `PROXY_ADMIN_VIEW_ONLY` bypass in `validate_membership` is gone. GET `/organization/info` is opened to the viewer through `LiteLLMRoutes.org_member_viewer_routes`. `/organization/list` and the UI user search already scoped to memberships, so they needed no change. Org-scoped spend views (`/team/daily/activity`, `/user/daily/activity`, spend reports) are not wired yet and move to A3
  - [ ] A3, wire the org-scoped spend views (`/team/daily/activity`, `/user/daily/activity`, the `*/spend/report` routes) to `org_wide_read_org_ids`, then review the direct `PROXY_ADMIN_VIEW_ONLY` comparisons one by one (`key_management_endpoints.py`, `internal_user_endpoints.py`, `customer_endpoints.py`, `auth_checks.py`, `spend_management_endpoints.py` `_is_admin_view_safe`, the MCP, prompt, agent, workflow and guardrail endpoints). Keep the ones that block viewer writes, remove the ones that grant global reads
  - [ ] A4, proxy-wide config reads (callbacks, config overrides, health, model cost map, coordination Redis) are not tenant data but do expose operator settings. Default: admin only, unless a customer-facing UI page needs them
- [ ] Leak 8, `/v2/guardrails/list` shows every guardrail with no team to every organization (params are masked). `LiteLLM_GuardrailsTable` has no `organization_id`, so this needs either a schema column (schema-only migration, no row rewrites) or a rule that team-less DB guardrails are admin-only
- [ ] Leak 9, inside one team (not cross-tenant): plain members see every teammate's key and token hash through `/team/info`, `/team/list` and `/key/info`. The `token` pop in `team_info` only touches a copy
- [ ] Gap, not a leak: org admins cannot see org-wide keys, users or team activity unless they are a team member. Reuse `_get_org_admin_org_ids` in `team_endpoints.py`. Do not reuse `_user_has_admin_privileges`, which is not org-scoped

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
- Names and grouping for the Checkpoint C features, for example one `jwt_auth` feature or separate `jwt_auth` and `oauth2_auth`

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

Leak 6 step A3: wire the org-scoped spend views to `org_wide_read_org_ids`, then go through the remaining direct `PROXY_ADMIN_VIEW_ONLY` comparisons and drop any that still grant a global read. Checkpoint B is small and mechanical and can run alongside or right after it

## Session log

Oct 4 end of day: branch `agami_remove_enterprise` is clean and pushed at `7272e80` (A1). To resume, pull the branch, rerun the local test setup above if the venv is gone, then start A2 by reading `_get_org_admin_org_ids` in `team_endpoints.py` and the `/team/list` and `/user/list` handlers that already use it. Write the A2 tests first: a viewer in org A reads org A's teams, users and spend, and gets nothing from org B

Oct 6: A2 landed (see its commit). The `tests/proxy_behavior` suite needs a live database and errors locally, so it is not part of the local check
