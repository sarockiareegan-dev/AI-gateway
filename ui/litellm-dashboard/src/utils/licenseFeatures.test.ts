import { describe, expect, it } from "vitest";
import { hasLicenseFeature } from "./licenseFeatures";

describe("hasLicenseFeature", () => {
  it("grants only listed features", () => {
    expect(hasLicenseFeature(["sso"], true, "sso")).toBe(true);
    expect(hasLicenseFeature(["sso"], true, "audit_logs")).toBe(false);
  });

  it("treats * as every feature", () => {
    expect(hasLicenseFeature(["*"], false, "guardrails")).toBe(true);
  });

  it("denies everything for an empty list", () => {
    expect(hasLicenseFeature([], true, "budgets")).toBe(false);
  });

  it("falls back to the premium flag when the token has no feature list", () => {
    expect(hasLicenseFeature(undefined, true, "budgets")).toBe(true);
    expect(hasLicenseFeature(null, false, "budgets")).toBe(false);
    expect(hasLicenseFeature(null, null, "budgets")).toBe(false);
  });
});
