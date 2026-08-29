import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { DemoRunProjectionResponse } from "@/types/api";

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

import api from "@/lib/api";
import { DEMO_RUN_PROJECTION_QUERY_KEY, useDemoRunProjection } from "@/hooks/use-demo-run-projection";
import { apiResponse } from "@/test/api-response";

const mockedGet = vi.mocked(api.get);
const mockedPost = vi.mocked(api.post);

let queryClient: QueryClient;

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

const PROJECTION_RESPONSE: DemoRunProjectionResponse = {
  schema: "masi.demo-run-projection.v1",
  operation_id: "op-123",
  projection: {
    demo_operation_id: "op-123",
    source_run_id: "run-1",
    producer_status: "succeeded",
    overall_progress: "3/5",
    overall_complete: false,
  },
};

describe("useDemoRunProjection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  });

  it("uses a stable demo-run-projection query key", () => {
    expect(DEMO_RUN_PROJECTION_QUERY_KEY).toBe("demo-run-projection");
  });

  it("does not fetch when operationId is null", async () => {
    renderHook(() => useDemoRunProjection(null), { wrapper });
    expect(mockedGet).not.toHaveBeenCalled();
  });

  it("fetches demo/runs/{id}/projection", async () => {
    mockedGet.mockResolvedValueOnce(apiResponse(PROJECTION_RESPONSE));
    renderHook(() => useDemoRunProjection("op-123"), { wrapper });
    await waitFor(() => expect(mockedGet).toHaveBeenCalledTimes(1));
    const url = mockedGet.mock.calls[0][0] as string;
    expect(url).toContain("demo/runs/op-123/projection");
  });

  it("does not call any POST", () => {
    renderHook(() => useDemoRunProjection("op-123"), { wrapper });
    expect(mockedPost).not.toHaveBeenCalled();
  });

  it("does not request retired Event-v2 endpoints", async () => {
    mockedGet.mockResolvedValue(apiResponse(PROJECTION_RESPONSE));
    renderHook(() => useDemoRunProjection("op-123"), { wrapper });
    await waitFor(() => expect(mockedGet).toHaveBeenCalled());
    for (const call of mockedGet.mock.calls) {
      const url = call[0] as string;
      expect(url).not.toMatch(/^\/?events(\?|$)/);
      expect(url).not.toMatch(/^\/?events\/stats/);
    }
  });
});
