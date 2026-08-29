"use client";

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useRuntimeConfig } from "@/hooks/use-config";
import type { RuntimeConfig } from "@/types/api";

export type RuntimeCapability = "read" | "validate" | "workflow" | "p4" | "admin";
export type RuntimeSafetyReason =
  | "loading"
  | "error"
  | "stale"
  | "workflow_maintenance"
  | "p4_maintenance";

export interface RuntimeSafetyState {
  allowed: boolean;
  reason?: RuntimeSafetyReason;
}

export function evaluateRuntimeSafety(
  capability: RuntimeCapability,
  state: {
    data?: RuntimeConfig;
    dataUpdatedAt?: number;
    status?: "pending" | "error" | "success";
  },
  now = Date.now(),
): RuntimeSafetyState {
  if (capability === "read" || capability === "validate") return { allowed: true };
  if (state.status === "error") return { allowed: false, reason: "error" };
  if (!state.data || state.status !== "success") return { allowed: false, reason: "loading" };
  if (!state.dataUpdatedAt || now - state.dataUpdatedAt > 30_000) {
    return { allowed: false, reason: "stale" };
  }
  if (capability === "workflow" && state.data.workflow_maintenance) {
    return { allowed: false, reason: "workflow_maintenance" };
  }
  if (capability === "p4" && state.data.p4_maintenance) {
    return { allowed: false, reason: "p4_maintenance" };
  }
  return { allowed: true };
}

export function useRuntimeSafety(capability: RuntimeCapability) {
  const runtime = useRuntimeConfig();
  const queryClient = useQueryClient();
  const safety = evaluateRuntimeSafety(capability, {
    data: runtime.data,
    dataUpdatedAt: runtime.dataUpdatedAt,
    status: runtime.status,
  });
  const guard = useCallback(() => {
    const current = queryClient.getQueryState<RuntimeConfig>(["runtime-config"]);
    const latest = evaluateRuntimeSafety(capability, {
      data: current?.data,
      dataUpdatedAt: current?.dataUpdatedAt,
      status: current?.status,
    });
    return latest.allowed;
  }, [capability, queryClient]);

  return {
    ...safety,
    guard,
    runtime,
    retry: runtime.refetch,
  };
}
