"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import api from "@/lib/api";
import type { AnalysisTraceV1Response } from "@/types/api";

// AgentAnalysisTraceV1 read-only query hook.
// Frozen baseline: Requirements r3 / Design r9 §13.1, §8.5, §18.4.
// Contract coverage: src/hooks/use-analysis-trace-v1.test.tsx
//
// Reads the frozen topology and append-only redacted trace events from the
// backend. The first request may use attempt="current"; the response
// resolves a concrete attempt number that subsequent incremental requests
// MUST pin. The Sheet-visible flag controls the 5-second single-flight
// poll. Once trace_state becomes "terminal", a final fetch is issued and
// polling stops. Reading trace never triggers LLM, workflow admission,
// review, outbox, or P4 mutation. Retry attempts produce separate traces
// and are never concatenated.

export const ANALYSIS_TRACE_QUERY_KEY = "analysis-trace-v1";

export interface AnalysisTraceV1Params {
  workflowId: string;
  revisionId: string;
  nodeRunId: string;
  initialAttempt?: "current" | number;
  afterSequenceNo?: number;
  sheetVisible: boolean;
}

export type ResolvedAttempt = "current" | number;

export function useAnalysisTraceV1(params: AnalysisTraceV1Params | null) {
  // Track the resolved attempt: starts as "current" or the caller-specified
  // number; after the first response, pins to the concrete attempt number.
  const [resolvedAttempt, setResolvedAttempt] = useState<ResolvedAttempt>(
    params?.initialAttempt ?? "current",
  );
  const [afterSequenceNo, setAfterSequenceNo] = useState<number | undefined>(
    params?.afterSequenceNo,
  );

  const enabled = params !== null && params.sheetVisible;
  // The query key is the cache identity: workflow/revision/node/attempt.
  // afterSequenceNo is a cursor that advances within the same cache entry
  // — keeping it OUT of the key prevents data flicker when the cursor moves.
  // React Query polls every 5s; on the next tick the queryFn closure reads
  // the latest cursor and requests only new events.
  const queryKey = enabled
    ? [
        ANALYSIS_TRACE_QUERY_KEY,
        params!.workflowId,
        params!.revisionId,
        params!.nodeRunId,
        resolvedAttempt,
      ]
    : [ANALYSIS_TRACE_QUERY_KEY];

  // Stash the cursor in a ref so the queryFn always reads the latest value
  // without needing it in the query key.  The ref is updated in an effect
  // (not during render) to comply with the React rules of hooks.
  const cursorRef = useRef(afterSequenceNo);
  useEffect(() => {
    cursorRef.current = afterSequenceNo;
  }, [afterSequenceNo]);

  const query = useQuery<AnalysisTraceV1Response>({
    queryKey,
    queryFn: ({ signal }) => {
      const cursor = cursorRef.current;
      const search = new URLSearchParams();
      search.append("attempt", String(resolvedAttempt));
      if (cursor !== undefined) {
        search.append("after_sequence_no", String(cursor));
      }
      search.append("limit", "200");
      const url = `workflows/${params!.workflowId}/revisions/${params!.revisionId}/nodes/${params!.nodeRunId}/analysis-trace?${search.toString()}`;
      return api
        .get<AnalysisTraceV1Response>(url, { signal })
        .then((response) => response.data);
    },
    enabled,
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
    retry: false,
  });

  // Pin the resolved attempt and advance the incremental cursor after each
  // response. Both are React Query pagination transitions: the query data
  // drives the next request's key, which requires updating state from the
  // effect. This is the canonical React Query dependent-query pattern.
  useEffect(() => {
    if (!query.data) return;
    if (resolvedAttempt === "current" && typeof query.data.attempt === "number" && query.data.attempt > 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setResolvedAttempt(query.data.attempt);
    }
    const next = query.data.next_after_sequence_no;
    if (typeof next === "number" && next > (afterSequenceNo ?? -1)) {
      setAfterSequenceNo(next);
    }
  }, [query.data, resolvedAttempt, afterSequenceNo]);

  // Stop polling when terminal.
  const isTerminal = query.data?.trace_state === "terminal";

  return {
    data: query.data,
    isLoading: query.isLoading,
    isError: query.isError,
    error: query.error,
    refetch: query.refetch,
    isTerminal,
    resolvedAttempt,
    afterSequenceNo,
  };
}