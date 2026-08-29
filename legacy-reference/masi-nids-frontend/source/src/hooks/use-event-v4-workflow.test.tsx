import type { ReactNode } from "react";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useAdmitEventV4Workflow } from "@/hooks/use-events-v3";
import api from "@/lib/api";
import { ApiClientError } from "@/lib/http-error";
import type { WorkflowResponse } from "@/types/api";

vi.mock("@/lib/api", () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

const workflow: WorkflowResponse = {
  id: "workflow-v4-1",
  revision: 0,
  status: "admitted",
  template_name: "inspect_only",
  requested_intent: null,
  actor_user_id: "actor-1",
  source_event_id: "018f5f41-7b58-7cc2-8f0f-9f855654f415",
  alert_group_id: null,
  review_packet_hash: null,
  approved_at: null,
  approval_user_id: null,
  created_at: "2026-07-19T00:00:00Z",
  updated_at: "2026-07-19T00:00:00Z",
  artifacts: [],
  agent_steps: [],
  p4_rule_plans: [],
};

const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

describe("useAdmitEventV4Workflow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryClient.clear();
  });

  it("uses one key and becomes permanently query-only after an unknown mutation", async () => {
    vi.mocked(api.post).mockRejectedValueOnce(
      new ApiClientError("timeout", { code: "ECONNABORTED", config: { method: "post" } }),
    );
    vi.mocked(api.get)
      .mockRejectedValueOnce(
        new ApiClientError("not found", {
          response: { status: 404 },
          config: { method: "get" },
        }),
      )
      .mockResolvedValueOnce({ data: workflow, status: 200, headers: new Headers() });

    const { result } = renderHook(
      () => useAdmitEventV4Workflow("018f5f41-7b58-7cc2-8f0f-9f855654f415"),
      { wrapper },
    );
    await act(async () => {
      await expect(result.current.mutateAsync({ locale: "en-US" })).rejects.toThrow(
        "WORKFLOW_ADMISSION_OUTCOME_UNKNOWN",
      );
    });
    await act(async () => {
      await expect(result.current.mutateAsync({ locale: "en-US" })).resolves.toEqual(workflow);
    });

    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.get).toHaveBeenCalledTimes(2);
    const postHeaders = vi.mocked(api.post).mock.calls[0][2]?.headers as Record<string, string>;
    const firstGetHeaders = vi.mocked(api.get).mock.calls[0][1]?.headers as Record<string, string>;
    const secondGetHeaders = vi.mocked(api.get).mock.calls[1][1]?.headers as Record<string, string>;
    expect(firstGetHeaders["Idempotency-Key"]).toBe(postHeaders["Idempotency-Key"]);
    expect(secondGetHeaders["Idempotency-Key"]).toBe(postHeaders["Idempotency-Key"]);
  });

  it("does not query or retry after a definitive policy rejection", async () => {
    const rejection = new ApiClientError("blocked", {
      response: { status: 409 },
      config: { method: "post" },
    });
    vi.mocked(api.post).mockRejectedValueOnce(rejection);
    const { result } = renderHook(
      () => useAdmitEventV4Workflow("018f5f41-7b58-7cc2-8f0f-9f855654f415"),
      { wrapper },
    );
    await act(async () => {
      await expect(result.current.mutateAsync({})).rejects.toBe(rejection);
    });
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.get).not.toHaveBeenCalled();
  });
});
