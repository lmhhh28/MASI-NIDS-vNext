import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import api from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { metadataFor, workflowStatus } from "@/lib/status-metadata";
import type {
  WorkflowListResponse,
  WorkflowResponse,
  WorkflowStartRequest,
  WorkflowSummary,
} from "@/types/api";

export interface WorkflowListParams {
  status?: string;
  template?: string;
  workflow_id?: string;
  source_event_id?: string;
  alert_group_id?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

export function useWorkflows(params: WorkflowListParams = {}) {
  return useQuery<WorkflowListResponse>({
    queryKey: ["workflows", params],
    queryFn: ({ signal }) =>
      api
        .get("/workflows", { signal, params: { ...params, include_total: 1 } })
        .then((response) => response.data),
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      return items.some((item) => !metadataFor(workflowStatus, item.status).terminal) ? 5_000 : false;
    },
    refetchIntervalInBackground: false,
  });
}

export function useWorkflowSummary(workflowId: string | null | undefined) {
  return useQuery<WorkflowSummary>({
    queryKey: ["workflow-summary", workflowId],
    queryFn: ({ signal }) =>
      api.get(`/workflows/${workflowId}/summary`, { signal }).then((response) => response.data),
    enabled: Boolean(workflowId),
    refetchInterval: (query) => {
      const summary = query.state.data;
      return summary && !metadataFor(workflowStatus, summary.status).terminal ? 5_000 : false;
    },
    refetchIntervalInBackground: false,
  });
}

export function useStartWorkflow() {
  const queryClient = useQueryClient();
  const { locale } = useI18n();
  return useMutation<WorkflowResponse, Error, WorkflowStartRequest>({
    retry: false,
    mutationFn: (payload) => {
      const workflowLocale = payload.locale ?? locale;
      return api
        .post(
          "/workflows",
          { ...payload, locale: workflowLocale },
          {
            headers: {
              "Accept-Language": workflowLocale,
              "Idempotency-Key": crypto.randomUUID(),
            },
          }
        )
        .then((response) => response.data);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workflows"] }),
  });
}
