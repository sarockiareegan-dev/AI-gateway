import { useMutation, useQuery, useQueryClient, UseQueryResult } from "@tanstack/react-query";
import { apiClient } from "@/components/networking";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";

export type KeyRequestStatus = "pending" | "approved" | "rejected" | "cancelled";

export interface KeyRequest {
  request_id: string;
  team_id: string;
  user_id: string;
  key_alias?: string | null;
  models: string[];
  max_budget?: number | null;
  duration?: string | null;
  reason?: string | null;
  status: KeyRequestStatus;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  review_note?: string | null;
  key_token?: string | null;
  key_ready: boolean;
  created_at?: string | null;
}

export interface KeyTermsInput {
  key_alias?: string | null;
  models?: string[];
  max_budget?: number | null;
  duration?: string | null;
}

export interface NewKeyRequestInput extends KeyTermsInput {
  team_id: string;
  reason?: string | null;
}

export interface CollectedKey {
  key: string;
  key_token?: string | null;
}

const requestPath = (requestId: string, action: string) => `/key_request/${encodeURIComponent(requestId)}/${action}`;

export const keyRequestListCall = (accessToken: string, teamId: string): Promise<KeyRequest[]> =>
  apiClient.get(`/key_request/list`, { accessToken, query: { team_id: teamId } });

export const keyRequestCreateCall = (accessToken: string, body: NewKeyRequestInput): Promise<KeyRequest> =>
  apiClient.post(`/key_request/new`, { accessToken, body });

export const keyRequestApproveCall = (
  accessToken: string,
  requestId: string,
  body: KeyTermsInput & { note?: string | null },
): Promise<KeyRequest> => apiClient.post(requestPath(requestId, "approve"), { accessToken, body });

export const keyRequestRejectCall = (
  accessToken: string,
  requestId: string,
  note: string | null,
): Promise<KeyRequest> => apiClient.post(requestPath(requestId, "reject"), { accessToken, body: { note } });

export const keyRequestCancelCall = (accessToken: string, requestId: string): Promise<KeyRequest> =>
  apiClient.post(requestPath(requestId, "cancel"), { accessToken, body: {} });

export const keyRequestCollectCall = (accessToken: string, requestId: string): Promise<CollectedKey> =>
  apiClient.post(requestPath(requestId, "key"), { accessToken, body: {} });

const listKey = (teamId: string) => ["key_requests", teamId] as const;

export const useKeyRequests = (teamId: string): UseQueryResult<KeyRequest[]> => {
  const { accessToken } = useAuthorized();
  return useQuery<KeyRequest[]>({
    queryKey: listKey(teamId),
    queryFn: () => keyRequestListCall(accessToken!, teamId),
    enabled: Boolean(accessToken && teamId),
  });
};

export const useKeyRequestAction = <TArgs, TResult>(
  teamId: string,
  call: (accessToken: string, args: TArgs) => Promise<TResult>,
) => {
  const { accessToken } = useAuthorized();
  const queryClient = useQueryClient();
  return useMutation<TResult, unknown, TArgs>({
    mutationFn: (args) => call(accessToken!, args),
    onSettled: () => queryClient.invalidateQueries({ queryKey: listKey(teamId) }),
  });
};
