import { useMutation, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import type { P4Switch, P4SwitchDeleteResult } from "@/types/api";

export function useDeleteSwitch() {
  const qc = useQueryClient();
  return useMutation<
    P4SwitchDeleteResult,
    Error,
    { switchId: string; force?: boolean }
  >({
    mutationFn: ({ switchId, force }) =>
      api
        .delete(`/p4/switches/${switchId}`, { params: { force: force ? true : undefined } })
        .then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["p4-switches"] });
    },
  });
}

export function usePatchSwitch(switchId: string) {
  const qc = useQueryClient();
  return useMutation<
    P4Switch,
    Error,
    { name?: string; grpc_addr?: string; p4info_path?: string; pipeline_owner?: string }
  >({
    mutationFn: (payload) =>
      api.patch(`/p4/switches/${switchId}`, payload).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["p4-switches"] });
    },
  });
}
