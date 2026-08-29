"use client";

import { useQuery } from "@tanstack/react-query";

import api from "@/lib/api";
import { parseRuntimeStatusProjection } from "@/lib/runtime-status-v1";

export const RUNTIME_STATUS_REFRESH_INTERVAL_MS = 5_000;

export function useRuntimeStatusV1() {
  return useQuery({
    queryKey: ["runtime-status-v1"],
    queryFn: ({ signal }) => api
      .get("/runtime-status/v1", { signal })
      .then((response) => parseRuntimeStatusProjection(response.data)),
    refetchInterval: RUNTIME_STATUS_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    retry: false,
  });
}
