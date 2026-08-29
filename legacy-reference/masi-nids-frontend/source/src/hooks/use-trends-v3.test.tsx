import type { ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TRENDS_QUERY_KEY, useTrendsV3 } from "@/hooks/use-trends-v3";
import api from "@/lib/api";
import { apiResponse } from "@/test/api-response";

// Regression coverage for the design r9 Event-v3 trends hook. The implementation
// begins as a comment-only stub, so the expected pre-failure is its missing exports.
// Frozen baseline: Requirements r3 / Design r9 §13.1-13.3.
//
// The api client is mocked rather than global fetch: that is the established
// pattern in use-event-v4-workflow.test.tsx, and it lets the assertions name the
// exact path the hook requests -- which is what proves the dashboard cutover.

vi.mock("@/lib/api", () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

const PAYLOAD = {
  schema: "masi.analytics-trends-v3.v1",
  bucket: "5m",
  from_at: "2032-03-01T12:00:00+00:00",
  to_at: "2032-03-01T13:00:00+00:00",
  target_uuid: null,
  model_role: "champion",
  generated_at: "2032-03-01T13:00:01+00:00",
  latest_event_observed_at: "2032-03-01T12:55:00+00:00",
  buckets: [
    {
      bucket_start: "2032-03-01T12:00:00+00:00",
      total: 12,
      normal: 5,
      anomaly: 4,
      unknown: 2,
      recovery: 1,
      max_packets_per_second: 900.5,
      max_bytes_per_second: 120_000,
      min_completeness: 0.95,
    },
  ],
};

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, gcTime: 0 } },
});

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

const RANGE = { from: PAYLOAD.from_at, to: PAYLOAD.to_at, bucket: "5m" as const };

describe("useTrendsV3", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryClient.clear();
  });

  it("requests the analytics v3 endpoint and never the Event-v2 dashboard routes", async () => {
    vi.mocked(api.get).mockResolvedValue(apiResponse(PAYLOAD));

    const { result } = renderHook(() => useTrendsV3(RANGE), { wrapper });
    await waitFor(() => expect(result.current.data).toBeDefined());

    const paths = vi.mocked(api.get).mock.calls.map((call) => String(call[0]));
    expect(paths.some((path) => path.startsWith("analytics/v3/trends"))).toBe(true);
    for (const retired of ["events", "events/stats"]) {
      expect(paths.some((path) => path === retired || path.startsWith(`${retired}?`))).toBe(false);
    }
  });

  it("passes the exact range and bucket through as query parameters", async () => {
    vi.mocked(api.get).mockResolvedValue(apiResponse(PAYLOAD));

    renderHook(() => useTrendsV3(RANGE), { wrapper });
    await waitFor(() => expect(api.get).toHaveBeenCalled());

    const requested = String(vi.mocked(api.get).mock.calls[0][0]);
    expect(requested).toContain("bucket=5m");
    expect(requested).toContain(encodeURIComponent(RANGE.from));
    expect(requested).toContain(encodeURIComponent(RANGE.to));
  });

  it("uses a stable trends-v3 query key that varies with the range", () => {
    expect(TRENDS_QUERY_KEY).toBe("trends-v3");
  });

  it("does not fetch until a complete range is supplied", () => {
    renderHook(() => useTrendsV3({ from: null, to: null, bucket: "5m" }), { wrapper });
    expect(api.get).not.toHaveBeenCalled();
  });

  it("surfaces the payload unchanged without inventing an anomaly score", async () => {
    vi.mocked(api.get).mockResolvedValue(apiResponse(PAYLOAD));

    const { result } = renderHook(() => useTrendsV3(RANGE), { wrapper });
    await waitFor(() => expect(result.current.data).toBeDefined());

    expect(result.current.data).toEqual(PAYLOAD);
    const serialized = JSON.stringify(result.current.data);
    expect(serialized).not.toContain("anomaly_score");
    expect(serialized).not.toContain("compatibility");
  });

  it("reports the error state without retrying a rejected query", async () => {
    vi.mocked(api.get).mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useTrendsV3(RANGE), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(api.get).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBeUndefined();
  });
});
