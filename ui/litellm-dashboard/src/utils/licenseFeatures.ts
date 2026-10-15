export type LicenseFeature =
  | "sso"
  | "organizations"
  | "secret_managers"
  | "logging_integrations"
  | "guardrails"
  | "spend_reports"
  | "budgets"
  | "access_control"
  | "audit_logs"
  | "email_branding"
  | "fine_tuning"
  | "jwt_auth"
  | "request_limits"
  | "enforced_params"
  | "team_admin_roles"
  | "advanced_keys"
  | "team_models"
  | "auto_router"
  | "model_audit"
  | "projects"
  | "managed_files";

/** Tokens issued before per-feature licensing carry no feature list, so they fall back to the all-or-nothing premium flag. */
export const hasLicenseFeature = (
  licenseFeatures: readonly string[] | null | undefined,
  premiumUser: boolean | null | undefined,
  feature: LicenseFeature,
): boolean => {
  if (!Array.isArray(licenseFeatures)) return premiumUser === true;
  return licenseFeatures.includes("*") || licenseFeatures.includes(feature);
};
