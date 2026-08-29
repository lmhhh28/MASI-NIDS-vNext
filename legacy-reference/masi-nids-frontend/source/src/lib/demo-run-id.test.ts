import { describe, expect, it } from "vitest";

import { createDemoOnlineRunId, normalizeDemoRunIdBase } from "@/lib/demo-run-id";

const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const FIXED_NOW = new Date(Date.UTC(2026, 7, 2, 12, 34, 56));

describe("demo run IDs", () => {
  it("normalizes operator-facing bases into backend-safe path components", () => {
    expect(normalizeDemoRunIdBase("  online balanced / demo  ")).toBe("online-balanced-demo");
    expect(normalizeDemoRunIdBase("../")).toBe("online-demo");
  });

  it("creates a safe unique online run ID from the configured base", () => {
    const id = createDemoOnlineRunId("online balanced / demo", {
      now: FIXED_NOW,
      randomSuffix: () => "abc-123456789",
    });

    expect(id).toBe("online-balanced-demo-20260802t123456z-abc12345");
    expect(id).toMatch(SAFE_RUN_ID);
  });

  it("keeps long generated IDs within the backend SafeRunId limit", () => {
    const id = createDemoOnlineRunId("x".repeat(200), {
      now: FIXED_NOW,
      randomSuffix: () => "deadbeef",
    });

    expect(id).toHaveLength(128);
    expect(id).toMatch(SAFE_RUN_ID);
    expect(id.endsWith("-20260802t123456z-deadbeef")).toBe(true);
  });
});
