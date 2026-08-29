import { describe, expect, it } from "vitest";

import { evaluateRuntimeSafety } from "@/hooks/use-runtime-safety";
import type { RuntimeConfig } from "@/types/api";

const runtime: RuntimeConfig = {
  background_tasks_enabled: true,
  auto_ingest_seconds: 0,
  auto_ingest_enabled: false,
  workflow_engine: "v2",
  workflow_maintenance: false,
  p4_maintenance: false,
};

describe("runtime safety", () => {
  it("fails closed for loading, current errors, and data older than thirty seconds", () => {
    expect(evaluateRuntimeSafety("admin", { status: "pending" })).toMatchObject({ allowed: false });
    expect(evaluateRuntimeSafety("admin", { status: "error", data: runtime, dataUpdatedAt: 100 })).toEqual({
      allowed: false,
      reason: "error",
    });
    expect(evaluateRuntimeSafety("admin", { status: "success", data: runtime, dataUpdatedAt: 1 }, 31_002))
      .toEqual({ allowed: false, reason: "stale" });
  });

  it("applies capability maintenance without blocking read and validate calls", () => {
    const maintained = { ...runtime, workflow_maintenance: true, p4_maintenance: true };
    const state = { status: "success" as const, data: maintained, dataUpdatedAt: 10_000 };
    expect(evaluateRuntimeSafety("workflow", state, 10_001).allowed).toBe(false);
    expect(evaluateRuntimeSafety("p4", state, 10_001).allowed).toBe(false);
    expect(evaluateRuntimeSafety("admin", state, 10_001).allowed).toBe(true);
    expect(evaluateRuntimeSafety("read", { status: "error" }, 10_001).allowed).toBe(true);
    expect(evaluateRuntimeSafety("validate", { status: "error" }, 10_001).allowed).toBe(true);
  });
});
