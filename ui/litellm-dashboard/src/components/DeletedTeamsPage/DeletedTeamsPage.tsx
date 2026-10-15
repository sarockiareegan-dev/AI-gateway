"use client";
import useLicenseFeature from "@/app/(dashboard)/hooks/useLicenseFeature";
import { PaginationState } from "@tanstack/react-table";
import { Info } from "lucide-react";
import { useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { DEFAULT_PAGE_SIZE_OPTIONS } from "@/components/shared/DataTable";
import { useDeletedTeams } from "@/app/(dashboard)/hooks/teams/useTeams";
import { DeletedTeamsTable } from "./DeletedTeamsTable/DeletedTeamsTable";

export default function DeletedTeamsPage() {
  const hasAuditLogs = useLicenseFeature("audit_logs");
  const [pagination, setPagination] = useState<PaginationState>({
    pageIndex: 0,
    pageSize: DEFAULT_PAGE_SIZE_OPTIONS[0],
  });
  const { data: teamsData, isLoading } = useDeletedTeams(pagination.pageIndex + 1, pagination.pageSize);

  return (
    <div className="flex flex-col gap-4">
      {!hasAuditLogs && (
        <Alert>
          <Info />
          <AlertTitle>Audit logs not licensed</AlertTitle>
          <AlertDescription>Deleted team auditing needs an Iagami license with audit logs</AlertDescription>
        </Alert>
      )}
      <DeletedTeamsTable
        teams={teamsData?.teams ?? []}
        isLoading={isLoading}
        pagination={pagination}
        onPaginationChange={setPagination}
        rowCount={teamsData?.total ?? 0}
      />
    </div>
  );
}
