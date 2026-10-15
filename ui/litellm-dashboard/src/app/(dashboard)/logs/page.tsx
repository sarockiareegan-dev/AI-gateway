"use client";

import useLicenseFeature from "@/app/(dashboard)/hooks/useLicenseFeature";
import SpendLogsTable from "@/components/view_logs";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";

export default function Logs() {
  const { accessToken, userRole, userId, token } = useAuthorized();
  const hasAuditLogs = useLicenseFeature("audit_logs");
  return (
    <SpendLogsTable
      userID={userId}
      userRole={userRole}
      token={token}
      accessToken={accessToken}
      premiumUser={hasAuditLogs}
    />
  );
}
