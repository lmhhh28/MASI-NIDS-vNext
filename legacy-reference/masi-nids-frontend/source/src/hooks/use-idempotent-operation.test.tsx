import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useIdempotentOperation } from "@/hooks/use-idempotent-operation";
import { ApiClientError } from "@/lib/http-error";
import { useOperationRegistry } from "@/lib/operations";

interface Payload {
  switchId: string;
  value: number;
}

describe("useIdempotentOperation", () => {
  beforeEach(() => {
    useOperationRegistry.setState({ operations: {} });
  });

  it("persists the key before executing and keeps unknown results recoverable", async () => {
    const execute = vi.fn(async (_payload: Payload, key: string) => {
      expect(useOperationRegistry.getState().operations[key]).toMatchObject({
        status: "submitting",
        kind: "p4.apply",
      });
      return { status: "unknown" };
    });
    const { result } = renderHook(() =>
      useIdempotentOperation<Payload, { status: string }>({
        kind: "p4.apply",
        target: (payload) => payload.switchId,
        execute,
        resultStatus: (response) => response.status,
      }),
    );

    let key = "";
    await act(async () => {
      key = (await result.current.run({ switchId: "s1", value: 1 })).key;
    });
    expect(execute).toHaveBeenCalledTimes(1);
    expect(useOperationRegistry.getState().operations[key]).toMatchObject({
      status: "outcome_unknown",
      operationStatus: "unknown",
    });
  });

  it("classifies a no-response timeout as unknown and a clear 409 as rejected", async () => {
    const timeout = new ApiClientError("timeout", {
      code: "ECONNABORTED",
      config: { method: "post" },
    });
    const conflict = new ApiClientError("conflict", {
      code: "ERR_BAD_RESPONSE",
      config: { method: "post" },
      response: {
        data: { detail: { code: "POLICY_REJECTED" } },
        status: 409,
      },
    });
    const execute = vi.fn()
      .mockRejectedValueOnce(timeout)
      .mockRejectedValueOnce(conflict);
    const { result } = renderHook(() =>
      useIdempotentOperation<Payload, { status: string }>({
        kind: "p4.rollback",
        target: (payload) => payload.switchId,
        execute,
      }),
    );

    await act(async () => {
      await expect(result.current.run({ switchId: "s1", value: 1 }, "recover-timeout-key"))
        .rejects.toBe(timeout);
    });
    expect(useOperationRegistry.getState().operations["recover-timeout-key"].status)
      .toBe("outcome_unknown");

    await act(async () => {
      await expect(result.current.run({ switchId: "s1", value: 2 }, "clear-rejection-key"))
        .rejects.toBe(conflict);
    });
    expect(useOperationRegistry.getState().operations["clear-rejection-key"]).toMatchObject({
      status: "rejected",
      errorCode: "POLICY_REJECTED",
    });
  });

  it("refuses to reuse an existing key with a different payload", async () => {
    const execute = vi.fn().mockResolvedValue({ status: "applied" });
    const { result } = renderHook(() =>
      useIdempotentOperation<Payload, { status: string }>({
        kind: "p4.apply",
        target: (payload) => payload.switchId,
        execute,
      }),
    );
    await act(async () => {
      await result.current.run({ switchId: "s1", value: 1 }, "same-key");
    });
    await act(async () => {
      await expect(result.current.run({ switchId: "s1", value: 2 }, "same-key"))
        .rejects.toThrow("IDEMPOTENCY_KEY_REUSE");
    });
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("does not classify an explicit P4 failure as success", async () => {
    const { result } = renderHook(() =>
      useIdempotentOperation<Payload, { status: string }>({
        kind: "p4.apply",
        target: (payload) => payload.switchId,
        execute: async () => ({ status: "p4_write_failed" }),
        resultStatus: (response) => response.status,
      }),
    );

    await act(async () => {
      await result.current.run({ switchId: "s1", value: 1 }, "failed-key");
    });
    expect(useOperationRegistry.getState().operations["failed-key"]).toMatchObject({
      status: "failed",
      operationStatus: "p4_write_failed",
    });
  });

  it("keeps a mutation 401 prepared under the original key for manual confirmation", async () => {
    const authChanged = new ApiClientError("confirm and retry", {
      code: "AUTH_MUTATION_RETRY_REQUIRED",
      config: { method: "post" },
      response: {
        data: { detail: { code: "AUTH_REQUIRED" } },
        status: 401,
      },
      manualRetryRequired: true,
    });
    const execute = vi.fn().mockRejectedValue(authChanged);
    const { result } = renderHook(() =>
      useIdempotentOperation<Payload, { status: string }>({
        kind: "p4.apply",
        target: (payload) => payload.switchId,
        execute,
      }),
    );

    await act(async () => {
      await expect(result.current.run({ switchId: "s1", value: 1 }, "manual-retry-key"))
        .rejects.toBe(authChanged);
    });
    expect(useOperationRegistry.getState().operations["manual-retry-key"]).toMatchObject({
      status: "prepared",
      manualRetryRequired: true,
    });
    expect(execute).toHaveBeenCalledTimes(1);
  });
});
