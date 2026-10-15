import { useSyncExternalStore } from "react";

export const BRAND_NAME = "Iagami";
export const PRODUCT_NAME = "Iagami Gateway";

export interface BrandLinks {
  docsUrl: string;
  supportUrl: string;
  licenseContactEmail: string;
}

const EMPTY_LINKS: BrandLinks = { docsUrl: "", supportUrl: "", licenseContactEmail: "" };

let brandLinks: BrandLinks = EMPTY_LINKS;
const listeners = new Set<() => void>();

export const setBrandLinks = (links: Partial<BrandLinks>): void => {
  brandLinks = {
    docsUrl: links.docsUrl?.trim() ?? "",
    supportUrl: links.supportUrl?.trim() ?? "",
    licenseContactEmail: links.licenseContactEmail?.trim() ?? "",
  };
  listeners.forEach((listener) => listener());
};

const subscribe = (listener: () => void): (() => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

export const useBrandLinks = (): BrandLinks =>
  useSyncExternalStore(
    subscribe,
    () => brandLinks,
    () => EMPTY_LINKS,
  );

const UPSTREAM_DOCS_PREFIX = "https://docs.litellm.ai";
const UPSTREAM_SUPPORT_PREFIX = "https://github.com/BerriAI/litellm/";

/** Swaps an upstream docs page for the admin-configured docs site; other URLs pass through. */
export const brandDocsUrl = (url: string): string =>
  brandLinks.docsUrl && url.startsWith(UPSTREAM_DOCS_PREFIX) ? brandLinks.docsUrl : url;

/** Swaps an upstream issue or discussion page for the admin-configured support link; other URLs pass through. */
export const brandSupportUrl = (url: string): string =>
  brandLinks.supportUrl && url.startsWith(UPSTREAM_SUPPORT_PREFIX) ? brandLinks.supportUrl : url;
