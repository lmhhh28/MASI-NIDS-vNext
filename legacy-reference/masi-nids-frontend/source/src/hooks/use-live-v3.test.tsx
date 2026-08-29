import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveV3Response } from "@/types/api";

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

import api from "@/lib/api";
import { LIVE_V3_QUERY_KEY, useLiveV3 } from "@/hooks/use-live-v3";
import { apiResponse } from "@/test/api-response";

const mockedGet = vi.mocked(api.get);
const mockedPost = vi.mocked(api.post);

let queryClient: QueryClient;

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

const PARAMS = {
  targetUuid: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  runtimeGeneration: 3,
  modelRole: "champion" as const,
  modelReleaseId: "demo-ae-v1",
};

const LIVE_RESPONSE: LiveV3Response = {
  schema: "masi.dashboard-live.v1",
  generated_at: "2026-08-02T03:20:50Z",
  target_uuid: PARAMS.targetUuid,
  runtime_generation: 3,
  model_role: "champion",
  model_release_id: "demo-ae-v1",
  window: "15m",
  step: "5s",
  watermark_at: "2026-08-02T03:20:50Z",
  latest_sample_at: "2026-08-02T03:20:50Z",
  freshness: { status: "known", age_seconds: 1, stale_after_seconds: 15, reason_code: null },
  samples: [],
  event_markers: [],
  current_incident: null,
};

describe("useLiveV3", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  });

  it("uses a stable live-v3 query key", () => {
    expect(LIVE_V3_QUERY_KEY).toBe("live-v3");
  });

  it("does not fetch when params is null", async () => {
    renderHook(() => useLiveV3(null), { wrapper });
    expect(mockedGet).not.toHaveBeenCalled();
  });

  it("fetches analytics/v3/live with required query params", async () => {
    mockedGet.mockResolvedValueOnce(apiResponse(LIVE_RESPONSE));
    renderHook(() => useLiveV3(PARAMS), { wrapper });
    await waitFor(() => expect(mockedGet).toHaveBeenCalledTimes(1));
    const url = mockedGet.mock.calls[0][0] as string;
    expect(url).toContain("analytics/v3/live");
    expect(url).toContain("target_uuid=");
    expect(url).toContain("runtime_generation=3");
    expect(url).toContain("model_role=champion");
    expect(url).toContain("model_release_id=demo-ae-v1");
    expect(url).toContain("window=15m");
    expect(url).toContain("step=5s");
  });

  it("uses a query key that varies with generation (no cross-fence cache)", () => {
    const { rerender } = renderHook(({ p }) => useLiveV3(p), {
      wrapper,
      initialProps: { p: PARAMS },
    });
    rerender({ p: { ...PARAMS, runtimeGeneration: 42 } });
    // Two different enabled states → the query key must differ
    expect(mockedGet).toHaveBeenCalled();
    const urls = mockedGet.mock.calls.map((c) => c[0] as string);
    expect(urls.some((u) => u.includes("runtime_generation=3"))).toBe(true);
    expect(urls.some((u) => u.includes("runtime_generation=42"))).toBe(true);
  });

  it("surfaces the response payload without injecting retired fields", async () => {
    mockedGet.mockResolvedValueOnce(apiResponse(LIVE_RESPONSE));
    const { result } = renderHook(() => useLiveV3(PARAMS), { wrapper });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data?.schema).toBe("masi.dashboard-live.v1");
    expect(JSON.stringify(result.current.data)).not.toContain("anomaly_score");
    expect(JSON.stringify(result.current.data)).not.toContain("compatibility");
  });

  it("does not call any POST, PUT, PATCH, or DELETE", () => {
    renderHook(() => useLiveV3(PARAMS), { wrapper });
    expect(mockedPost).not.toHaveBeenCalled();
  });

  it("does not request retired Event-v2 endpoints", async () => {
    mockedGet.mockResolvedValue(apiResponse(LIVE_RESPONSE));
    renderHook(() => useLiveV3(PARAMS), { wrapper });
    await waitFor(() => expect(mockedGet).toHaveBeenCalled());
    for (const call of mockedGet.mock.calls) {
      const url = call[0] as string;
      expect(url).not.toMatch(/^\/?events(\?|$)/);
      expect(url).not.toMatch(/^\/?events\/stats/);
    }
  });

  it("reports isError without retry when the fetch is rejected", async () => {
    mockedGet.mockRejectedValueOnce(new Error("network"));
    const { result } = renderHook(() => useLiveV3(PARAMS), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(mockedGet).toHaveBeenCalledTimes(1);
  });
});
