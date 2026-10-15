"use client";

import useLicenseFeature from "@/app/(dashboard)/hooks/useLicenseFeature";
import OrganizationsPanel from "./_components/OrganizationsPanel";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";

export default function OrganizationsPage() {
  const { accessToken, userRole } = useAuthorized();
  const hasOrganizations = useLicenseFeature("organizations");
  return <OrganizationsPanel userRole={userRole ?? ""} accessToken={accessToken} premiumUser={hasOrganizations} />;
}
