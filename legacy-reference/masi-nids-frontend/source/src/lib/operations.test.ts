import { beforeEach, describe, expect, it } from "vitest";

import {
  normalizePersistedOperations,
  OPERATION_TERMINAL_LIMIT,
  OPERATION_UNRESOLVED_LIMIT,
  payloadFingerprint,
  useOperationRegistry,
} from "@/lib/operations";

describe("operation registry", () => {
  beforeEach(() => useOperationRegistry.setState({ operations: {} }));

  it("uses a canonical payload fingerprint", async () => {
    expect(await payloadFingerprint({ b: 2, a: { z: 1 } })).toBe(
      await payloadFingerprint({ a: { z: 1 }, b: 2 })
    );
  });

  it("keeps unknown outcomes durable until explicitly resolved", () => {
    const registry = useOperationRegistry.getState();
    registry.begin({
      key: "key-1",
      kind: "p4.apply",
      target: "switch/table",
      payloadFingerprint: "abc",
    });
    registry.transition("key-1", "outcome_unknown", { requestId: "request-1" });
    expect(useOperationRegistry.getState().operations["key-1"]).toMatchObject({
      status: "outcome_unknown",
      requestId: "request-1",
    });
  });

  it("turns an interrupted submit into an unknown outcome on hydration", () => {
    const now = new Date().toISOString();
    const restored = normalizePersistedOperations({
      interrupted: {
        key: "interrupted",
        kind: "p4.apply",
        target: "switch/table",
        payloadFingerprint: "abc",
        status: "submitting",
        createdAt: now,
        updatedAt: now,
      },
    });
    expect(restored.interrupted).toMatchObject({
      status: "outcome_unknown",
      errorCode: "PAGE_RELOADED_DURING_SUBMIT",
    });
  });

  it("retains unknown records without age pruning and bounds recent terminal history", () => {
    const now = Date.now();
    const operations = Object.fromEntries([
      ["unknown-old", {
        key: "unknown-old",
        kind: "p4.apply",
        target: "s/t",
        payloadFingerprint: "u",
        status: "outcome_unknown" as const,
        createdAt: new Date(0).toISOString(),
        updatedAt: new Date(0).toISOString(),
      }],
      ...Array.from({ length: OPERATION_TERMINAL_LIMIT + 5 }, (_, index) => [
        `terminal-${index}`,
        {
          key: `terminal-${index}`,
          kind: "p4.apply",
          target: "s/t",
          payloadFingerprint: String(index),
          status: "succeeded" as const,
          createdAt: new Date(now - index * 1_000).toISOString(),
          updatedAt: new Date(now - index * 1_000).toISOString(),
        },
      ]),
    ]);
    const restored = normalizePersistedOperations(operations, now);
    expect(restored["unknown-old"]).toBeDefined();
    expect(Object.values(restored).filter((item) => item.status === "succeeded"))
      .toHaveLength(OPERATION_TERMINAL_LIMIT);
  });

  it("blocks a new operation when one hundred unresolved records require recovery", () => {
    const now = new Date().toISOString();
    useOperationRegistry.setState({
      operations: Object.fromEntries(
        Array.from({ length: OPERATION_UNRESOLVED_LIMIT }, (_, index) => [
          `unknown-${index}`,
          {
            key: `unknown-${index}`,
            kind: "p4.apply",
            target: "s/t",
            payloadFingerprint: String(index),
            status: "outcome_unknown" as const,
            createdAt: now,
            updatedAt: now,
          },
        ]),
      ),
    });
    expect(() => useOperationRegistry.getState().begin({
      key: "blocked",
      kind: "p4.apply",
      target: "s/t",
      payloadFingerprint: "blocked",
    })).toThrow("OPERATION_RECOVERY_LIMIT_REACHED");
    expect(useOperationRegistry.getState().operations.blocked).toBeUndefined();
  });
});
