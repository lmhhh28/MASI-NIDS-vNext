import { useMutation, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import { invalidateEventQueries } from "@/hooks/use-events";
import type {
  EventSourceDeleteResult,
  EventSourcePatch,
  EventSourceResponse,
} from "@/types/api";

export function usePatchEventSource(sourceId: string) {
  const qc = useQueryClient();
  return useMutation<EventSourceResponse, Error, EventSourcePatch>({
    mutationFn: (payload) =>
      api.patch(`/events/sources/${sourceId}`, payload).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["event-sources"] });
    },
  });
}

export function useDeleteEventSource() {
  const qc = useQueryClient();
  return useMutation<EventSourceDeleteResult, Error, string>({
    mutationFn: (sourceId) =>
      api.delete(`/events/sources/${sourceId}`).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["event-sources"] });
      invalidateEventQueries(qc);
    },
  });
}
