"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";

export type OperationOutcome =
  | "prepared"
  | "submitting"
  | "outcome_unknown"
  | "succeeded"
  | "rejected"
  | "failed";

export interface OperationRecord {
  key: string;
  kind: string;
  target: string;
  payloadFingerprint: string;
  status: OperationOutcome;
  createdAt: string;
  updatedAt: string;
  requestId?: string;
  deploymentId?: string;
  operationStatus?: string;
  errorCode?: string;
  manualRetryRequired?: boolean;
}

interface OperationRegistryState {
  operations: Record<string, OperationRecord>;
  begin: (operation: Omit<OperationRecord, "status" | "createdAt" | "updatedAt">) => void;
  transition: (key: string, status: OperationOutcome, patch?: Partial<OperationRecord>) => void;
  remove: (key: string) => void;
  pruneTerminal: () => void;
}

export const OPERATION_TERMINAL_RETENTION_MS = 7 * 24 * 60 * 60 * 1_000;
export const OPERATION_TERMINAL_LIMIT = 100;
export const OPERATION_UNRESOLVED_LIMIT = 100;
const TERMINAL_OUTCOMES = new Set<OperationOutcome>(["succeeded", "rejected", "failed"]);
const UNRESOLVED_OUTCOMES = new Set<OperationOutcome>([
  "prepared",
  "submitting",
  "outcome_unknown",
]);

export function unresolvedOperationCount(operations: Record<string, OperationRecord>): number {
  return Object.values(operations).filter((operation) => UNRESOLVED_OUTCOMES.has(operation.status)).length;
}

export function normalizePersistedOperations(
  operations: Record<string, OperationRecord>,
  now = Date.now(),
): Record<string, OperationRecord> {
  const unresolved: OperationRecord[] = [];
  const terminal: OperationRecord[] = [];
  for (const operation of Object.values(operations)) {
    const normalized = operation.status === "submitting"
      ? {
          ...operation,
          status: "outcome_unknown" as const,
          errorCode: operation.errorCode ?? "PAGE_RELOADED_DURING_SUBMIT",
        }
      : operation;
    if (!TERMINAL_OUTCOMES.has(normalized.status)) {
      unresolved.push(normalized);
      continue;
    }
    const updatedAt = Date.parse(normalized.updatedAt || normalized.createdAt);
    if (Number.isFinite(updatedAt) && now - updatedAt <= OPERATION_TERMINAL_RETENTION_MS) {
      terminal.push(normalized);
    }
  }
  terminal.sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt));
  return Object.fromEntries(
    [...unresolved, ...terminal.slice(0, OPERATION_TERMINAL_LIMIT)].map((operation) => [
      operation.key,
      operation,
    ]),
  );
}

export function serializeOperationRegistry(operations: Record<string, OperationRecord>): string {
  return JSON.stringify(
    {
      exportedAt: new Date().toISOString(),
      unresolvedCount: unresolvedOperationCount(operations),
      operations: Object.values(operations).sort((left, right) =>
        left.createdAt.localeCompare(right.createdAt)),
    },
    null,
    2,
  );
}

function canonicalize(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalize(record[key])}`)
    .join(",")}}`;
}

export async function payloadFingerprint(payload: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalize(payload));
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function newIdempotencyKey(prefix = "web"): string {
  return `${prefix}-${globalThis.crypto.randomUUID()}`;
}

export const useOperationRegistry = create<OperationRegistryState>()(
  persist(
    (set) => ({
      operations: {},
      begin: (operation) =>
        set((state) => {
          if (unresolvedOperationCount(state.operations) >= OPERATION_UNRESOLVED_LIMIT) {
            throw new Error("OPERATION_RECOVERY_LIMIT_REACHED");
          }
          const now = new Date().toISOString();
          return {
            operations: {
              ...state.operations,
              [operation.key]: {
                ...operation,
                status: "submitting",
                createdAt: now,
                updatedAt: now,
              },
            },
          };
        }),
      transition: (key, status, patch = {}) =>
        set((state) => {
          const current = state.operations[key];
          if (!current) return state;
          return {
            operations: {
              ...state.operations,
              [key]: { ...current, ...patch, key, status, updatedAt: new Date().toISOString() },
            },
          };
        }),
      remove: (key) =>
        set((state) => {
          const operations = { ...state.operations };
          delete operations[key];
          return { operations };
        }),
      pruneTerminal: () =>
        set((state) => ({
          operations: normalizePersistedOperations(state.operations),
        })),
    }),
    {
      name: "nids-operation-registry-v1",
      version: 2,
      partialize: (state) => ({ operations: state.operations }),
      migrate: (persisted) => persisted,
      merge: (persisted, current) => {
        const restored = persisted as Partial<OperationRegistryState>;
        return {
          ...current,
          ...restored,
          operations: normalizePersistedOperations(restored.operations ?? {}),
        };
      },
    }
  )
);
