"use client";

import { useCallback } from "react";

import { parseApiError } from "@/lib/api-errors";
import { hasNoHttpResponse } from "@/lib/http-error";
import {
  newIdempotencyKey,
  payloadFingerprint,
  useOperationRegistry,
  type OperationRecord,
} from "@/lib/operations";

const UNKNOWN_CODES = new Set([
  "BFF_UPSTREAM_TIMEOUT",
  "BFF_UPSTREAM_UNAVAILABLE",
  "IDEMPOTENCY_IN_PROGRESS",
  "IDEMPOTENCY_NEEDS_RECONCILE",
]);
const REJECTED_RESULT_STATUSES = new Set(["rejected", "cancelled_before_submit"]);
const FAILED_RESULT_STATUSES = new Set(["failed", "p4_write_failed"]);
const PREPARED_CODES = new Set([
  "AUTH_MUTATION_RETRY_REQUIRED",
  "RUNTIME_STATE_UNKNOWN",
  "WORKFLOW_MAINTENANCE",
  "P4_MAINTENANCE",
]);

interface OperationOptions<TPayload, TResult> {
  kind: string;
  target: (payload: TPayload) => string;
  execute: (payload: TPayload, idempotencyKey: string) => Promise<TResult>;
  resultStatus?: (result: TResult) => string | undefined;
}

export function useIdempotentOperation<TPayload, TResult>(
  options: OperationOptions<TPayload, TResult>
) {
  const begin = useOperationRegistry((state) => state.begin);
  const transition = useOperationRegistry((state) => state.transition);

  const run = useCallback(
    async (payload: TPayload, existingKey?: string): Promise<{ result: TResult; key: string }> => {
      const key = existingKey ?? newIdempotencyKey(options.kind.replaceAll(/[^a-z0-9]+/gi, "-"));
      const fingerprint = await payloadFingerprint(payload);
      const current = useOperationRegistry.getState().operations[key];
      if (current && current.payloadFingerprint !== fingerprint) {
        throw new Error("IDEMPOTENCY_KEY_REUSE");
      }
      if (!current) {
        begin({
          key,
          kind: options.kind,
          target: options.target(payload),
          payloadFingerprint: fingerprint,
        });
      } else {
        transition(key, "submitting", { manualRetryRequired: false });
      }
      try {
        const result = await options.execute(payload, key);
        const resultStatus = options.resultStatus?.(result);
        if (["unknown", "needs_reconcile"].includes(resultStatus ?? "")) {
          transition(key, "outcome_unknown", { operationStatus: resultStatus });
        } else if (REJECTED_RESULT_STATUSES.has(resultStatus ?? "")) {
          transition(key, "rejected", { operationStatus: resultStatus });
        } else if (FAILED_RESULT_STATUSES.has(resultStatus ?? "")) {
          transition(key, "failed", { operationStatus: resultStatus });
        } else {
          transition(key, "succeeded", { operationStatus: resultStatus });
        }
        return { result, key };
      } catch (error) {
        const info = parseApiError(error);
        const noResponse = hasNoHttpResponse(error);
        const patch: Partial<OperationRecord> = {
          requestId: info.requestId,
          deploymentId: info.deploymentId,
          operationStatus: info.operationStatus,
          errorCode: info.code,
        };
        if (info.manualRetryRequired || PREPARED_CODES.has(info.code ?? "")) {
          transition(key, "prepared", { ...patch, manualRetryRequired: true });
        } else if (noResponse || UNKNOWN_CODES.has(info.code ?? "")) {
          transition(key, "outcome_unknown", patch);
        } else if ((info.status ?? 500) < 500) {
          transition(key, "rejected", patch);
        } else {
          transition(key, "failed", patch);
        }
        throw error;
      }
    },
    [begin, options, transition]
  );

  return { run };
}
