"use client";

import useLicenseFeature from "@/app/(dashboard)/hooks/useLicenseFeature";
import PassThroughSettings from "@/components/PassThroughSettings/PassThroughSettings";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";

export default function PassThroughPanel() {
  const { accessToken, userRole, userId: userID } = useAuthorized();
  const hasAccessControl = useLicenseFeature("access_control");
  return (
    <PassThroughSettings accessToken={accessToken} userRole={userRole} userID={userID} premiumUser={hasAccessControl} />
  );
}
