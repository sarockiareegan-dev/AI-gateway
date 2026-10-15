import { afterEach, describe, expect, it } from "vitest";
import { brandDocsUrl, brandSupportUrl, setBrandLinks } from "./brand";

const UPSTREAM_DOCS = "https://docs.litellm.ai/docs/proxy/caching";
const UPSTREAM_ISSUES = "https://github.com/BerriAI/litellm/issues/new?template=feature_request.yml";

describe("brand links", () => {
  afterEach(() => setBrandLinks({}));

  it("keeps upstream links when nothing is configured", () => {
    expect(brandDocsUrl(UPSTREAM_DOCS)).toBe(UPSTREAM_DOCS);
    expect(brandSupportUrl(UPSTREAM_ISSUES)).toBe(UPSTREAM_ISSUES);
  });

  it("swaps upstream docs and support links for the configured ones", () => {
    setBrandLinks({ docsUrl: " https://docs.example.com ", supportUrl: "mailto:help@example.com" });

    expect(brandDocsUrl(UPSTREAM_DOCS)).toBe("https://docs.example.com");
    expect(brandSupportUrl(UPSTREAM_ISSUES)).toBe("mailto:help@example.com");
  });

  it("leaves other links alone", () => {
    setBrandLinks({ docsUrl: "https://docs.example.com", supportUrl: "https://help.example.com" });

    expect(brandDocsUrl("https://api.slack.com/messaging/webhooks")).toBe("https://api.slack.com/messaging/webhooks");
    expect(brandSupportUrl("https://github.com/BerriAI/litellm-pgvector")).toBe(
      "https://github.com/BerriAI/litellm-pgvector",
    );
  });
});
