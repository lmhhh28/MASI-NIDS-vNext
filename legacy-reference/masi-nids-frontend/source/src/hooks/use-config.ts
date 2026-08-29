import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";
import type { RiskPolicySummary, RuntimeConfig } from "@/types/api";

export function useRiskPolicySummary() {
  return useQuery<RiskPolicySummary>({
    queryKey: ["risk-policy-summary"],
    queryFn: ({ signal }) => api.get("/config/risk-policy", { signal }).then((r) => r.data),
    staleTime: 30_000,
  });
}

export function useRuntimeConfig() {
  return useQuery<RuntimeConfig>({
    queryKey: ["runtime-config"],
    queryFn: ({ signal }) => api.get("/config/runtime", { signal }).then((r) => r.data),
    staleTime: 10_000,
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
  });
}
