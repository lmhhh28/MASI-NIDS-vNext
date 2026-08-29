import { describe, expect, it } from "vitest";

import { metadataFor, p4Status, runtimeConsoleStatus, workflowStatus } from "@/lib/status-metadata";

describe("status metadata", () => {
  it("never treats a P4 unknown outcome as terminal success or ordinary failure", () => {
    expect(p4Status.unknown).toMatchObject({ tone: "warning", terminal: false });
    expect(p4Status.unknown.actions).toContain("resolve");
  });

  it("marks completed workflow states as terminal", () => {
    expect(workflowStatus.completed.terminal).toBe(true);
    expect(metadataFor(workflowStatus, "vendor_state").terminal).toBe(false);
  });

  it("never maps unknown, HOLD, degraded, or restart-required runtime state to success", () => {
    for (const state of ["unknown", "hold", "degraded", "restart_required"]) {
      expect(runtimeConsoleStatus[state].tone).not.toBe("success");
      expect(runtimeConsoleStatus[state].actions).toEqual([]);
    }
  });
});
