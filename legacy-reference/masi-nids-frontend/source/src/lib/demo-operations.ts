"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";

import type {
  DemoOperationResponse,
  DemoOperationStatus,
  DemoTrafficCleanupRequest,
  DemoTrafficRunRequest,
} from "@/types/api";

export type TrackedDemoOperationKind = "run" | "stop" | "cleanup";
export type TrackedDemoOperationPayload =
  | DemoTrafficRunRequest
  | { online_run_id?: string }
  | DemoTrafficCleanupRequest;
export type TrackedDemoOperationStatus = "submitting" | DemoOperationStatus;

export interface TrackedDemoOperation {
  key: string;
  actorUserId: string;
  kind: TrackedDemoOperationKind;
  onlineRunId: string | null;
  payload: TrackedDemoOperationPayload;
  status: TrackedDemoOperationStatus;
  operationId: string | null;
  phase: string | null;
  attemptCount: number;
  result: Record<string, unknown>;
  errorCode: string | null;
  needsResolve: boolean;
  manualRetryRequired: boolean;
  terminalHandled: boolean;
  createdAt: string;
  updatedAt: string;
  finishedAt: string | null;
}

interface DemoOperationRegistryState {
  operations: Record<string, TrackedDemoOperation>;
  begin: (operation: {
    key: string;
    actorUserId: string;
    kind: TrackedDemoOperationKind;
    onlineRunId: string | null;
    payload: TrackedDemoOperationPayload;
  }) => void;
  applyResponse: (actorUserId: string, key: string, response: DemoOperationResponse) => void;
  markAdmissionUnknown: (actorUserId: string, key: string, errorCode?: string | null) => void;
  markAdmissionFailed: (actorUserId: string, key: string, errorCode?: string | null) => void;
  markOperationMissing: (actorUserId: string, key: string, errorCode?: string | null) => void;
  requestResolution: (actorUserId: string, key: string, errorCode?: string | null) => void;
  prepareRetry: (actorUserId: string, key: string) => void;
  markTerminalHandled: (actorUserId: string, key: string) => void;
  remove: (actorUserId: string, key: string) => void;
  prune: () => void;
}

export const DEMO_OPERATION_TERMINAL_RETENTION_MS = 7 * 24 * 60 * 60 * 1_000;
export const DEMO_OPERATION_TERMINAL_LIMIT = 20;
export const DEMO_OPERATION_UNRESOLVED_LIMIT = 20;

const SERVER_TERMINAL_STATUSES = new Set<DemoOperationStatus>([
  "succeeded",
  "failed",
  "outcome_unknown",
  "cancelled",
]);

export function demoOperationStoreKey(actorUserId: string, key: string): string {
  return `${actorUserId}:${key}`;
}

export function isDemoOperationTerminal(operation: TrackedDemoOperation): boolean {
  return !operation.needsResolve && operation.status !== "submitting" &&
    SERVER_TERMINAL_STATUSES.has(operation.status);
}

export function isDemoOperationActive(operation: TrackedDemoOperation): boolean {
  return !isDemoOperationTerminal(operation);
}

export function actorDemoOperations(
  operations: Record<string, TrackedDemoOperation>,
  actorUserId: string | null | undefined,
): TrackedDemoOperation[] {
  if (!actorUserId) return [];
  return Object.values(operations)
    .filter((operation) => operation.actorUserId === actorUserId)
    .sort((left, right) => right.createdAt.localeCompare(left.createdAt));
}

export function normalizePersistedDemoOperations(
  operations: Record<string, TrackedDemoOperation>,
  now = Date.now(),
): Record<string, TrackedDemoOperation> {
  const unresolved: TrackedDemoOperation[] = [];
  const terminal: TrackedDemoOperation[] = [];
  for (const operation of Object.values(operations)) {
    const normalized = operation.status === "submitting"
      ? {
          ...operation,
          status: "outcome_unknown" as const,
          needsResolve: true,
          manualRetryRequired: false,
          terminalHandled: false,
          errorCode: operation.errorCode ?? "PAGE_RELOADED_DURING_SUBMIT",
        }
      : operation;
    if (!isDemoOperationTerminal(normalized)) {
      unresolved.push(normalized);
      continue;
    }
    const updatedAt = Date.parse(normalized.updatedAt || normalized.createdAt);
    if (Number.isFinite(updatedAt) && now - updatedAt <= DEMO_OPERATION_TERMINAL_RETENTION_MS) {
      terminal.push(normalized);
    }
  }
  terminal.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
  return Object.fromEntries(
    [...unresolved, ...terminal.slice(0, DEMO_OPERATION_TERMINAL_LIMIT)].map((operation) => [
      demoOperationStoreKey(operation.actorUserId, operation.key),
      operation,
    ]),
  );
}

export const useDemoOperationRegistry = create<DemoOperationRegistryState>()(
  persist(
    (set) => ({
      operations: {},
      begin: (operation) =>
        set((state) => {
          const existing = actorDemoOperations(state.operations, operation.actorUserId).filter(
            isDemoOperationActive,
          );
          if (existing.length >= DEMO_OPERATION_UNRESOLVED_LIMIT) {
            throw new Error("DEMO_OPERATION_RECOVERY_LIMIT_REACHED");
          }
          const storeKey = demoOperationStoreKey(operation.actorUserId, operation.key);
          const current = state.operations[storeKey];
          if (current) {
            if (
              current.kind !== operation.kind ||
              JSON.stringify(current.payload) !== JSON.stringify(operation.payload)
            ) {
              throw new Error("IDEMPOTENCY_KEY_REUSE");
            }
            return state;
          }
          const timestamp = new Date().toISOString();
          return {
            operations: {
              ...state.operations,
              [storeKey]: {
                ...operation,
                status: "submitting",
                operationId: null,
                phase: "admission",
                attemptCount: 0,
                result: {},
                errorCode: null,
                needsResolve: false,
                manualRetryRequired: false,
                terminalHandled: false,
                createdAt: timestamp,
                updatedAt: timestamp,
                finishedAt: null,
              },
            },
          };
        }),
      applyResponse: (actorUserId, key, response) =>
        set((state) => {
          const storeKey = demoOperationStoreKey(actorUserId, key);
          const current = state.operations[storeKey];
          if (!current) return state;
          if (
            current.operationId === response.operation_id &&
            current.status === response.status &&
            current.updatedAt === response.updated_at &&
            current.errorCode === response.error_code
          ) {
            return state;
          }
          return {
            operations: {
              ...state.operations,
              [storeKey]: {
                ...current,
                operationId: response.operation_id,
                status: response.status,
                phase: response.phase,
                onlineRunId: response.online_run_id ?? current.onlineRunId,
                attemptCount: response.attempt_count,
                result: response.result ?? {},
                errorCode: response.error_code,
                needsResolve: false,
                manualRetryRequired: false,
                terminalHandled: isDemoOperationTerminal({
                  ...current,
                  status: response.status,
                  needsResolve: false,
                }) && current.status === response.status
                  ? current.terminalHandled
                  : false,
                updatedAt: response.updated_at,
                finishedAt: response.finished_at,
              },
            },
          };
        }),
      markAdmissionUnknown: (actorUserId, key, errorCode = null) =>
        set((state) => patchOperation(state, actorUserId, key, {
          status: "outcome_unknown",
          phase: "admission_response_lost",
          errorCode,
          needsResolve: true,
          manualRetryRequired: false,
          terminalHandled: false,
        })),
      markAdmissionFailed: (actorUserId, key, errorCode = null) =>
        set((state) => patchOperation(state, actorUserId, key, {
          status: "failed",
          phase: "admission",
          errorCode,
          needsResolve: false,
          manualRetryRequired: true,
          terminalHandled: false,
          finishedAt: new Date().toISOString(),
        })),
      markOperationMissing: (actorUserId, key, errorCode = "DEMO_OPERATION_NOT_FOUND") =>
        set((state) => patchOperation(state, actorUserId, key, {
          status: "outcome_unknown",
          phase: "admission_not_found",
          operationId: null,
          errorCode,
          needsResolve: false,
          manualRetryRequired: true,
          terminalHandled: false,
          finishedAt: new Date().toISOString(),
        })),
      requestResolution: (actorUserId, key, errorCode = "DEMO_OPERATION_NOT_FOUND") =>
        set((state) => patchOperation(state, actorUserId, key, {
          status: "outcome_unknown",
          phase: "resolving_by_idempotency_key",
          operationId: null,
          errorCode,
          needsResolve: true,
          manualRetryRequired: false,
          terminalHandled: false,
          finishedAt: null,
        })),
      prepareRetry: (actorUserId, key) =>
        set((state) => patchOperation(state, actorUserId, key, {
          status: "submitting",
          phase: "admission_retry",
          errorCode: null,
          needsResolve: false,
          manualRetryRequired: false,
          terminalHandled: false,
          finishedAt: null,
        })),
      markTerminalHandled: (actorUserId, key) =>
        set((state) => patchOperation(state, actorUserId, key, { terminalHandled: true })),
      remove: (actorUserId, key) =>
        set((state) => {
          const operations = { ...state.operations };
          delete operations[demoOperationStoreKey(actorUserId, key)];
          return { operations };
        }),
      prune: () => set((state) => ({ operations: normalizePersistedDemoOperations(state.operations) })),
    }),
    {
      name: "nids-demo-operation-registry-v1",
      version: 1,
      partialize: (state) => ({ operations: state.operations }),
      merge: (persisted, current) => {
        const restored = persisted as Partial<DemoOperationRegistryState>;
        return {
          ...current,
          ...restored,
          operations: normalizePersistedDemoOperations(restored.operations ?? {}),
        };
      },
    },
  ),
);

function patchOperation(
  state: DemoOperationRegistryState,
  actorUserId: string,
  key: string,
  patch: Partial<TrackedDemoOperation>,
): DemoOperationRegistryState {
  const storeKey = demoOperationStoreKey(actorUserId, key);
  const current = state.operations[storeKey];
  if (!current) return state;
  return {
    ...state,
    operations: {
      ...state.operations,
      [storeKey]: {
        ...current,
        ...patch,
        key,
        actorUserId,
        updatedAt: new Date().toISOString(),
      },
    },
  };
}
