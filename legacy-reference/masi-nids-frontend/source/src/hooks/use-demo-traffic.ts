"use client";

import { useMutation, useQuery } from "@tanstack/react-query";

import api from "@/lib/api";
import { parseApiError } from "@/lib/api-errors";
import {
  demoOperationStoreKey,
  useDemoOperationRegistry,
  type TrackedDemoOperation,
  type TrackedDemoOperationKind,
  type TrackedDemoOperationPayload,
} from "@/lib/demo-operations";
import { hasNoHttpResponse } from "@/lib/http-error";
import { useAuthStore } from "@/lib/auth";
import type {
  DemoTrafficCleanupRequest,
  DemoTrafficCleanupPreviewRequest,
  DemoTrafficCleanupPreview,
  DemoOperationResponse,
  DemoTrafficRunRequest,
  DemoTrafficStatus,
} from "@/types/api";

const UNKNOWN_ADMISSION_CODES = new Set([
  "BFF_UPSTREAM_TIMEOUT",
  "BFF_UPSTREAM_UNAVAILABLE",
  "ECONNABORTED",
  "ERR_CANCELED",
  "ERR_NETWORK",
]);

export interface DemoOperationSubmission<TPayload> {
  payload: TPayload;
  onlineRunId: string | null;
  idempotencyKey?: string;
}

export interface DemoOperationAdmission {
  operation: DemoOperationResponse;
  idempotencyKey: string;
}

function newDemoIdempotencyKey(kind: TrackedDemoOperationKind): string {
  return `demo-${kind}-${globalThis.crypto.randomUUID()}`;
}

function endpointFor(kind: TrackedDemoOperationKind): string {
  return `/admin/demo-traffic/${kind}`;
}

export async function submitTrackedDemoOperation<TPayload extends TrackedDemoOperationPayload>(
  actorUserId: string,
  kind: TrackedDemoOperationKind,
  submission: DemoOperationSubmission<TPayload>,
): Promise<DemoOperationAdmission> {
  const registry = useDemoOperationRegistry.getState();
  const key = submission.idempotencyKey ?? newDemoIdempotencyKey(kind);
  const storeKey = demoOperationStoreKey(actorUserId, key);
  const current = registry.operations[storeKey];
  if (!current) {
    registry.begin({
      key,
      actorUserId,
      kind,
      onlineRunId: submission.onlineRunId,
      payload: submission.payload,
    });
  } else {
    if (
      current.kind !== kind ||
      JSON.stringify(current.payload) !== JSON.stringify(submission.payload)
    ) {
      throw new Error("IDEMPOTENCY_KEY_REUSE");
    }
    registry.prepareRetry(actorUserId, key);
  }

  try {
    const response = await api.post<DemoOperationResponse>(endpointFor(kind), submission.payload, {
      headers: { "Idempotency-Key": key },
    });
    useDemoOperationRegistry.getState().applyResponse(actorUserId, key, response.data);
    return { operation: response.data, idempotencyKey: key };
  } catch (error) {
    const info = parseApiError(error);
    if (hasNoHttpResponse(error) || UNKNOWN_ADMISSION_CODES.has(info.code ?? "")) {
      useDemoOperationRegistry.getState().markAdmissionUnknown(actorUserId, key, info.code);
    } else {
      useDemoOperationRegistry.getState().markAdmissionFailed(actorUserId, key, info.code);
    }
    throw error;
  }
}

export async function retryTrackedDemoOperation(
  operation: TrackedDemoOperation,
): Promise<DemoOperationAdmission> {
  return submitTrackedDemoOperation(operation.actorUserId, operation.kind, {
    payload: operation.payload,
    onlineRunId: operation.onlineRunId,
    idempotencyKey: operation.key,
  });
}

export function getDemoOperation(operationId: string, signal?: AbortSignal) {
  return api
    .get<DemoOperationResponse>(`/admin/demo-traffic/operations/${encodeURIComponent(operationId)}`, {
      signal,
    })
    .then((response) => response.data);
}

export function resolveDemoOperation(idempotencyKey: string, signal?: AbortSignal) {
  return api
    .get<DemoOperationResponse>("/admin/demo-traffic/operations/resolve", {
      signal,
      headers: { "Idempotency-Key": idempotencyKey },
    })
    .then((response) => response.data);
}

export function useDemoTrafficStatus() {
  return useQuery<DemoTrafficStatus>({
    queryKey: ["demo-traffic-status"],
    queryFn: ({ signal }) => api.get("/admin/demo-traffic/status", { signal }).then((r) => r.data),
    refetchInterval: 3_000,
    refetchIntervalInBackground: false,
  });
}

export function usePreviewDemoCleanup() {
  return useMutation<DemoTrafficCleanupPreview, Error, DemoTrafficCleanupPreviewRequest>({
    retry: false,
    mutationFn: (payload) =>
      api.post("/admin/demo-traffic/cleanup/preview", payload).then((response) => response.data),
  });
}

function useTrackedDemoMutation<TPayload extends TrackedDemoOperationPayload>(
  kind: TrackedDemoOperationKind,
) {
  const actorUserId = useAuthStore((state) => state.user?.id ?? null);
  return useMutation<DemoOperationAdmission, Error, DemoOperationSubmission<TPayload>>({
    retry: false,
    mutationFn: (submission) => {
      if (!actorUserId) throw new Error("AUTH_REQUIRED");
      return submitTrackedDemoOperation(actorUserId, kind, submission);
    },
  });
}

export function useRunDemoTraffic() {
  return useTrackedDemoMutation<DemoTrafficRunRequest>("run");
}

export function useStopDemoTraffic() {
  return useTrackedDemoMutation<{ online_run_id?: string }>("stop");
}

export function useCleanupDemoTraffic() {
  return useTrackedDemoMutation<DemoTrafficCleanupRequest>("cleanup");
}
