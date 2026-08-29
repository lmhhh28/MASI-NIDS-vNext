import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";
import type { AlertGroup } from "@/types/api";

export function useAlerts(params?: {
  status?: string;
  limit?: number;
  offset?: number;
}) {
  return useQuery<AlertGroup[]>({
    queryKey: ["alerts", params],
    queryFn: ({ signal }) => api.get("/alerts", { params, signal }).then((r) => r.data),
    refetchInterval: 12_000,
    refetchIntervalInBackground: false,
  });
}
