"use client";

import { useQuery } from "@tanstack/react-query";

import api from "@/lib/api";
import type { DemoRunProjectionResponse } from "@/types/api";

// Demo run projection read-only hook.
// Frozen baseline: Requirements r3 / Design r9 §13.1, §7.4.
//
// Projects the cross-link progress of a durable demo operation
// (producer → segment → ACK → Event → Incident). Polls every 5 seconds
// while the page is visible and the operation is not yet complete.
// Read-only: never modifies demo operation state.

export const DEMO_RUN_PROJECTION_QUERY_KEY = "demo-run-projection";

export function useDemoRunProjection(operationId: string | null) {
  const enabled = operationId !== null && operationId.trim().length > 0;
  const queryKey = enabled
    ? [DEMO_RUN_PROJECTION_QUERY_KEY, operationId]
    : [DEMO_RUN_PROJECTION_QUERY_KEY];

  return useQuery<DemoRunProjectionResponse>({
    queryKey,
    queryFn: ({ signal }) =>
      api
        .get<DemoRunProjectionResponse>(
          `demo/runs/${encodeURIComponent(operationId!)}/projection`,
          { signal },
        )
        .then((response) => response.data),
    enabled,
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
    retry: false,
  });
}