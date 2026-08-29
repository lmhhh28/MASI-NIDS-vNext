import { useQuery } from "@tanstack/react-query";

import api from "@/lib/api";
import type { OperationsSummary } from "@/types/api";

export const OPERATIONS_SUMMARY_INTERVAL_MS = 10_000;

export function useOperationsSummary() {
  return useQuery<OperationsSummary>({
    queryKey: ["operations-summary"],
    queryFn: ({ signal }) =>
      api.get<OperationsSummary>("/operations/summary", { signal }).then((response) => response.data),
    refetchInterval: OPERATIONS_SUMMARY_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}
