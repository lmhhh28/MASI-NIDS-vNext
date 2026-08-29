import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import api from "@/lib/api";
import type { OperatorBatch, OperatorBatchCreate } from "@/types/api";

const ACTIVE_BATCH_KEY = "nids.operator-batch.active.v1";
const TERMINAL = new Set(["completed", "completed_with_errors", "cancelled"]);

export function loadActiveBatchId(): string | null {
  if (typeof window === "undefined") return null;
  const value = window.localStorage.getItem(ACTIVE_BATCH_KEY);
  return value && /^[0-9a-f-]{36}$/i.test(value) ? value : null;
}

export function rememberActiveBatchId(batchId: string | null): void {
  if (typeof window === "undefined") return;
  if (batchId) window.localStorage.setItem(ACTIVE_BATCH_KEY, batchId);
  else window.localStorage.removeItem(ACTIVE_BATCH_KEY);
}

export function isTerminalBatch(batch: Pick<OperatorBatch, "status"> | undefined): boolean {
  return Boolean(batch && TERMINAL.has(batch.status));
}

export function useOperatorBatch(batchId: string | null) {
  return useQuery<OperatorBatch>({
    queryKey: ["operator-batch", batchId],
    queryFn: ({ signal }) =>
      api
        .get<OperatorBatch>(`/operator-batches/${encodeURIComponent(batchId!)}`, { signal })
        .then((response) => response.data),
    enabled: Boolean(batchId),
    refetchInterval: (query) => (isTerminalBatch(query.state.data) ? false : 2_000),
    refetchIntervalInBackground: false,
  });
}

export function useCreateOperatorBatch() {
  const queryClient = useQueryClient();
  return useMutation<OperatorBatch, Error, { payload: OperatorBatchCreate; idempotencyKey: string }>({
    retry: false,
    mutationFn: ({ payload, idempotencyKey }) =>
      api
        .post<OperatorBatch>("/operator-batches", payload, {
          headers: { "Idempotency-Key": idempotencyKey },
        })
        .then((response) => response.data),
    onSuccess: (batch) => {
      rememberActiveBatchId(batch.id);
      queryClient.setQueryData(["operator-batch", batch.id], batch);
      queryClient.invalidateQueries({ queryKey: ["operations-summary"] });
    },
  });
}

export function useCancelOperatorBatch() {
  const queryClient = useQueryClient();
  return useMutation<OperatorBatch, Error, { batchId: string; expectedStateVersion: number }>({
    retry: false,
    mutationFn: ({ batchId, expectedStateVersion }) =>
      api
        .post<OperatorBatch>(`/operator-batches/${encodeURIComponent(batchId)}/cancel`, {
          expected_state_version: expectedStateVersion,
        })
        .then((response) => response.data),
    onSuccess: (batch) => {
      queryClient.setQueryData(["operator-batch", batch.id], batch);
      queryClient.invalidateQueries({ queryKey: ["operations-summary"] });
    },
  });
}
