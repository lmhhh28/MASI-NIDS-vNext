import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AnalysisTraceV1Response } from "@/types/api";

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

import api from "@/lib/api";
import { ANALYSIS_TRACE_QUERY_KEY, useAnalysisTraceV1 } from "@/hooks/use-analysis-trace-v1";
import { apiResponse } from "@/test/api-response";

const mockedGet = vi.mocked(api.get);
const mockedPost = vi.mocked(api.post);

let queryClient: QueryClient;

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

const BASE_PARAMS = {
  workflowId: "wf-1",
  revisionId: "rev-1",
  nodeRunId: "nr-1",
  sheetVisible: true,
};

function traceResponse(
  overrides: Partial<AnalysisTraceV1Response> = {},
): AnalysisTraceV1Response {
  return {
    schema_version: 1,
    workflow_id: "wf-1",
    revision_id: "rev-1",
    node_run_id: "nr-1",
    node_attempt_id: "na-1",
    attempt: 2,
    analysis_run_id: "ar-1",
    trace_id: "trace-1",
    graph_version: "masi-agent-workflow-v2.1",
    graph_topology_sha256: "abc123",
    evidence_bundle_sha256: "def456",
    provider_snapshot_sha256: null,
    prompt_bundle_sha256: null,
    tool_policy_sha256: "tp-1",
    trace_state: "active",
    outer_attempt_status: "running",
    started_at: "2026-08-02T03:20:50Z",
    completed_at: null,
    topology: { nodes: [], edges: [] },
    first_sequence_no: 1,
    last_sequence_no: 10,
    terminal_sequence_no: null,
    events: [],
    next_after_sequence_no: 10,
    trace_complete: false,
    terminal_trace_sha256: null,
    retention_expires_at: "2026-08-09T03:20:50Z",
    generated_at: "2026-08-02T03:20:50Z",
    ...overrides,
  };
}

describe("useAnalysisTraceV1", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  });

  it("uses a stable analysis-trace-v1 query key", () => {
    expect(ANALYSIS_TRACE_QUERY_KEY).toBe("analysis-trace-v1");
  });

  it("does not fetch when params is null", async () => {
    renderHook(() => useAnalysisTraceV1(null), { wrapper });
    expect(mockedGet).not.toHaveBeenCalled();
  });

  it("does not fetch when sheetVisible is false", async () => {
    renderHook(
      () => useAnalysisTraceV1({ ...BASE_PARAMS, sheetVisible: false }),
      { wrapper },
    );
    expect(mockedGet).not.toHaveBeenCalled();
  });

  it("pins the resolved attempt after the first response", async () => {
    mockedGet.mockResolvedValue(apiResponse(traceResponse({ attempt: 2 })));
    const { result } = renderHook(() => useAnalysisTraceV1(BASE_PARAMS), { wrapper });
    await waitFor(() => expect(result.current.resolvedAttempt).toBe(2));
    expect(result.current.resolvedAttempt).not.toBe("current");
  });

  it("uses attempt=current on the first request then the pinned attempt afterward", async () => {
    mockedGet.mockResolvedValue(apiResponse(traceResponse({ attempt: 2 })));
    renderHook(() => useAnalysisTraceV1(BASE_PARAMS), { wrapper });
    await waitFor(() => expect(mockedGet).toHaveBeenCalledTimes(1));
    const firstUrl = mockedGet.mock.calls[0][0] as string;
    expect(firstUrl).toContain("attempt=current");
  });

  it("advances after_sequence_no cursor for incremental reads", async () => {
    mockedGet.mockResolvedValue(apiResponse(traceResponse({ next_after_sequence_no: 15 })));
    const { result } = renderHook(() => useAnalysisTraceV1(BASE_PARAMS), { wrapper });
    await waitFor(() => expect(result.current.data).toBeDefined());
    await waitFor(() => expect(result.current.afterSequenceNo).toBe(15));
  });

  it("surfaces isTerminal when trace_state becomes terminal", async () => {
    mockedGet.mockResolvedValue(
      apiResponse(traceResponse({ trace_state: "terminal", terminal_sequence_no: 10, trace_complete: true })),
    );
    const { result } = renderHook(() => useAnalysisTraceV1(BASE_PARAMS), { wrapper });
    await waitFor(() => expect(result.current.isTerminal).toBe(true));
  });

  it("does not call any POST", () => {
    renderHook(() => useAnalysisTraceV1(BASE_PARAMS), { wrapper });
    expect(mockedPost).not.toHaveBeenCalled();
  });

  it("does not request retired Event-v2 endpoints", async () => {
    mockedGet.mockResolvedValue(apiResponse(traceResponse()));
    renderHook(() => useAnalysisTraceV1(BASE_PARAMS), { wrapper });
    await waitFor(() => expect(mockedGet).toHaveBeenCalled());
    for (const call of mockedGet.mock.calls) {
      const url = call[0] as string;
      expect(url).not.toMatch(/^\/?events(\?|$)/);
      expect(url).not.toMatch(/^\/?events\/stats/);
    }
  });
});
