"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import api from "@/lib/api";
import type { FlowEvidenceV3ListResponse, WorkflowResponse } from "@/types/api";

// FlowEvidenceV3 read-only list hook.
// Frozen baseline: Requirements r3 / Design r9 §13.1.
//
// Lists bounded candidate observed flows for a given Event v3 ID. Read-only;
// does not create, modify, or rewrite evidence. Returns an empty list when
// no evidence exists for the event — the wizard shows the "collect evidence"
// path instead.

export const FLOW_EVIDENCE_V3_QUERY_KEY = "flow-evidence-v3";

export function useFlowEvidenceV3(eventId: string | null) {
  const enabled = eventId !== null && eventId.trim().length > 0;
  const queryKey = enabled
    ? [FLOW_EVIDENCE_V3_QUERY_KEY, eventId]
    : [FLOW_EVIDENCE_V3_QUERY_KEY];

  return useQuery<FlowEvidenceV3ListResponse>({
    queryKey,
    queryFn: ({ signal }) =>
      api
        .get<FlowEvidenceV3ListResponse>(
          `flow-evidence/v3?event_id=${encodeURIComponent(eventId!)}`,
          { signal },
        )
        .then((response) => response.data),
    enabled,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useFlowEvidenceBlockAdmission(flowEvidenceId: string | null | undefined) {
  const queryClient = useQueryClient();
  return useMutation<WorkflowResponse, Error, { locale?: string; idempotencyKey?: string }>({
    retry: false,
    mutationFn: ({ locale, idempotencyKey }) => {
      if (!flowEvidenceId) throw new Error("FLOW_EVIDENCE_ID_REQUIRED");
      const key = idempotencyKey ?? crypto.randomUUID();
      return api
        .post<WorkflowResponse>(
          `workflows/flow-evidence-v3/${encodeURIComponent(flowEvidenceId)}/block`,
          {},
          {
            headers: {
              "Accept-Language": locale ?? "en",
              "Idempotency-Key": key,
            },
          },
        )
        .then((response) => response.data);
    },
    onSuccess: (workflow) => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      queryClient.setQueryData(["workflow-detail", workflow.id], workflow);
      queryClient.invalidateQueries({ queryKey: ["workflow-summary", workflow.id] });
    },
  });
}

export function useResolveFlowEvidenceBlockAdmission(
  flowEvidenceId: string | null | undefined,
  idempotencyKey: string | null | undefined,
) {
  return useQuery<WorkflowResponse>({
    queryKey: ["flow-evidence-block-admission", flowEvidenceId, idempotencyKey],
    queryFn: ({ signal }) =>
      api
        .get<WorkflowResponse>(`workflows/flow-evidence-v3/${encodeURIComponent(flowEvidenceId!)}/block`, {
          signal,
          headers: { "Idempotency-Key": idempotencyKey! },
        })
        .then((response) => response.data),
    enabled: Boolean(flowEvidenceId && idempotencyKey),
    retry: false,
  });
}
