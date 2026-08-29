import { useQuery } from "@tanstack/react-query";

import api from "@/lib/api";
import type { AuditLogListResponse } from "@/types/api";

export interface AuditFilters {
  action?: string;
  actor_role?: string;
  resource?: string;
  request_id?: string;
  status?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
  offset?: number;
}

export function useAuditLogs(params: AuditFilters, live = true) {
  return useQuery<AuditLogListResponse>({
    queryKey: ["audit", params],
    queryFn: ({ signal }) =>
      api
        .get<AuditLogListResponse>("/audit", { signal, params: { ...params, include_total: 1 } })
        .then((response) => response.data),
    refetchInterval: live ? 15_000 : false,
    refetchIntervalInBackground: false,
  });
}
