"use client";

import { useQuery } from "@tanstack/react-query";

import api from "@/lib/api";

// Event-v3 trends query hook.
// Frozen baseline: Requirements r3 / Design r9 §13.1-13.3.
// Contract coverage: src/hooks/use-trends-v3.test.tsx
//
// Reads the minute-level Event-v3 rollup endpoint. It never touches the
// retired Event-v2 dashboard routes (`/events`, `/events/stats`) and never
// invents an anomaly score or compatibility value.

export const TRENDS_QUERY_KEY = "trends-v3";

export type TrendsBucketToken = "1m" | "5m" | "15m" | "1h";

export interface TrendsV3Range {
  from: string | null;
  to: string | null;
  bucket: TrendsBucketToken;
  targetUuid?: string | null;
  modelRole?: "champion" | "shadow";
}

export interface TrendsBucket {
  bucket_start: string;
  total: number;
  normal: number;
  anomaly: number;
  unknown: number;
  recovery: number;
  max_packets_per_second: number | null;
  max_bytes_per_second: number | null;
  min_completeness: number | null;
}

export interface TrendsV3Response {
  schema: string;
  generated_at: string;
  bucket: TrendsBucketToken;
  model_role: "champion" | "shadow";
  target_uuid: string | null;
  from: string;
  to: string;
  latest_event_observed_at: string | null;
  buckets: TrendsBucket[];
}

function buildTrendsUrl(range: TrendsV3Range): string {
  const search = new URLSearchParams();
  if (range.from) search.append("from", range.from);
  if (range.to) search.append("to", range.to);
  search.append("bucket", range.bucket);
  if (range.targetUuid?.trim()) search.append("target_uuid", range.targetUuid.trim());
  search.append("model_role", range.modelRole ?? "champion");
  return `analytics/v3/trends?${search.toString()}`;
}

export function useTrendsV3(range: TrendsV3Range) {
  const complete = Boolean(range.from && range.to);
  return useQuery<TrendsV3Response>({
    queryKey: [TRENDS_QUERY_KEY, range],
    queryFn: ({ signal }) =>
      api
        .get<TrendsV3Response>(buildTrendsUrl(range), { signal })
        .then((response) => response.data),
    enabled: complete,
    refetchOnWindowFocus: false,
  });
}
