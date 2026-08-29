import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import { invalidateEventQueries } from "@/hooks/use-events";
import type { DirectionalEvidence } from "@/types/api";

interface ResolveDirectionalEvidencePayload {
  nids_event_id: string;
  raw_digest_path?: string | null;
  timestamp_field?: "control" | "switch" | null;
  tolerance_seconds?: number;
}

export function useDirectionalEvidence(id: string | null) {
  return useQuery<DirectionalEvidence>({
    queryKey: ["directional-evidence", id],
    queryFn: ({ signal }) =>
      api.get(`/directional-evidence/${id}`, { signal }).then((r) => r.data),
    enabled: !!id,
  });
}

export function useResolveDirectionalEvidence() {
  const qc = useQueryClient();
  return useMutation<DirectionalEvidence, Error, ResolveDirectionalEvidencePayload>({
    mutationFn: (payload) =>
      api.post("/directional-evidence:resolve", payload).then((r) => r.data),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["directional-evidence", data.id] });
      invalidateEventQueries(qc);
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
  });
}

export function useReviewDirectionalEvidence(id: string) {
  const qc = useQueryClient();
  return useMutation<DirectionalEvidence, Error, "approve" | "reject">({
    mutationFn: (decision) =>
      api
        .post(`/directional-evidence/${id}/review`, undefined, {
          params: { decision },
        })
        .then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["directional-evidence", id] });
      invalidateEventQueries(qc);
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
  });
}
