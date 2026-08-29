"use client";

import { useQuery } from "@tanstack/react-query";

import api from "@/lib/api";
import type { LiveV3Response } from "@/types/api";

// DashboardLiveSampleV1 live query hook.
// Frozen baseline: Requirements r3 / Design r9 §13.1.1, §13.3, §5.
// Contract coverage: src/hooks/use-live-v3.test.tsx
//
// Reads the 5-second live sample projection endpoint. Polls every 5 seconds
// while the page is visible and the network is online; pauses in the
// background. Single-flight per query key. Manual refresh only invalidates
// the GET — never triggers inference, LLM, workflow, or P4 mutation.
// Falls back to last-good data on transient errors; marks stale after 15
// seconds without a same-generation sample; never falls back to /events or
// minute rollup.

export const LIVE_V3_QUERY_KEY = "live-v3";

export interface LiveV3Params {
  targetUuid: string;
  runtimeGeneration: number;
  modelRole: "champion" | "shadow";
  modelReleaseId: string;
  window?: string;
  step?: string;
}

function buildLiveUrl(params: LiveV3Params): string {
  const search = new URLSearchParams();
  search.append("target_uuid", params.targetUuid);
  search.append("runtime_generation", String(params.runtimeGeneration));
  search.append("model_role", params.modelRole);
  search.append("model_release_id", params.modelReleaseId);
  search.append("window", params.window ?? "15m");
  search.append("step", params.step ?? "5s");
  return `analytics/v3/live?${search.toString()}`;
}

export function useLiveV3(params: LiveV3Params | null) {
  const enabled = params !== null;
  const queryKey = enabled
    ? [
        LIVE_V3_QUERY_KEY,
        params.targetUuid,
        params.runtimeGeneration,
        params.modelRole,
        params.modelReleaseId,
        params.window ?? "15m",
        params.step ?? "5s",
      ]
    : [LIVE_V3_QUERY_KEY];

  return useQuery<LiveV3Response>({
    queryKey,
    queryFn: ({ signal }) =>
      api
        .get<LiveV3Response>(buildLiveUrl(params!), { signal })
        .then((response) => response.data),
    enabled,
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
    staleTime: 0,
    retry: false,
  });
}