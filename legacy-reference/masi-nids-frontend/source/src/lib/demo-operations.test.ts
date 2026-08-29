import { beforeEach, describe, expect, it, vi } from "vitest";

import { submitTrackedDemoOperation } from "@/hooks/use-demo-traffic";
import {
  actorDemoOperations,
  demoOperationStoreKey,
  isDemoOperationActive,
  isDemoOperationTerminal,
  normalizePersistedDemoOperations,
  useDemoOperationRegistry,
  type TrackedDemoOperation,
} from "@/lib/demo-operations";
import type { DemoOperationResponse } from "@/types/api";

const runPayload = {
  duration_seconds: 60,
  profile: "all" as const,
  online_run_id: "online-demo",
  train_run_id: "demo-train",
};

function response(status: DemoOperationResponse["status"]): DemoOperationResponse {
  return {
    operation_id: "operation-run-1",
    kind: "run",
    status,
    phase: status === "succeeded" ? "complete" : "runtime",
    online_run_id: "online-demo",
    attempt_count: 1,
    result: status === "succeeded" ? { completed: true } : {},
    error_code: null,
    created_at: "2026-07-18T00:00:00Z",
    updated_at: `2026-07-18T00:00:0${status === "queued" ? 1 : 2}Z`,
    finished_at: status === "succeeded" ? "2026-07-18T00:00:02Z" : null,
  };
}

describe("demo operation registry", () => {
  beforeEach(() => useDemoOperationRegistry.setState({ operations: {} }));

  it("tracks a long-running run and a stop independently for the same actor", () => {
    const registry = useDemoOperationRegistry.getState();
    registry.begin({
      key: "run-key",
      actorUserId: "admin-1",
      kind: "run",
      onlineRunId: "online-demo",
      payload: runPayload,
    });
    registry.applyResponse("admin-1", "run-key", response("running"));
    registry.begin({
      key: "stop-key",
      actorUserId: "admin-1",
      kind: "stop",
      onlineRunId: "online-demo",
      payload: { online_run_id: "online-demo" },
    });

    const records = actorDemoOperations(useDemoOperationRegistry.getState().operations, "admin-1");
    expect(records).toHaveLength(2);
    expect(records.map((record) => record.kind).sort()).toEqual(["run", "stop"]);
    expect(records.every(isDemoOperationActive)).toBe(true);
    expect(actorDemoOperations(useDemoOperationRegistry.getState().operations, "admin-2")).toEqual([]);
  });

  it("recovers a page reload during POST by resolving the persisted idempotency key", () => {
    const operation: TrackedDemoOperation = {
      key: "lost-response-key",
      actorUserId: "admin-1",
      kind: "cleanup",
      onlineRunId: "online-demo",
      payload: { preview_id: "preview-1", scope_hash: "scope-1" },
      status: "submitting",
      operationId: null,
      phase: "admission",
      attemptCount: 0,
      result: {},
      errorCode: null,
      needsResolve: false,
      manualRetryRequired: false,
      terminalHandled: false,
      createdAt: "2026-07-18T00:00:00Z",
      updatedAt: "2026-07-18T00:00:00Z",
      finishedAt: null,
    };
    const normalized = normalizePersistedDemoOperations({
      [demoOperationStoreKey("admin-1", operation.key)]: operation,
    }, Date.parse("2026-07-18T00:01:00Z"));
    expect(normalized[demoOperationStoreKey("admin-1", operation.key)]).toMatchObject({
      status: "outcome_unknown",
      needsResolve: true,
      manualRetryRequired: false,
      errorCode: "PAGE_RELOADED_DURING_SUBMIT",
    });
  });

  it("keeps terminal handling explicit and idempotent", () => {
    const registry = useDemoOperationRegistry.getState();
    registry.begin({
      key: "run-key",
      actorUserId: "admin-1",
      kind: "run",
      onlineRunId: "online-demo",
      payload: runPayload,
    });
    registry.applyResponse("admin-1", "run-key", response("succeeded"));
    let record = useDemoOperationRegistry.getState().operations[
      demoOperationStoreKey("admin-1", "run-key")
    ];
    expect(isDemoOperationTerminal(record)).toBe(true);
    expect(record.terminalHandled).toBe(false);

    useDemoOperationRegistry.getState().markTerminalHandled("admin-1", "run-key");
    useDemoOperationRegistry.getState().applyResponse("admin-1", "run-key", response("succeeded"));
    record = useDemoOperationRegistry.getState().operations[
      demoOperationStoreKey("admin-1", "run-key")
    ];
    expect(record.terminalHandled).toBe(true);
  });

  it("rejects reuse of an idempotency key with a different payload", () => {
    const registry = useDemoOperationRegistry.getState();
    registry.begin({
      key: "fixed-key",
      actorUserId: "admin-1",
      kind: "run",
      onlineRunId: "online-demo",
      payload: runPayload,
    });
    expect(() => registry.begin({
      key: "fixed-key",
      actorUserId: "admin-1",
      kind: "run",
      onlineRunId: "online-demo",
      payload: { ...runPayload, duration_seconds: 30 },
    })).toThrow("IDEMPOTENCY_KEY_REUSE");
  });

  it("persists submitting before POST settles and reuses one record after a lost response", async () => {
    let settleFetch: ((response: Response) => void) | undefined;
    const pendingFetch = new Promise<Response>((resolve) => {
      settleFetch = resolve;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockReturnValueOnce(pendingFetch);
    const submission = submitTrackedDemoOperation("admin-1", "run", {
      payload: runPayload,
      onlineRunId: "online-demo",
      idempotencyKey: "durable-run-key",
    });
    expect(useDemoOperationRegistry.getState().operations[
      demoOperationStoreKey("admin-1", "durable-run-key")
    ]).toMatchObject({ status: "submitting", needsResolve: false });

    settleFetch?.(new Response(JSON.stringify(response("queued")), {
      status: 202,
      headers: { "content-type": "application/json" },
    }));
    await submission;
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fetchMock.mockRejectedValueOnce(new TypeError("connection lost"));
    await expect(submitTrackedDemoOperation("admin-1", "run", {
      payload: runPayload,
      onlineRunId: "online-demo",
      idempotencyKey: "durable-run-key",
    })).rejects.toThrow("Network request failed");
    expect(useDemoOperationRegistry.getState().operations[
      demoOperationStoreKey("admin-1", "durable-run-key")
    ]).toMatchObject({ status: "outcome_unknown", needsResolve: true });

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(response("queued")), {
      status: 202,
      headers: { "content-type": "application/json" },
    }));
    await submitTrackedDemoOperation("admin-1", "run", {
      payload: runPayload,
      onlineRunId: "online-demo",
      idempotencyKey: "durable-run-key",
    });
    expect(Object.keys(useDemoOperationRegistry.getState().operations)).toHaveLength(1);
  });
});
