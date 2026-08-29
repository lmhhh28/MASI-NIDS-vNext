"use client";

import * as React from "react";

/**
 * Per-item lifecycle state. The reducer transitions:
 *  pending → running → success | failed | skipped
 */
export type BulkItemStatus =
  | "pending"
  | "running"
  | "success"
  | "skipped"
  | "failed";

export interface BulkItemResult<T> {
  id: string;
  status: BulkItemStatus;
  data?: T;
  errorCode?: string;
  errorMessage?: string;
  skipReason?: string;
}

export type BulkRunner<I extends { id: string }, T> = (
  item: I,
  signal: AbortSignal,
) => Promise<T | { __skipped: true; reason: string }>;

export interface BulkState<I extends { id: string }, T> {
  status: "idle" | "running" | "done" | "aborted";
  total: number;
  completed: number;
  okCount: number;
  failedCount: number;
  skippedCount: number;
  current?: I;
  results: Record<string, BulkItemResult<T>>;
  startedAt?: number;
  finishedAt?: number;
}

export interface BulkStartResult<T> {
  status: "done" | "aborted";
  results: Record<string, BulkItemResult<T>>;
}

const initialState = <I extends { id: string }, T>(): BulkState<I, T> => ({
  status: "idle",
  total: 0,
  completed: 0,
  okCount: 0,
  failedCount: 0,
  skippedCount: 0,
  results: {},
});

type Action<I extends { id: string }, T> =
  | { type: "start"; items: I[]; now: number }
  | { type: "tick"; id: string; current: I }
  | { type: "result"; id: string; result: BulkItemResult<T> }
  | { type: "abort"; now: number }
  | { type: "finalize"; now: number }
  | { type: "reset" };

function reducer<I extends { id: string }, T>(
  state: BulkState<I, T>,
  action: Action<I, T>,
): BulkState<I, T> {
  switch (action.type) {
    case "start": {
      const results: Record<string, BulkItemResult<T>> = {};
      for (const item of action.items) {
        results[item.id] = { id: item.id, status: "pending" };
      }
      return {
        status: "running",
        total: action.items.length,
        completed: 0,
        okCount: 0,
        failedCount: 0,
        skippedCount: 0,
        results,
        startedAt: action.now,
        finishedAt: undefined,
        current: action.items[0],
      };
    }
    case "tick":
      return {
        ...state,
        current: action.current,
        results: {
          ...state.results,
          [action.id]: { id: action.id, status: "running" },
        },
      };
    case "result": {
      const results = { ...state.results, [action.id]: action.result };
      let okCount = state.okCount;
      let failedCount = state.failedCount;
      let skippedCount = state.skippedCount;
      switch (action.result.status) {
        case "success":
          okCount += 1;
          break;
        case "failed":
          failedCount += 1;
          break;
        case "skipped":
          skippedCount += 1;
          break;
      }
      return {
        ...state,
        results,
        completed: state.completed + 1,
        okCount,
        failedCount,
        skippedCount,
      };
    }
    case "abort":
      return {
        ...state,
        status: "aborted",
        finishedAt: action.now,
        current: undefined,
      };
    case "finalize":
      // Natural completion -- only transitions if still running, so a
      // late abort cannot clobber the aborted status.
      if (state.status !== "running") return state;
      return {
        ...state,
        status: "done",
        finishedAt: action.now,
        current: undefined,
      };
    case "reset":
      return initialState<I, T>();
    default:
      return state;
  }
}

/** Maximum batch size enforced by the hook (concurrency=1, serial). */
export const BULK_HARD_LIMIT = 25;

/**
 * Generic bulk-action orchestrator for the dashboard.
 *
 * Runs `runner` against each `item` strictly sequentially (concurrency=1)
 * to bound backend load and preserve the DirectionalEvidence/P4 operation
 * ordering contracts. Each runner call
 * receives the user-controlled `AbortSignal`; calling `abort()` flips
 * the signal so a cooperating runner can short-circuit and the loop
 * stops issuing new items.
 *
 * The hook does NOT perform any network requests itself — callers
 * supply runners (typically wrapping React Query mutations).
 */
export function useBulkAction<I extends { id: string }, T>() {
  const [state, dispatch] = React.useReducer(
    reducer<I, T>,
    undefined,
    initialState<I, T>,
  );
  const abortRef = React.useRef<AbortController | null>(null);

  const start = React.useCallback(
    async (items: I[], runner: BulkRunner<I, T>): Promise<BulkStartResult<T>> => {
      if (items.length === 0) {
        return { status: "done", results: {} };
      }
      if (items.length > BULK_HARD_LIMIT) {
        throw new Error(
          `Bulk batch exceeds hard limit ${BULK_HARD_LIMIT} (got ${items.length})`,
        );
      }
      // New batch supersedes any previous controller.
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      dispatch({ type: "start", items, now: Date.now() });

      let aborted = false;
      const batchResults: Record<string, BulkItemResult<T>> = {};
      for (const item of items) {
        if (controller.signal.aborted) {
          aborted = true;
          break;
        }
        dispatch({ type: "tick", id: item.id, current: item });
        try {
          const outcome = await runner(item, controller.signal);
          if (
            outcome &&
            typeof outcome === "object" &&
            "__skipped" in outcome &&
            outcome.__skipped === true
          ) {
            const result: BulkItemResult<T> = {
              id: item.id,
              status: "skipped",
              skipReason: outcome.reason,
            };
            batchResults[item.id] = result;
            dispatch({
              type: "result",
              id: item.id,
              result,
            });
          } else {
            const result: BulkItemResult<T> = {
              id: item.id,
              status: "success",
              data: outcome as T,
            };
            batchResults[item.id] = result;
            dispatch({
              type: "result",
              id: item.id,
              result,
            });
          }
        } catch (err: unknown) {
          if (controller.signal.aborted) {
            aborted = true;
            break;
          }
          const result: BulkItemResult<T> = {
            id: item.id,
            status: "failed",
            errorMessage:
              err instanceof Error ? err.message : String(err),
            errorCode:
              err && typeof err === "object" && "code" in err
                ? String((err as { code?: unknown }).code ?? "")
                : undefined,
          };
          batchResults[item.id] = result;
          dispatch({
            type: "result",
            id: item.id,
            result,
          });
        }
      }

      if (aborted) {
        dispatch({ type: "abort", now: Date.now() });
        return { status: "aborted", results: batchResults };
      } else {
        dispatch({ type: "finalize", now: Date.now() });
        return { status: "done", results: batchResults };
      }
    },
    [],
  );

  const abort = React.useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const reset = React.useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    dispatch({ type: "reset" });
  }, []);

  return { state, start, abort, reset };
}
