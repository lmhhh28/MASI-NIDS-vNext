"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import api from "@/lib/api";
import type { FlowCaptureRequest, FlowCaptureResponse } from "@/types/api";

export const FLOW_CAPTURE_QUERY_KEY = "flow-capture";

export function useFlowCapture(operationId: string | null | undefined) {
  return useQuery<FlowCaptureResponse>({
    queryKey: [FLOW_CAPTURE_QUERY_KEY, operationId],
    queryFn: ({ signal }) =>
      api.get<FlowCaptureResponse>(`flow-captures/${operationId}`, { signal }).then((response) => response.data),
    enabled: Boolean(operationId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? 3_000 : false;
    },
    refetchIntervalInBackground: false,
    retry: false,
  });
}

export function useCreateFlowCapture() {
  const queryClient = useQueryClient();
  return useMutation<FlowCaptureResponse, Error, FlowCaptureRequest>({
    retry: false,
    mutationFn: (payload) =>
      api
        .post<FlowCaptureResponse>("flow-captures", payload, {
          headers: { "Idempotency-Key": crypto.randomUUID() },
        })
        .then((response) => response.data),
    onSuccess: (data) => {
      queryClient.setQueryData([FLOW_CAPTURE_QUERY_KEY, data.operation_id], data);
    },
  });
}
