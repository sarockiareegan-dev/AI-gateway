# Agami gateway: complete work report

This document explains every piece of work done on the Agami fork of LiteLLM, in enough detail that an engineer can understand, debug, change and reproduce each part by hand, without any AI help. `PROGRESS.md` is the short tracker. This file is the long explanation

Repository: `https://github.com/sarockiareegan-dev/AI-gateway`, base branch `dev`, every PR squash-merged

| PR | Commit on `dev` | What it delivered |
|----|-----------------|-------------------|
| #1 | `a47f91e` | Agami licence, tenant RBAC engine, organization models, 9 leak fixes, per-feature licence gates, rebuilt enterprise pieces, Projects |
| #2 | `257e278` | Dependency and security bumps |
| #4 | `01617ca` | Rust extension, e2e harness and LiteLLM repo tooling removed, Textract OCR and Bedrock transcription ported to Python |
| #5 | `5deb495` | Managed files hook rebuilt |
| #6 | `2e1f70f` | This work report |
| #7 | `dcdd0e4` | Guardrail owner (`team_id`, `organization_id`) in the guardrail list and info responses, guardrail info scoped like the list |
| #8 | `8c2ebb6` | Demo config and Docker Compose override committed |
| #9 | `e70ef8d` | Guardrail owner shown in the Admin UI table and overview |
| #10 | pending | Batch cost poller rebuilt |

PR #1 was squashed, so the individual commit hashes in `PROGRESS.md` (for example `e0c77f5`) live on the old `agami_remove_enterprise` branch, not on `dev`. Use `git log -S "<symbol>" origin/dev` or `git blame` to find where a line came from

## Table of contents

1. Why this work exists
2. The Agami licence
3. Per-feature licence gates
4. The tenant authorization engine (RBAC)
5. Organization-owned models
6. Cross-tenant leak fixes
7. Rebuilt enterprise features
8. Projects
9. Managed files
10. Dependency bumps
11. Rust, e2e and tooling removal
12. Running the demo
13. Testing and lint commands
14. Debugging playbook
15. How to reproduce everything from scratch
16. What is still open
17. Decisions log
18. Bugs found and fixed along the way

---

## 1. Why this work exists

Upstream LiteLLM is open source, but its paid features ("enterprise") were:

- Locked behind one global boolean, `premium_user`, computed once at startup
- Verified by calling LiteLLM's own server `https://license.litellm.ai/verify_license/<key>` (phone-home), with a fallback to an RSA public key `public_key.pem` that LiteLLM controls
- Partly implemented in a separate `enterprise/` package with its own commercial licence

That means a product built on LiteLLM depends on LiteLLM the company: their licence keys, their server, their enterprise code. The standing goal for Agami is full control and zero dependency on LiteLLM. So the work did four things:

1. Replace LiteLLM's licence with our own licence that we sign and verify offline
2. Replace the single `premium_user` switch with one licence feature per paid capability
3. Make the gateway truly multi-tenant: one `LiteLLM_OrganizationTable` row is one customer, and no customer can see another customer's data
4. Rebuild (clean-room) the enterprise features we still need, and delete everything else that ties us to LiteLLM (Rust extension, their CI, their bots, their scanners)

---

## 2. The Agami licence

### 2.1 What it is

An Agami licence is a JWT (JSON Web Token) signed with Ed25519 (algorithm name `EdDSA`). It is a single string of three base64url parts: `header.payload.signature`

Payload (claims) example:

```json
{
  "iss": "agami",
  "sub": "example-customer",
  "jti": "6f0c1c2e-....",
  "iat": 1760400000,
  "exp": 1798761599,
  "features": ["sso", "organizations", "budgets"],
  "max_users": 500,
  "max_teams": 50
}
```

| Claim | Meaning | Required |
|-------|---------|----------|
| `iss` | Issuer, must be exactly `agami` | yes |
| `sub` | Customer name, shown in `/health/license` as `user_id` | yes |
| `jti` | Unique licence id (uuid4), lets you identify and revoke-by-list later | yes |
| `exp` | Expiry, unix seconds (end of the chosen day, UTC) | yes |
| `iat` | Issued at, informational | no |
| `features` | List of `LicenseFeature` values, or `["*"]` for everything | no (empty means nothing) |
| `max_users` | Seat limit, optional | no |
| `max_teams` | Team limit, optional | no |

### 2.2 Files

| File | Role |
|------|------|
| `litellm/proxy/auth/entitlements.py` | Verification, `LicenseFeature` enum, `EntitlementService`, `is_licensed`, `has_valid_license` |
| `litellm/proxy/auth/agami_license_public_key.pem` | Public key bundled with the code. Anyone can read it; it can only verify, never sign |
| `~/.agami/license_signing_key.pem` | Private key. Lives only on the issuer's machine, never committed. Whoever has it can mint licences |
| `scripts/agami_license.py` | CLI: `keygen` creates the key pair, `issue` signs a licence |
| `litellm/proxy/utils.py` line ~7803 | `require_license_feature(feature, name)`, raises HTTP 403 |
| `litellm/proxy/proxy_server.py` line ~865 | `_license_check = get_entitlement_service()`, the process-wide service |
| `litellm/proxy/proxy_server.py` line ~4519 | `_apply_config_licence`, reads `general_settings.agami_license` from the config file |
| `litellm/proxy/health_endpoints/_health_endpoints.py` line ~1403 | `GET /health/license` |
| `tests/test_litellm/proxy/auth/license_test_helpers.py` | Test helpers that mint licences with a throwaway key |

### 2.3 Architecture

```
  ISSUER MACHINE (offline)                          GATEWAY (customer side)
  ------------------------                          -----------------------
  ~/.agami/license_signing_key.pem  (private)       litellm/proxy/auth/agami_license_public_key.pem (public)
            |                                                     |
  scripts/agami_license.py issue  ---> JWT string --->  AGAMI_LICENSE env var  or  general_settings.agami_license
                                                                  |
                                                     EntitlementService.from_environment()
                                                                  |
                                                     verify_license(token, public_key, now)
                                                                  |
                         NoLicense | InvalidLicense(reason) | ExpiredLicense | ValidLicense(Entitlements)
                                                                  |
                                     is_licensed(LicenseFeature.X)  /  has_valid_license()
                                                                  |
                                   every gate in the proxy (about 35 files)
```

### 2.4 Verification workflow, line by line (`entitlements.py`)

1. Constants (lines 30 to 43): env var `AGAMI_LICENSE`, config key `agami_license`, issuer `agami`, algorithm `EdDSA`. `_DECODE_OPTIONS` requires `exp`, `iss`, `sub`, `jti` and switches off PyJWT's own `exp` check (`verify_exp: False`). We check expiry ourselves against an injectable clock so tests can fake time and so expiry is re-checked on every call
2. `LicenseFeature` (line 48): the list of sellable features. The value string is what goes in the `features` claim
3. `_LicenseClaims` (line 72): a frozen pydantic model. `sub` and `jti` must be non-empty, `max_users` and `max_teams` must be `>= 0`. Unknown claims are ignored (`extra="ignore"`), so adding new claims later does not break old gateways
4. `Entitlements` (line 84): the validated result. `grants(feature)` is true when the feature is listed or `*` is listed. `as_license_data()` converts to the shape the UI already expects (`expiration_date`, `user_id`, `allowed_features`, `max_users`, `max_teams`)
5. Status types (lines 108 to 128): four frozen dataclasses. Code matches on them with `match`, so a new state cannot be forgotten silently
6. `verify_license` (line 131):
   - No token means `NoLicense`
   - No public key means `InvalidLicense` (a broken build fails closed)
   - `jwt.decode` checks the signature with the public key, the algorithm allow-list `("EdDSA",)` and `iss == "agami"`. Any PyJWT error or pydantic error becomes `InvalidLicense` with the reason
   - `exp <= now` means `ExpiredLicense`
   - Otherwise `ValidLicense(Entitlements(...))`
7. `load_public_key` (line 162): loads PEM, refuses anything that is not Ed25519 (so nobody can swap in an RSA key and do algorithm-confusion tricks)
8. `EntitlementService` (line 178):
   - `from_environment()` loads the bundled public key and reads `AGAMI_LICENSE`
   - `load(token)` verifies and logs a warning for invalid or expired tokens (`Ignoring AGAMI_LICENSE: ...`). It never logs the token itself
   - `entitlements` property (line 212) returns the entitlements only if the status is valid AND `expires_at > now` at the moment of the call. This is what makes an expired licence stop working without a restart
   - `is_premium()`, `grants_feature()`, `is_over_limit(total_users)`, `is_team_count_over_limit(team_count)`, `auto_router_capability_limit()` all go through `entitlements`
9. `get_entitlement_service()` (line 244) is wrapped in `functools.cache`, so one service per process
10. `is_licensed(feature, entitlements=None)` (line 249): the one function every gate calls. The optional `entitlements` argument lets tests and the config loader pass their own service
11. `has_valid_license()` (line 253): any valid licence. Used for the UI `premium_user` token field and for Swagger branding

### 2.5 Config file licence

`_apply_config_licence` in `proxy_server.py` runs early in config loading. If `general_settings.agami_license` is a string it calls `license_check.load(token)`, which overrides the env var. Then it checks the two config settings that are licence gated at load time:

- `allowed_ips` needs `access_control`, otherwise startup fails with a `ValueError`
- `enforced_params` needs `enforced_params`, otherwise startup fails

The licence is loaded first on purpose. Upstream checked `enforced_params` before reading the licence in the same file, so a licence set only in the config was refused. `test_licence_gated_config_settings_follow_the_licence_in_the_same_config` pins the fix

### 2.6 How to control it (operator guide)

Generate a key pair once (only ever do this once; a new pair invalidates every licence already issued):

```powershell
.venv\Scripts\python.exe scripts\agami_license.py keygen `
  --private-key $HOME\.agami\license_signing_key.pem `
  --public-key litellm\proxy\auth\agami_license_public_key.pem
```

`keygen` refuses to overwrite an existing private key. Commit only the public key

Issue a licence for a customer:

```powershell
.venv\Scripts\python.exe scripts\agami_license.py issue `
  --private-key $HOME\.agami\license_signing_key.pem `
  --customer acme --expires 2027-12-31 `
  --features "sso,organizations,budgets" --max-users 500 --max-teams 50
```

- `--expires` is the last valid day; the token expires at 23:59:59 UTC that day
- `--features "*"` grants everything
- The command prints the JWT. Give it to the customer

Install it on a gateway, either:

- `.env` or the environment: `AGAMI_LICENSE=<jwt>`
- or the config file: `general_settings: { agami_license: <jwt> }`

Restart the proxy after changing the env var (the service is cached per process). Users must log out and in again for the UI, because the UI reads `premium_user` from the login session token

Check it:

```powershell
curl.exe -s http://localhost:4000/health/license -H "Authorization: Bearer $env:LITELLM_MASTER_KEY"
```

returns `has_license`, `license_type` (`enterprise` or `community`), `expiration_date`, `allowed_features` and `limits`. The token itself is never returned

To upgrade a customer, issue a new licence with more features and replace the old string. To downgrade or revoke, issue a shorter one; since there is no phone-home, an already-issued licence stays valid until its `exp`. Keep expiries short (for example one year) for that reason. If you ever need hard revocation, add a `jti` deny-list check in `verify_license`

### 2.7 How secure it is

| Property | How it is achieved |
|----------|--------------------|
| Cannot be forged | Ed25519 signature. Without the private key nobody can produce a token the public key accepts |
| No algorithm confusion | Algorithm allow-list is only `EdDSA`, and `load_public_key` rejects non-Ed25519 keys. `alg: none` and HS256-with-public-key tricks fail |
| Cannot be edited | Changing any claim (for example adding a feature or pushing `exp`) breaks the signature |
| Expiry cannot be bypassed by uptime | `entitlements` compares `expires_at` to the clock on every call |
| Works air-gapped | Verification is purely local. No network, no LiteLLM server |
| Fails closed | Missing key, wrong issuer, malformed token, wrong algorithm, expired: all mean "not licensed" |
| No secret leakage | The token is never logged; `/health/license` returns only metadata |

Limits you should know:

- The public key and the check live in the code the customer runs. A customer who edits the Python source can switch gates off. That is true for every self-hosted licence system; the licence is a contract control, not DRM
- No revocation before `exp` (see above)
- The private key is the crown jewel. Back it up offline. If it leaks, run `keygen` again (to a new path), ship the new public key in a release, and reissue every licence

### 2.8 Agami licence vs LiteLLM's licence

| Aspect | LiteLLM upstream (`litellm/proxy/auth/litellm_license.py`, deleted) | Agami (`entitlements.py`) |
|--------|----------------------------------------------------------|---------------------------|
| Env var | `LITELLM_LICENSE` | `AGAMI_LICENSE` |
| Who can issue | Only LiteLLM | Only us (we hold the private key) |
| Verification | HTTP GET to `https://license.litellm.ai/verify_license/<key>` with 3 retries, plus an RSA public-key fallback | Offline Ed25519 only |
| Network dependency | Yes, and the licence key was put in the URL and debug logs | None, token never logged |
| Granularity | One boolean `premium_user` for every paid feature (plus a few `allowed_features`) | 21 named features, each checked separately |
| When checked | Mostly once at startup into a global | Every call, so expiry works at runtime |
| Failure modes | Server down or slow could mean premium flips off | Deterministic |
| Testability | Needed patching of `premium_user` in about 180 test places | Tests mint real licences with a throwaway key through `license_test_helpers.py` |

### 2.9 Where it is used

Search: `rg "is_licensed|require_license_feature|has_valid_license" litellm`

Main sites (about 35 files): `litellm_pre_call_utils.py` (request-time guardrails, policies, callback disabling), `proxy_server.py` (config gates, UI token, Swagger branding, worker registry, model audit fields), `key_management_endpoints.py` (advanced keys, key regeneration), `fine_tuning_endpoints/endpoints.py`, `ui_sso.py` (SSO user limit, debug login, custom SSO handler), `auth_utils.py` (request size limits), `user_api_key_auth.py` (JWT auth), `spend_management_endpoints.py` (spend reports), `team_endpoints.py` (team admin roles), `audit_logs`, `common_utils.py` (premium metadata fields), the secret managers (HashiCorp, CyberArk, Google), Azure Blob, GCS bucket, GCS Pub/Sub, Slack alerting, SCIM, OAuth2, MCP token flows, organization endpoints, project endpoints, model management, the budget limiter, team metadata validation, HTTP parsing, legacy guardrail callbacks, auto-router permissions and `hooks/managed_files.py`

The UI session token field `premium_user` still exists because about 60 dashboard components read it. Its value is now `has_valid_license()` at login time (`proxy_server.py`, `ui_sso.py`, `login_utils.py`)

---

## 3. Per-feature licence gates

### 3.1 The feature list

| Feature value | What it unlocks | Main gate location |
|---------------|-----------------|--------------------|
| `sso` | More than 5 SSO users, `/sso/debug/login`, SCIM (`/scim/v2/*`), custom UI SSO handler | `ui_sso.py` `_raise_if_sso_exceeds_free_user_limit`, `require_scim_licence` |
| `organizations` | Every `/organization/*` route | `organization_endpoints.py` |
| `secret_managers` | HashiCorp, CyberArk, Google secret managers, `secret_manager_settings` metadata | secret manager modules |
| `logging_integrations` | Azure Blob, GCS bucket, GCS Pub/Sub logging, `logging` metadata, per-request callback disabling | integrations, `litellm_pre_call_utils.py` |
| `guardrails` | Per-request guardrail body params, key/team/project guardrails and policies, legacy guardrail callbacks, tag-based modes | `custom_guardrail.py`, `litellm_pre_call_utils.py`, `legacy_callbacks.py` |
| `spend_reports` | Global and scoped spend reports | `spend_management_endpoints.py` |
| `budgets` | Tag budgets, `model_max_budget` on keys, teams, users | `budget_limiter.py`, key and team validation |
| `access_control` | `public_routes`, `allowed_ips`, `allowed_passthrough_routes` | `auth_utils.py`, `_apply_config_licence` |
| `audit_logs` | Audit log routes and writes | `audit_logs` |
| `email_branding` | Custom email logo and branding | email alerting |
| `fine_tuning` | Fine-tuning endpoints | `fine_tuning_endpoints/endpoints.py` |
| `jwt_auth` | JWT auth, OAuth2 auth, MCP token exchange and bridge flows | `user_api_key_auth.py`, `oauth2_check.py`, MCP |
| `request_limits` | `max_request_size_mb`, `max_response_size_mb`, `max_file_size_mb` | `auth_utils.py`, `http_parsing_utils.py` |
| `enforced_params` | `general_settings.enforced_params` | `proxy_server.py`, `auth_utils.py` |
| `team_admin_roles` | Adding or promoting team admins | `team_endpoints.py` |
| `advanced_keys` | Wildcard model access groups, `get_spend_routes` permission, key regeneration, `tags`, `prompts`, `team_member_key_duration` metadata | `key_management_endpoints.py`, `common_utils.py` |
| `team_models` | Team-scoped models and custom team metadata validation | `model_management_endpoints.py` |
| `auto_router` | Members creating auto routers, unlimited auto-router capability | `auto_router_permissions.py` |
| `model_audit` | `worker_registry`, model `created_at/updated_at/created_by/updated_by` | `proxy_server.py` |
| `projects` | `/project/*` | `project_endpoints.py` |
| `managed_files` | Uploads with `target_model_names` | `hooks/managed_files.py` |

Premium metadata fields map to features through `PREMIUM_METADATA_FIELD_LICENCES` in `litellm/proxy/management_endpoints/common_utils.py`, enforced by `require_metadata_field_licence` on key, team and organization create and update. Empty values never need a licence (so you can always clear a field)

### 3.2 Gate patterns

There are three ways a gate refuses, depending on where it sits:

```python
# 1. HTTP route: 403 with "X is a premium feature"
from litellm.proxy.utils import require_license_feature
require_license_feature(LicenseFeature.PROJECTS, "Projects")

# 2. Boolean branch: feature silently ignored with a warning
if not is_licensed(LicenseFeature.GUARDRAILS):
    verbose_proxy_logger.warning("...dropped...")
    return {}

# 3. Startup config: refuse to boot
if general_settings.get("enforced_params") is not None and not is_licensed(LicenseFeature.ENFORCED_PARAMS, license_check):
    raise ValueError("...")
```

Rule we followed: settings that open things (public routes, bigger limits) are gated; settings that restrict things (`allowed_routes`, `admin_only_routes`) are always enforced, licensed or not. Upstream skipped `admin_only_routes` without a licence, which with runtime expiry would have quietly opened admin routes the day a licence expired

### 3.3 How to add a new gated feature (reproduce by hand)

1. Add a member to `LicenseFeature` in `entitlements.py`, for example `VECTOR_STORES = "vector_stores"`
2. At the entry point, call `require_license_feature(LicenseFeature.VECTOR_STORES, "Vector stores")` (route) or branch on `is_licensed(...)` (behaviour)
3. Write three tests using `license_test_helpers.py`:
   - no licence: refused
   - licence with only some other feature: refused
   - licence with only this feature: allowed

```python
from tests.test_litellm.proxy.auth.license_test_helpers import install_entitlements, licensed_entitlements, unlicensed_entitlements

def test_refused_without_feature(monkeypatch):
    install_entitlements(monkeypatch, licensed_entitlements(features=("sso",)))
    with pytest.raises(HTTPException, match="premium"):
        ...

def test_allowed_with_feature(monkeypatch):
    install_entitlements(monkeypatch, licensed_entitlements(features=("vector_stores",)))
    ...
```

4. Issue a licence with the new feature for customers who bought it. Old licences keep working; they just do not have the new feature

### 3.4 Impact of changing a gate

- Removing a gate makes the feature free for everyone, including expired licences
- Moving a gate from startup to call time changes behaviour on licence expiry (call time stops at expiry; startup only checks once)
- Renaming a feature value breaks every licence already issued that lists the old name. Never rename a value; add a new one and keep the old one

---

## 4. The tenant authorization engine (RBAC)

### 4.1 Why

Upstream LiteLLM roles are global (`proxy_admin`, `proxy_admin_viewer`, `internal_user`, `internal_user_viewer`, `org_admin`) and each endpoint checks them its own way. That made it easy for org A to read org B's data. The engine gives one central, testable answer to "may this actor do this action on this resource"

### 4.2 Files (the `agami/` package, sibling to `litellm/`)

| File | Contents |
|------|----------|
| `agami/auth/permissions.py` | `ResourceType` (organization, team, key, budget, model), `Action` (view, create, edit, delete, manage_members, revoke, use, override, assign, restrict) |
| `agami/auth/roles.py` | `TenantRole`, `Grant`, `PERMISSION_MATRIX` |
| `agami/auth/context.py` | `Actor`, `TeamMembership`, `Resource` dataclasses |
| `agami/auth/service.py` | `effective_roles`, `authorize`, `raise_for_decision` |
| `agami/auth/model_ownership.py` | Org model write rules |
| `agami/adapters/litellm_compat.py` | Maps LiteLLM DB rows to an `Actor` |
| `litellm/proxy/auth/agami_access.py` | Glue: `load_request_actor`, `key_model_visibility`, `admin_model_visibility` |

The `agami` package has no FastAPI route or DB code of its own (only `HTTPException` in `raise_for_decision`). It is pure logic, so it is easy to unit test. Its tests are in `tests/test_agami/` (`auth/test_service.py`, `auth/test_model_ownership.py`, `adapters/test_litellm_compat.py`, `routing/test_org_models.py`)

### 4.3 Roles and grants

`TenantRole`: `org_admin`, `team_admin`, `team_member`, `viewer`. `Grant`: `ANY` (any resource in scope) or `OWN` (only resources whose `owner_user_id` is the actor)

The matrix (`roles.py` lines 41 to 76):

| Resource, action | org_admin | team_admin | team_member | viewer |
|------------------|-----------|------------|-------------|--------|
| organization view | ANY | ANY | ANY | ANY |
| organization edit, manage_members | ANY | | | |
| team view | ANY | ANY | ANY | ANY |
| team create, edit, manage_members | ANY | ANY | | |
| team delete | ANY | | | |
| key view | ANY | ANY | ANY | ANY |
| key create, edit, revoke | ANY | ANY | OWN | |
| key delete | ANY | | | |
| key use | ANY | ANY | ANY | |
| budget view | ANY | ANY | ANY | ANY |
| budget create, edit, override | ANY | | | |
| model view | ANY | ANY | ANY | ANY |
| model use | ANY | ANY | ANY | |
| model create, edit, delete, assign, restrict | ANY | | | |

Pairs not in the matrix (organization create and delete) are super admin only

### 4.4 Decision workflow (`service.py`)

```
authorize(actor, action, resource)
  actor.is_active is False           -> Denied(INACTIVE_ACTOR)     (SCIM deprovisioned users)
  actor.is_super_admin               -> Allowed                     (PROXY_ADMIN)
  roles = effective_roles(actor, resource)
  roles empty                        -> Denied(NOT_A_MEMBER)
  grants = matrix[(type, action)] for those roles
  ANY in grants                      -> Allowed
  OWN in grants and owner == actor   -> Allowed, else Denied(NOT_OWNER)
  otherwise                          -> Denied(NOT_PERMITTED)
```

`effective_roles` (line 33): org roles always apply in their org. Team roles apply only when the team belongs to the resource's org and either the resource is that team's or the resource is org-level (no `team_id`). So a team admin of team T in org A cannot act on team U in org A, and nothing in org B

`raise_for_decision` turns a `Denied` into HTTP 403 with `{"error": "Not allowed to edit this team.", "reason": "not_permitted"}`. The `reason` field is your first debugging clue

### 4.5 Mapping LiteLLM rows to an Actor (`litellm_compat.py`)

- `is_super_admin` = user role is `proxy_admin`
- `is_active` = `metadata.scim_active` is not `False`
- Each `LiteLLM_OrganizationMembership` becomes an org role: `org_admin` maps to `ORG_ADMIN`, everything else to `VIEWER`. If the user's global role is view-only (`internal_user_viewer`, `proxy_admin_viewer`) it is always `VIEWER`
- Each team in `members_with_roles` where the user appears becomes a `TeamMembership`: `admin` maps to `TEAM_ADMIN`, `user` to `TEAM_MEMBER` (or `VIEWER` if view-only). Teams with no organization are skipped
- `load_actor`: a key with no user row (master key, service keys) gets an actor with only its key role (super admin if the key role is `proxy_admin`, otherwise no roles at all)

### 4.6 How to change it

- To give team members delete on their own keys: change `(ResourceType.KEY, Action.DELETE): _ORG_ADMIN` to a row that includes `team_member=Grant.OWN`. Impact: every endpoint that calls `authorize` for key delete changes at once. Run `tests/test_agami` and the key management tests
- To add a resource type: add it to `ResourceType`, add matrix rows, and call `authorize` from the endpoint with a `Resource(resource_type=..., organization_id=..., team_id=..., owner_user_id=...)`

### 4.7 Comparison with LiteLLM

| | LiteLLM | Agami |
|-|---------|-------|
| Role scope | Global | Per organization and per team |
| Where decided | Scattered `if user_role == ...` checks in each endpoint | One matrix plus one `authorize` function (plus the scoped helpers in section 6) |
| `proxy_admin_viewer` | Reads everything in every org | Read-only org admin of only the orgs it belongs to |
| Deprovisioned users | Not considered | `scim_active: false` denies everything |
| Denial detail | Varies | Fixed `reason` codes |

---

## 5. Organization-owned models

### 5.1 Problem

In LiteLLM a model name like `gpt-4o` is global: every key that can call `gpt-4o` load-balances across every deployment named `gpt-4o`. For a multi-tenant product, customer A must be able to bring their own `gpt-4o` (own API key, own billing) without customer B ever routing to it or even seeing it

### 5.2 Design (`agami/routing/org_models.py`)

- Each org deployment is stored with an internal name `agami-org/{organization_id}/{public_name}` (`org_model_name`, line 11). Different orgs never share a router group, so the router cannot mix them
- `model_info.organization_id` and `model_info.organization_public_model_name` record the owner and the name users type
- `deployment_org_model_name` (line 41) recognises an org deployment only if all three agree (owner, public name, internal name). A mismatched row is treated as global, which is why `1872d97` keeps them consistent on update
- `ModelVisibility(organization_ids, sees_every_organization)` (line 63) answers "may this caller see a model owned by org X":
  - `for_key(org_id, is_super_admin)`: what a key can call (global models plus its own org)
  - `for_actor(actor, key_org_id)`: what a person sees in admin views (every org they belong to)
  - `allows_owner(None)` is always true, so global models stay visible to everyone
  - `GLOBAL_MODELS_ONLY` and `EVERY_MODEL` are the two fixed instances

### 5.3 Workflow

Create (`model_management_endpoints.py` line ~1478): an org admin posts `/model/new` with `model_info.organization_id`. The handler copies the public `model_name` into `organization_public_model_name` and rewrites `model_name` to `agami-org/<org>/<name>`. Writes are checked by `authorize_org_model_write` then `raise_for_org_model_write` (`agami/auth/model_ownership.py`, used at line ~2033):

- Owner cannot change after creation (`OwnershipChangeRejected`)
- An org model cannot also have a `team_id`
- Unknown organization means 400 (`UnknownOrganization`)
- Otherwise the matrix decides (only `org_admin` of that org, or a proxy admin)

Request routing (`litellm/proxy/route_llm_request.py` line 80, `_organization_model_name`): when a request says `model: gpt-4o` and the key belongs to org A, the proxy checks whether `agami-org/A/gpt-4o` exists in the router. If yes it routes there; if not it falls back to the global `gpt-4o`. A key of org B can never produce the name `agami-org/A/...` because the org id comes from the key, not the request

Router lookups (`litellm/router.py` around lines 10065 to 10406): `_get_org_public_name_deployment` and the deployment lookups take a `visibility` argument defaulting to `GLOBAL_MODELS_ONLY`, so any code path that forgets to pass visibility can only see global models (fail closed)

Listings: `/models`, `/model/info`, `/v2/model/info`, `/model_group/info` filter through `ModelVisibility.visible_model_names` (`proxy_server.py` lines ~10948, 14638, 15430, 15744; `utils.py` ~8175). `/model_group/info` shows the caller's own org model under its public name (`077c9c8`)

Credentials: file, batch and credential endpoints call `key_model_visibility(user_api_key_dict)` so passthrough credential lookups cannot borrow another org's API key (`b88ad63`)

### 5.4 Debugging

- Model "not found" for an org user: check the DB row: `model_name` must equal `agami-org/<model_info.organization_id>/<model_info.organization_public_model_name>`. If any part disagrees it is treated as a global model with a strange name
- Org user sees another org's model: find the endpoint and check it passes a `ModelVisibility` built from the caller, not `EVERY_MODEL`
- Key has no `org_id`: it only gets global models. Set the key's or team's organization

---

## 6. Cross-tenant leak fixes

An audit on Oct 1 found 9 places where data crossed tenants. All are fixed

### 6.1 Central helpers (`litellm/proxy/management_endpoints/common_utils.py`)

```python
def org_wide_read_org_ids(user_role, memberships) -> tuple[str, ...]   # line ~225
```
Returns the orgs the caller may read in full: orgs where the membership role is `org_admin`, plus every org it belongs to when the caller's global role is `proxy_admin_viewer`

```python
async def caller_org_wide_read_org_ids(user_api_key_dict) -> frozenset[str]   # line ~259
```
Same, loading the caller's user row. A key with no user row has no orgs

```python
def can_see_every_team_key(user_api_key_dict, team_obj) -> bool   # line ~319
```
Team admins, and members the team explicitly granted `/key/list`, see every key in the team. Everyone else sees only their own keys

`_can_read_team_org_wide`, `_is_user_org_admin_for_team`, `_verify_org_access(access="read"|"write")` build on these. `access="write"` ignores the viewer role

### 6.2 The leaks

| Leak | Before | After | Where |
|------|--------|-------|-------|
| 1, 2 | `/spend/tags`, `/global/activity/cache_hits` open to non-admins | Proxy admin view only | spend endpoints |
| 3 | `/user/filter/ui` could search every user | Always scoped to the caller's orgs; `scope_user_search_to_org` setting removed | user endpoints |
| 4, 5 | Passing any administered `organization_id` opened every admin viewer route; spend logs and activity unscoped | Scoped for every non-admin | spend, activity |
| 6 | `proxy_admin_viewer` was a global super-reader | Per-org viewer (steps A1 to A4 below) | ~120 call sites |
| 7 | `/team/available` and self-join listed teams in every org | Only the caller's orgs | team endpoints |
| 8 | `/v2/guardrails/list` showed every team-less guardrail to everyone | Owner-scoped, new `organization_id` column | `guardrail_endpoints.py` |
| 9 | Plain team members saw every teammate's key and token hash | Own keys only, unless team admin or granted `/key/list` | `/team/info`, `/team/list`, `/key/info` |
| Gap | Org admins could not see org keys unless also a team member | Org-wide readers read every key in their orgs | `/key/info`, `/key/list` |

Leak 6 steps:

- A1: `user_api_key_has_admin_view` returns true only for `PROXY_ADMIN`. The viewer goes through internal-user-viewer route checks and sees only its own data
- A2: `org_wide_read_org_ids` gives the viewer read access back, but only for its own orgs: `/team/list`, `/v2/team/list`, `/user/list`, `/organization/daily/activity`, `/organization/info`
- A3a (`3001e86`): spend views. `_is_admin_view_safe` delegates to `user_api_key_has_admin_view`; team and user daily activity add org-wide teams and users
- A3b (`72a83d1`): every direct `PROXY_ADMIN_VIEW_ONLY` comparison that granted a global read now checks `PROXY_ADMIN`
- A4: proxy-wide config reads are refused to every non-admin (pinned by `test_proxy_wide_config_reads_are_refused_to_every_non_admin_role`)

Leak 8 details: migration `20261006000000_add_guardrail_organization_id` adds a nullable, indexed `organization_id` to `LiteLLM_GuardrailsTable`. `_guardrail_visible_to` (`guardrail_endpoints.py` line ~882):

```python
if ownership.team_id is not None:
    return ownership.team_id in scope.team_ids
return ownership.organization_id is not None and ownership.organization_id in scope.organization_ids
```

A guardrail with neither owner is proxy-wide and only proxy admins list it (that is a behaviour change from upstream). Proxy admins set the owner with `organization_id` on `POST /guardrails` and `PUT /guardrails/{id}`

Follow-up (Oct 14, #7):

- `GuardrailInfoResponse` in `litellm/types/guardrails.py` has `team_id` and `organization_id`, so `/guardrails/list`, `/v2/guardrails/list` and `/guardrails/{id}/info` show who owns each guardrail
- The values come from `_guardrail_ownership(row).model_dump()`, which reads only those two keys from the stored row. They were deliberately not added to the `Guardrail` TypedDict, because that type is also the `guardrail` field of the create and update request bodies. Adding them there would let a client put a fake owner into the in-memory copy of a guardrail. The real owner is only ever set from the top-level `organization_id` of the request, by a proxy admin
- `/guardrails/{id}/info` (and its alias `/guardrails/{id}`) used to return any guardrail's masked config to any logged-in user. It now takes `user_api_key_dict` and, for non-admins, answers 404 unless `_guardrail_visible_to(result, scope)` is true. 404 instead of 403 means a caller cannot probe which guardrail IDs exist
- Generated files regenerated with it: `litellm/proxy/_lazy_openapi_snapshot.json` (`python -m litellm.proxy._lazy_openapi_snapshot`) and `ui/litellm-dashboard/src/lib/http/schema.d.ts` (`npm run gen:api` in `ui/litellm-dashboard`). CI's "UI API types" workflow fails if either is stale
- Tests in `tests/test_litellm/proxy/guardrails/test_guardrail_endpoints.py`: `test_list_guardrails_v2_returns_each_guardrails_owner`, and `test_get_guardrail_info_follows_the_list_visibility`, which crosses every scoped guardrail (proxy-wide, org A, org B, team 1, team 2 in org A) with every caller (proxy admin, org and team member, org viewer, team key without user) and expects exactly the list's answer
- If you change the visibility rule, change only `_guardrail_visible_to`; the two lists and the info endpoint all call it

Admin UI (#9):

- `ui/litellm-dashboard/src/components/guardrails/types.ts`: the `Guardrail` interface has optional `team_id` and `organization_id`
- `guardrail_info_helpers.tsx`: `guardrailOwnerLabel({team_id, organization_id})` returns "Team <id>" when there is a team (a team wins, matching `_guardrail_visible_to`), else "Organization <id>", else "Proxy-wide"
- `guardrailTableColumns.tsx`: new Owner column (between Mode and Default On). `guardrail_info.tsx`: new Owner card on the overview tab
- Tests: `guardrail_table.test.tsx` checks the header and each of the three labels. Run with `npx vitest run "src/app/(dashboard)/guardrails"` from `ui/litellm-dashboard` (`npm ci --engine-strict=false` first if your Node is slightly older than `.nvmrc`)
- The owner is shown, not edited. To add an owner picker later, send `organization_id` at the top level of the create and update bodies (not inside `guardrail`), and only show the picker to proxy admins

### 6.3 How to debug a "user sees too much / too little" report

1. Identify the endpoint and the caller's `user_role`, `organization_memberships`, team roles
2. For org-level reads: what does `org_wide_read_org_ids(role, memberships)` return for that user? If the org is missing, the membership role is not `org_admin` (or the global role is not `proxy_admin_viewer`)
3. For team keys: does `can_see_every_team_key` return true? Check `members_with_roles` and `team_member_permissions`
4. For guardrails: check the row's `team_id` and `organization_id` against `_get_guardrail_reader_scope`
5. A 401 or 403 with `reason` comes from `authorize`; read section 4.4

### 6.4 How it differs from LiteLLM

Upstream had no concept of "this org admin can read only these orgs" for most read endpoints, and `proxy_admin_viewer` read everything. Agami behaviour for each role:

| Role | Sees |
|------|------|
| `proxy_admin` | Everything |
| `org_admin` (membership) | Everything in the orgs it administers, nothing else |
| `proxy_admin_viewer` | Read-only, everything in the orgs it belongs to |
| team admin | Its team, all keys in its team |
| plain member | Its own keys, its org read-only |

---

## 7. Rebuilt enterprise features

Upstream code under `enterprise/` has its own commercial licence, so it was deleted and the features we need were rewritten clean-room, reusing only public config keys and data models. Every restored guardrail refuses to start without `guardrails`, and `CustomGuardrail` takes `required_license_feature`, so a licence that expires after startup turns requests into 403 instead of silently skipping the guardrail

| Feature | Implementation | Config |
|---------|----------------|--------|
| `banned_keywords` | Built on the open-source content filter, BLOCK words on request and response | `banned_keywords_list` (list or `.txt`) |
| `blocked_user_check` | Pre-call guardrail, 403 when `user` or the resolved end user is listed, exact match | `blocked_user_list` |
| `hide_secrets` | `guardrail_hooks/hide_secrets/`, masks secrets as `[REDACTED <type>]` using `detect-secrets` regex detectors | `detect_secrets_config.plugins_used` |
| `openai_moderations` | Open-source OpenAI moderation guardrail run during the call | `openai_moderations_model_name` |
| `llamaguard_moderations` | `llamaguard_moderation.py`, sends turns to the guard model through the router; unsafe is 400, no verdict or failure is 502 (fail closed) | `llamaguard_model_name`, `llamaguard_unsafe_content_categories` |
| `llmguard_moderations` | `llm_guard_moderation.py`, posts to `LLM_GUARD_API_BASE/analyze/prompt` | `llm_guard_mode`: `all`, `key-specific`, `request-specific` |
| `google_text_moderation` | Cloud Natural Language v2 `moderateText` | `google_moderation_confidence_threshold` (default 0.8) |
| Tag-based guardrail modes | `mode: {tags: {...}, default: ...}` runs per matching request tag | guardrail `mode` |
| Custom UI SSO handler | `general_settings.custom_ui_sso_sign_in_handler`, behind `sso`, only accepts requests from `trusted_proxy_ranges` | `trusted_proxy_ranges` |

Code lives in `litellm/proxy/guardrails/legacy_callbacks.py` and `litellm/proxy/guardrails/guardrail_hooks/`. The custom SSO handler is in `ui_sso.py`. The trusted-proxy check matters because that path trusts identity headers set by the reverse proxy; without it anyone could send a header and log in as anyone

Debug: a guardrail that "does nothing" is usually unlicensed (check startup log and `/health/license`) or its mode does not match the request's tags

---

## 8. Projects

Upstream `/project/*` routes were removed with the enterprise code, which broke the Terraform provider audit. `litellm/proxy/management_endpoints/project_endpoints.py` rebuilds `/project/new`, `/project/update`, `/project/delete`, `/project/info`, `/project/list` behind `projects`, keeping the request and response shapes the Terraform provider and Admin UI use

Access rules:

- Proxy admins and org admins of the team's org manage projects
- Team admins too, when `team_admin_editable_team_fields` includes `projects`
- Team members and org-wide readers only read
- A project outside the caller's scope answers 404 (not 403, so existence is not leaked)

Business rules: budget fields create or update the project's own budget, `budget_id` links an existing budget, models must be in the team's models, moving a project to another team is refused, delete is refused while keys are attached. DB access goes through an injected `ProjectStore`, so tests use an in-memory store

---

## 9. Managed files

### 9.1 What it does

A "managed file" is uploaded once with `target_model_names` and is created on every deployment of those models. The caller gets one unified ID and can use it with any of those deployments. Batches and fine-tuning jobs created from it also get unified IDs

### 9.2 Files

- `litellm/proxy/hooks/managed_files.py`: `_PROXY_LiteLLMManagedFiles(CustomLogger, BaseFileEndpoints)`
- `litellm/proxy/hooks/__init__.py`: registered as `PROXY_HOOKS["managed_files"]`
- DB tables: `LiteLLM_ManagedFileTable`, `LiteLLM_ManagedObjectTable` (through `ManagedFileRepository`, `ManagedObjectRepository`)
- Tests: `tests/test_litellm/proxy/hooks/test_managed_files.py` (15 tests, in-memory `_FakeTable` Prisma fake)

The hook needs a database (`prisma_client`), so without `DATABASE_URL` it is not loaded

### 9.3 ID format

Unified file ID, before encoding:

```
litellm_proxy:{content_type};unified_id,{uuid};target_model_names,{names};llm_output_file_id,None;llm_output_file_model_id,None
```

It is encoded with `base64.urlsafe_b64encode(...).rstrip("=")`. Batch and fine-tune IDs use the `SpecialEnums` formats in the same way and carry the model ID plus the provider's raw ID. To debug an ID, decode it (add back `=` padding to a multiple of 4) and read the parts

### 9.4 Workflow

1. Upload (`acreate_file`): checks `managed_files` (403 otherwise), loops target models calling `llm_router.acreate_file`, rewinds the file stream between calls, collects `model_file_id_mapping` (model id to provider file id), stores it with `store_unified_file_id`
2. Chat or Responses with a unified file in `messages` or `input`: the pre-call hook builds `model_file_id_mapping`, refuses with 403 if the caller has no access, and `async_filter_deployments` keeps only deployments that hold the file (falls back to all healthy deployments if none match)
3. Batch or fine-tune create: `async_pre_call_deployment_hook` swaps `input_file_id`, `training_file`, `validation_file` for the chosen deployment's provider file ID, taking the model id from `model_info`, `litellm_metadata` or `metadata`
4. After the call: the post-call hook mints unified batch and fine-tune IDs and upserts a `LiteLLM_ManagedObjectTable` row with the creator's key, org and tags (`store_unified_object_id`)
5. Retrieve, delete, content: `afile_retrieve`, `afile_delete`, `afile_content` route through `_routable` (output-file model first, then mapped deployments). `aretrieve_batch` and fine-tune retrieve or cancel decode the ID into `model` and raw ID
6. Listing: `afile_list` and `list_user_batches` filter with `build_owner_filter`. A bad `after` cursor gives a 400 `invalid_value`

Access: the creator, their team and proxy admins (via `can_access_resource` in `isolation.py`). A missing row is admin only

### 9.5 Comparison and limits

Upstream's version lived in the enterprise package and used caching. Ours reads the DB each time (simpler, always consistent) and is tenant-scoped. Not rebuilt yet: `managed_vector_stores`

### 9.6 Batch cost poller (#10)

Problem: a managed batch costs money when the provider finishes it, but upstream's code only charged it when somebody called `GET /v1/batches/{id}` after completion (or LiteLLM's enterprise poller did). A batch nobody re-checks was never billed

File: `litellm/proxy/spend_tracking/batch_cost_poller.py`, class `CheckBatchCost`. Tests: `tests/test_litellm/proxy/spend_tracking/test_batch_cost_poller.py`

Registration (`proxy_server.py`, end of `initialize_scheduled_background_jobs`):

```python
if llm_router is not None and PROXY_BATCH_POLLING_ENABLED:
    batch_cost_poller = CheckBatchCost(prisma_client=prisma_client, llm_router=llm_router)
    if await batch_cost_poller.confirm_batch_processed_support():
        scheduler.add_job(batch_cost_poller.check_batch_cost, "interval",
                          seconds=proxy_batch_polling_interval + random.randint(0, 30),
                          id="check_batch_cost_job", ...)
```

Workflow of one run (`check_batch_cost`):

1. Query `LiteLLM_ManagedObjectTable` where `file_purpose="batch"` and `batch_processed=False`, oldest first, at most `BATCH_COST_POLL_LIMIT` (100)
2. For each row, decode `unified_object_id` (base64 of `litellm_proxy;model_id:<deployment>;llm_batch_id:<provider id>`). Skip rows that don't decode and LiteLLM-executed batches (`litellm_batch_` ids, priced elsewhere)
3. Build the retrieve call: `batch_id` = provider id, `litellm_metadata` = the creator's attribution (`user_api_key` and `user_api_key_hash` = stored hashed key, `user_api_key_user_id`, `user_api_key_team_id`, `user_api_key_org_id`, `tags`), plus the deployment's credential snapshot (`add_internal_model_credentials` with `EVERY_MODEL`, because the poller is a system job and org models must resolve)
4. `llm_router.aretrieve_batch(model=<deployment id>, ...)`. This runs the normal logging path: `litellm_logging.py` sees `call_type=aretrieve_batch` and a final batch, downloads the output file and prices it (`_handle_completed_batch`); `_ProxyDBLogger` writes the spend row with request id `<batch id>_batch_cost` and increments key, user, team and org spend
5. Update the row: `status` (`completed` stored as `complete`), plus `batch_processed=True` only if `batch_cost_is_final(response)`. A "completed" batch whose output file hasn't arrived yet stays eligible, so its cost isn't pinned at $0
6. One failing batch (deployment gone, provider down) is logged and counted (`record_check_batch_cost_error`) and the run continues. Prometheus gauges and counters that already existed (`litellm_check_batch_cost_*`) are fed when the Prometheus callback is on

Why it can't double-charge:

- The spend writer (`db_spend_update_writer.py`, `_claim_batch_cost_spend_log`) treats the `<batch id>_batch_cost` row as a claim: the first writer wins and later writers for the same id don't add spend. So a user retrieve and the poller racing on the same batch charge it once
- `batch_cost_poller_is_active()` in `openai_files_endpoints/common_utils.py` returns true only when `check_batch_cost_job` is scheduled and the poller confirmed the `batch_processed` column. Only then does the retrieve path stop accounting by itself (`batch_ignore_default_logging`) and leave `batch_processed` for the poller. If the poller is off, the retrieve path keeps charging as before

How to control it:

| Setting | Effect |
|---------|--------|
| `PROXY_BATCH_POLLING_ENABLED=false` (env) | Poller off, retrieve path accounts |
| `PROXY_BATCH_POLLING_INTERVAL` (env, seconds, default 3600) or `general_settings.proxy_batch_polling_interval` | How often it runs |
| No database or no router | Poller not registered |
| `general_settings.track_unmanaged_batch_cost` | Not supported, logs a warning. Only batches with a managed (unified) id are polled |

How to debug:

- Not running: look for `check_batch_cost_job` in the scheduler, or the warning `Batch cost poller disabled, batch_processed is not readable` (schema not migrated)
- A batch never billed: check its row. `batch_processed=True` with no `_batch_cost` spend row means the cost callback failed after retirement; look for errors around the retrieve in the proxy log. `batch_processed=False` and an old `updated_at` means the poller can't reach it (`Batch cost poller could not check <id>` warning)
- Billed to nobody: the row's `api_key`, `created_by`, `team_id`, `org_id` are empty (created by the master key, or before attribution was stored). The cost is still written, unattributed, on purpose

Different from LiteLLM: theirs was enterprise code with its own licence. Ours reuses the existing logging and spend-claim path instead of writing spend itself, so there is only one place that prices a batch

---

## 10. Dependency bumps (PR #2)

Python lock: `pyjwt` 2.15.1, `pypdf` 6.19.0, `mlflow` 3.17.0, `oauthlib` 4.0.0, `urllib3` 2.8.0, `tornado` 6.5.10, plus `fsspec`, `gitpython`, `langgraph-sdk`, `mako`, `multidict`, `werkzeug`. UI: `next` 16.3.8, `moment` 2.31.0, `brace-expansion` 5.0.12, `sharp` 0.35.5, plus `smol-toml` and `source-map-js`. Dockerfiles pin the `wolfi-base` digest with glibc 2.44-r8, and `apk add` asks for `openssl~3.6` because the pinned Prisma CLI cannot detect OpenSSL 4.0

Reproduce: `uv lock --upgrade-package <name>` for Python, `npm install <pkg>@<version>` in `ui/litellm-dashboard`, then run the tests and the UI build

---

## 11. Rust, e2e and tooling removal (PR #4)

Removed: `litellm-rust/`, `litellm/rust_bridge/`, four dispatch modules, `/v1/rust_control_plane/logs`, `tests/e2e/`, `.github/e2e-stack/`, record-replay, Redis chaos and load workflows, `docker/build_from_pip/`, Rust toolchain files and CI steps. The wheel builds with hatchling instead of maturin

Two features only worked through Rust, so they were ported to Python first:

- AWS Textract OCR: `litellm/llms/aws_textract/ocr/` (detect-document-text, analyze-document)
- Bedrock audio transcription: `litellm/llms/bedrock/audio_transcription/` (Converse API)

Both sign requests with SigV4 through new `sign_request` hooks on the OCR and transcription configs. Tested by unit tests only (no AWS account)

`litellm/public_entrypoints.py` keeps `completion`, `responses`, `anthropic_messages`, `ocr` typed because the `@client` decorator erases signatures

LiteLLM's repo tooling was also removed: AGENTS/CONTRIBUTING docs, templates, CODEOWNERS, git hooks, ruff-strict and type budget gates, mutation testing, CodSpeed, CodeQL, semgrep, OSV, grype, scorecard, zizmor, GitGuardian, release and issue bots, Dependabot, `.circleci/`, `vscode-extension/`, `helm/`, `terraform/`, `cookbook/`, `examples/`, installers and benchmarks. 15 workflows remain (unit, proxy DB, Postgres, Redis, MCP, UI build, lint, UI API types, docs, code quality, schema sync). The change classifier moved to `.github/scripts/classify_changes.sh`

Why: those tools needed LiteLLM's accounts and secrets, and they gated our PRs on budgets tuned for LiteLLM's codebase

---

## 12. Running the demo

Files: `agami_demo_config.yaml` and `docker-compose.demo.yml` are committed in the repo root (#8). `.env` is local only and never committed

`.env` needs (never commit or print these): `LITELLM_MASTER_KEY`, `GROQ_API_KEY`, `DATABASE_URL` (PostgreSQL), `AGAMI_LICENSE`

`agami_demo_config.yaml`:

```yaml
model_list:
  - model_name: gpt-oss-120b
    litellm_params: { model: groq/openai/gpt-oss-120b, api_key: os.environ/GROQ_API_KEY }
  - model_name: gpt-oss-20b
    litellm_params: { model: groq/openai/gpt-oss-20b, api_key: os.environ/GROQ_API_KEY }
litellm_settings: { drop_params: true }
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: true
```

Run locally:

```powershell
$env:PYTHONPATH='.'
.venv\Scripts\python.exe litellm\proxy\proxy_cli.py --config agami_demo_config.yaml --port 4000
```

(`litellm` loads `.env` on import.) Open `http://localhost:4000/ui`, log in with the master key, check `/health/license`

Docker: start Docker Desktop, then

```powershell
docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build
```

`docker-compose.demo.yml` is an override on top of `docker-compose.yml`. It renames the image to `agami-gateway:demo`, so Compose builds it from our `Dockerfile` instead of pulling LiteLLM's published image, and mounts `agami_demo_config.yaml` as `/app/config.yaml`. The base file supplies the Postgres container (`db`), port 4000, `env_file: .env` and the health check. Its `environment.DATABASE_URL` wins over the `DATABASE_URL` in `.env`, so the container uses the Compose database, not your local one. The first build takes several minutes (it builds the UI too)

Demo script for a manager:

1. `/health/license` shows the Agami licence and features
2. Create org A and org B, an org admin in each, a team in each
3. As org A admin, `/team/list` shows only A's team; `/organization/info` for B is refused
4. Add an org model to A; B's key cannot call or list it
5. Issue a licence without `organizations`, restart, show `/organization/new` returns 403
6. Chat call through Groq returns 200

---

## 13. Testing and lint commands

```powershell
$env:LITELLM_MODE='PRODUCTION'; $env:PYTHONPATH='.'
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -W ignore tests/test_litellm/proxy/auth/test_entitlements.py
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -W ignore tests/test_agami
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -W ignore tests/test_litellm/proxy/hooks/test_managed_files.py
```

`LITELLM_MODE=PRODUCTION` stops `.env` from loading, matching CI. Run `tests/test_litellm/proxy` per directory; one big run dies on this machine

Lint:

```powershell
cd litellm; ..\.venv\Scripts\ruff.exe check .; ..\.venv\Scripts\ruff.exe format --check .; cd ..
.venv\Scripts\ruff.exe check --config ruff-tests.toml tests
.venv\Scripts\python.exe -m pytest -q tests/documentation_tests/test_circular_imports.py
.venv\Scripts\python.exe -c "from litellm import *"
```

Known local failures not caused by this work (cost map, Windows, missing packages) are listed in `PROGRESS.md` under "Local test setup (Windows)"

Live DB behaviour suite: `tests/proxy_behavior` needs a migrated throwaway database (`prisma db push`); last run 902 passed

---

## 14. Debugging playbook

| Symptom | Likely cause | Check |
|---------|--------------|-------|
| UI says "This is a LiteLLM Enterprise feature" | No valid licence, or old session token | `/health/license`; log out and in |
| `/health/license` shows `has_license: true`, `license_type: community` | Token present but invalid or expired | Proxy log line `Ignoring AGAMI_LICENSE: ...` gives the reason |
| `InvalidSignatureError` in the reason | Token signed by a different private key than the bundled public key | Reissue with the right key |
| `InvalidIssuerError` | `iss` is not `agami` | Use `scripts/agami_license.py` |
| Feature 403 "X is a premium feature" | The licence lacks that feature | Compare `allowed_features` with section 3.1 |
| Proxy refuses to start mentioning `allowed_ips` or `enforced_params` | Those config keys need `access_control` or `enforced_params` | Add the feature or remove the setting |
| 403 with `reason: not_a_member` | Actor has no role in the resource's org | User's org memberships and team rows |
| 403 with `reason: not_owner` | Team member acting on someone else's key | Expected |
| Org user cannot see their org model | Inconsistent `model_name` and `model_info` | Section 5.4 |
| Guardrail invisible to non-admin | Guardrail has no `team_id` and no `organization_id` | Set `organization_id` |
| Managed file upload 403 | Missing `managed_files` | Licence |
| Managed file features absent entirely | No database, hook not loaded | `DATABASE_URL` |
| Redis connection errors in the log | `.env` points at a Redis that is not running | Non-fatal; remove `REDIS_*` or start Redis |

To decode a JWT without verifying (safe, just reading claims):

```powershell
.venv\Scripts\python.exe -c "import jwt,os; print(jwt.decode(os.environ['AGAMI_LICENSE'], options={'verify_signature': False}))"
```

Do not paste a real customer licence into online JWT sites

To verify one by hand:

```powershell
.venv\Scripts\python.exe -c "from litellm.proxy.auth.entitlements import *; from datetime import datetime,timezone; import os; print(verify_license(os.environ['AGAMI_LICENSE'], load_public_key(BUNDLED_PUBLIC_KEY_PATH), datetime.now(timezone.utc)))"
```

---

## 15. How to reproduce everything from scratch

Order matters, because later steps depend on earlier ones

1. Start from upstream LiteLLM at the "full code" commit (`2167fb7`)
2. Licence:
   - Write `entitlements.py` as in section 2.4 (start with `verify_license` and its tests using a throwaway key)
   - Write `scripts/agami_license.py`, run `keygen`, commit only the public key
   - Point `_license_check` in `proxy_server.py` at `get_entitlement_service()` and delete `litellm_license.py`
3. Tenant engine: write `agami/auth/*` and `agami/adapters/litellm_compat.py`, pure functions first, matrix tests next
4. Org models: `agami/routing/org_models.py`, then the create path in `model_management_endpoints.py`, then routing in `route_llm_request.py` and `router.py`, then each listing endpoint
5. Leaks: write a failing test per leak ("org A caller asks for org B data, expects nothing"), then fix through `org_wide_read_org_ids`, `can_see_every_team_key`, `_guardrail_visible_to`
6. Licence gates: replace each `premium_user` use with `is_licensed(LicenseFeature.X)`, one feature per commit, three tests each (section 3.3). Finally delete the `premium_user` global and keep only the UI token field
7. Rebuild guardrails, custom SSO, Projects, managed files (sections 7 to 9), each behind its feature
8. Bump dependencies, remove Rust and tooling (sections 10, 11)
9. After each step: targeted tests, ruff, circular imports, import safety, then a PR to `dev`

Search commands you will use constantly:

```powershell
rg "premium_user" litellm                         # should only show the UI token field and one error message
rg "is_licensed\(LicenseFeature\.\w+" litellm -o   # every gate
rg "PROXY_ADMIN_VIEW_ONLY" litellm                 # viewer checks, must not grant global reads
rg "agami-org/" litellm agami                      # org model naming
```

---

## 16. What is still open

- Step 6: rename the `litellm` package to `agami` (about 2,470 Python files, about 4,300 importers), keeping a thin `litellm` shim
- UI rebrand (the UI still says LiteLLM and reads one `premium_user` flag instead of per-feature gating)
- `managed_vector_stores` hook
- Docker Compose demo run (the files are committed in #8; the run needs Docker Desktop, which was not running)
- Step 7: refactor (scope not decided)

`PROGRESS.md` is the source of truth for this list. When an item is done, tick it there and add its explanation to the matching section here, in the same PR

---

## 17. Decisions log

Mirrors "Open decisions" in `PROGRESS.md`, with the reason for each

| Date | Decision | Why |
|------|----------|-----|
| Oct 4 | `proxy_admin_viewer` is per organization | A global reader breaks tenant isolation; it matches the adapter's org-scoped `VIEWER` |
| Oct 6 | Guardrails get a nullable `organization_id` column, schema-only migration | Lets orgs own guardrails without rewriting rows |
| Oct 6 | Plain team members see only their own keys | Teammates' token hashes were visible |
| Oct 6 | Proxy-wide config reads are proxy admin only | Config holds every tenant's settings |
| Oct 6 | One `jwt_auth` feature for JWT, OAuth2 and MCP token flows; grouped features for the rest | Fewer, sellable feature names |
| Oct 7 | `allowed_routes` and `admin_only_routes` always enforced, licensed or not | Restrictions must not lapse when a licence expires |
| Oct 7 | `allowed_ips` gated on `access_control` | Same family as public routes |
| Oct 8 | SCIM gated on `sso`; UI token keeps `premium_user` computed from `has_valid_license()`; Swagger branding needs any valid licence | About 60 UI components read the flag |
| Oct 8 | Order: step 3 (rebuild), then rename, then refactor; rename keeps a thin `litellm` shim; rebuilds are clean-room | The enterprise code has its own commercial licence |
| Oct 14 | Guardrail owner returned through `_guardrail_ownership`, not the request type; guardrail info answers 404 outside the caller's scope | Clients must not set owners; IDs must not be probeable |

## 18. Bugs found and fixed along the way

These were real defects (most also upstream) caught while moving gates and writing tests

- `/key/update` ran the old premium check inside a `try` that only logged, so an unlicensed premium metadata field was silently dropped and the update still succeeded. Now refused with 403
- Key regeneration, `model_max_budget` and enforced params answered 500 instead of 403 when unlicensed (caught by the live curl proof, fixed in `33b627d`)
- The `get_spend_routes` key permission raised a `ValueError` that surfaced as a bare "Internal Server Error". Now a 403 naming `advanced_keys`
- The old key tags gate was dead code (tags were already moved into `metadata` before the check ran). Removed; tags are gated through the metadata field map
- `enforced_params` in the config was checked before the licence in the same config was loaded, so a config-only licence was refused. `_apply_config_licence` now loads the licence first
- Upstream skipped `admin_only_routes` without a licence, so routes meant to be restricted were open on unlicensed installs

Live proof, Oct 8, on a fresh database: with an `sso`-only licence SCIM was 200 while `/organization/new`, `/key/generate` with `model_max_budget` and `/key/regenerate` were 403. With `sso,organizations,advanced_keys,budgets` all four were 200, and an org admin of org A saw only org A's team and got 401 on org B's `/organization/info`. `tests/proxy_behavior` on a migrated throwaway database: 902 passed
