"use client";

import { hasLicenseFeature, type LicenseFeature } from "@/utils/licenseFeatures";
import useAuthorized from "./useAuthorized";

const useLicenseFeature = (feature: LicenseFeature): boolean => {
  const { licenseFeatures, premiumUser } = useAuthorized();
  return hasLicenseFeature(licenseFeatures, premiumUser, feature);
};

export default useLicenseFeature;
