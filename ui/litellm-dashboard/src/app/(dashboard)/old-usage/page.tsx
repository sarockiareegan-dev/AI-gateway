"use client";

import useLicenseFeature from "@/app/(dashboard)/hooks/useLicenseFeature";
import Usage from "./_components/usage";
import { DeprecationBanner } from "@/components/DeprecationBanner";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";

export default function OldUsagePage() {
  const { accessToken, token, userRole, userId: userID } = useAuthorized();
  const hasSpendReports = useLicenseFeature("spend_reports");
  return (
    <>
      <DeprecationBanner featureName="The old Usage page" />
      <Usage
        accessToken={accessToken}
        token={token}
        userRole={userRole}
        userID={userID}
        keys={null}
        premiumUser={hasSpendReports}
      />
    </>
  );
}
