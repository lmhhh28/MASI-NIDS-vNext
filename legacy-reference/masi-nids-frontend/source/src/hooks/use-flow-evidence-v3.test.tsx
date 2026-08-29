import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { FlowEvidenceV3ListResponse } from "@/types/api";

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

import api from "@/lib/api";
import { FLOW_EVIDENCE_V3_QUERY_KEY, useFlowEvidenceV3 } from "@/hooks/use-flow-evidence-v3";
import { apiResponse } from "@/test/api-response";

const mockedGet = vi.mocked(api.get);
const mockedPost = vi.mocked(api.post);

let queryClient: QueryClient;

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

const EMPTY_RESPONSE: FlowEvidenceV3ListResponse = {
  schema: "masi.flow-evidence-v3-list.v1",
  event_id: "11111111-1111-1111-1111-111111111111",
  items: [],
  count: 0,
};

describe("useFlowEvidenceV3", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  });

  it("uses a stable flow-evidence-v3 query key", () => {
    expect(FLOW_EVIDENCE_V3_QUERY_KEY).toBe("flow-evidence-v3");
  });

  it("does not fetch when eventId is null", async () => {
    renderHook(() => useFlowEvidenceV3(null), { wrapper });
    expect(mockedGet).not.toHaveBeenCalled();
  });

  it("fetches flow-evidence/v3 with the event_id query param", async () => {
    mockedGet.mockResolvedValueOnce(apiResponse(EMPTY_RESPONSE));
    renderHook(() => useFlowEvidenceV3("11111111-1111-1111-1111-111111111111"), { wrapper });
    await waitFor(() => expect(mockedGet).toHaveBeenCalledTimes(1));
    const url = mockedGet.mock.calls[0][0] as string;
    expect(url).toContain("flow-evidence/v3");
    expect(url).toContain("event_id=11111111-1111-1111-1111-111111111111");
  });

  it("surfaces an empty items list without error (wizard can show collect path)", async () => {
    mockedGet.mockResolvedValueOnce(apiResponse(EMPTY_RESPONSE));
    const { result } = renderHook(() => useFlowEvidenceV3("11111111-1111-1111-1111-111111111111"), { wrapper });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data?.items).toEqual([]);
    expect(result.current.data?.count).toBe(0);
    expect(result.current.isError).toBe(false);
  });

  it("does not call any POST", () => {
    renderHook(() => useFlowEvidenceV3("11111111-1111-1111-1111-111111111111"), { wrapper });
    expect(mockedPost).not.toHaveBeenCalled();
  });

  it("does not request retired Event-v2 endpoints", async () => {
    mockedGet.mockResolvedValue(apiResponse(EMPTY_RESPONSE));
    renderHook(() => useFlowEvidenceV3("11111111-1111-1111-1111-111111111111"), { wrapper });
    await waitFor(() => expect(mockedGet).toHaveBeenCalled());
    for (const call of mockedGet.mock.calls) {
      const url = call[0] as string;
      expect(url).not.toMatch(/^\/?events(\?|$)/);
      expect(url).not.toMatch(/^\/?events\/stats/);
    }
  });
});
