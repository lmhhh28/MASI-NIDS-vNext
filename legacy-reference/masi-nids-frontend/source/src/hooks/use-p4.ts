import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import type {
  P4DeploymentListResponse,
  P4DeploymentRequestResolution,
  P4Switch,
  P4SwitchCreate,
} from "@/types/api";

export function useSwitches() {
  return useQuery<P4Switch[]>({
    queryKey: ["p4-switches"],
    queryFn: ({ signal }) => api.get("/p4/switches", { signal }).then((r) => r.data),
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
}

export function useSwitch(id: string | null) {
  return useQuery<P4Switch[], Error, P4Switch | null>({
    queryKey: ["p4-switches"],
    queryFn: ({ signal }) => api.get("/p4/switches", { signal }).then((r) => r.data),
    select: (switches) => switches.find((sw) => sw.id === id) ?? null,
    enabled: !!id,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
}

export function useDeployments(params: {
  switchId: string;
  status?: string;
  limit?: number;
  offset?: number;
}) {
  const limit = params.limit ?? 25;
  const offset = params.offset ?? 0;
  return useQuery<P4DeploymentListResponse>({
    queryKey: ["p4-deployments", params.switchId, params.status ?? "all", limit, offset],
    queryFn: ({ signal }) =>
      api
        .get<P4DeploymentListResponse>("/p4/deployments", {
          signal,
          params: {
            switch_id: params.switchId,
            status: params.status || undefined,
            include_total: 1,
            limit,
            offset,
          },
        })
        .then((response) => response.data),
    refetchInterval: (query) => {
      const unresolved = query.state.data?.items.some(
        (item) => item.status === "unknown" || ["rollback_unknown", "rollback_in_progress"].includes(item.rollback_state ?? ""),
      );
      return unresolved ? 5_000 : 20_000;
    },
    refetchIntervalInBackground: false,
  });
}

export function resolveP4Operation(idempotencyKey: string, signal?: AbortSignal) {
  return api
    .get<P4DeploymentRequestResolution>("/p4/deployment-requests/resolve", {
      signal,
      headers: { "Idempotency-Key": idempotencyKey },
    })
    .then((response) => response.data);
}

export function useRegisterSwitch() {
  const qc = useQueryClient();
  return useMutation<P4Switch, Error, P4SwitchCreate>({
    mutationFn: (payload) =>
      api.post("/p4/switches", payload).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["p4-switches"] });
    },
  });
}

export function useConnectSwitch() {
  const qc = useQueryClient();
  return useMutation<P4Switch, Error, string>({
    mutationFn: (switchId) =>
      api.post(`/p4/switches/${switchId}/connect`).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["p4-switches"] });
    },
  });
}

export function useConnectWriteMaster() {
  const qc = useQueryClient();
  return useMutation<P4Switch, Error, P4Switch>({
    mutationFn: (sw) =>
      api
        .post(`/p4/switches/${sw.id}/enable-writes`, {
          expected_runtime_generation_version: sw.runtime_generation_version,
          expected_agent_session_version: sw.agent_session_version,
          acknowledge_generation_change: sw.runtime_state === "changed",
        })
        .then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["p4-switches"] });
    },
  });
}
