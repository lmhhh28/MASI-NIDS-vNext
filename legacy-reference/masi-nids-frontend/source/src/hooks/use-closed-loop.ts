import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import type { ClosedLoopObservation, ObservationStartRequest } from "@/types/api";

export function useObservations(deploymentId: string | null) {
  return useQuery<ClosedLoopObservation[]>({
    queryKey: ["closed-loop-observations", deploymentId],
    queryFn: ({ signal }) =>
      api
        .get(`/p4/deployments/${deploymentId}/observations`, { signal })
        .then((r) => r.data),
    enabled: !!deploymentId,
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
  });
}

export function useStartObservation(deploymentId: string) {
  const qc = useQueryClient();
  return useMutation<ClosedLoopObservation, Error, ObservationStartRequest>({
    mutationFn: (payload) =>
      api
        .post(`/p4/deployments/${deploymentId}/observe`, payload)
        .then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["closed-loop-observations", deploymentId] });
    },
  });
}
