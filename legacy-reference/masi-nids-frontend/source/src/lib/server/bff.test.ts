// @vitest-environment node

import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  ACCESS_COOKIE,
  BFF_ROUTE_POLICIES,
  CSRF_COOKIE,
  matchBffRoutePolicy,
  proxyBackendRequest,
  resetRuntimePreflightCacheForTests,
  validateMutationRequest,
} from "@/lib/server/bff";

describe("same-origin BFF boundary", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    resetRuntimePreflightCacheForTests();
  });

  it("rejects mutation requests with a foreign origin or stale CSRF token", async () => {
    const foreign = new NextRequest("http://console.test/api/admin/risk-policy", {
      method: "PATCH",
      headers: {
        origin: "https://attacker.test",
        cookie: `${CSRF_COOKIE}=csrf-1`,
        "x-csrf-token": "csrf-1",
      },
    });
    const foreignResponse = validateMutationRequest(foreign);
    expect(foreignResponse?.status).toBe(403);
    await expect(foreignResponse?.json()).resolves.toMatchObject({
      detail: { code: "BFF_ORIGIN_REJECTED" },
    });

    const stale = new NextRequest("http://console.test/api/admin/risk-policy", {
      method: "PATCH",
      headers: {
        origin: "http://console.test",
        cookie: `${CSRF_COOKIE}=csrf-1`,
        "x-csrf-token": "csrf-stale",
      },
    });
    const staleResponse = validateMutationRequest(stale);
    expect(staleResponse?.status).toBe(403);
    await expect(staleResponse?.json()).resolves.toMatchObject({
      detail: { code: "BFF_CSRF_REJECTED" },
    });
  });

  it("drops browser credentials and hop-by-hop headers before injecting the cookie token", async () => {
    const fetchMock = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      if (String(_url).endsWith("/api/config/runtime")) {
        return new Response(JSON.stringify({ workflow_maintenance: false, p4_maintenance: false }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      const headers = new Headers(init?.headers);
      expect(headers.get("authorization")).toBe("Bearer server-access-token");
      expect(headers.get("cookie")).toBeNull();
      expect(headers.get("connection")).toBeNull();
      expect(headers.get("idempotency-key")).toBe("operation-key-1");
      expect(headers.get("if-match")).toBe("7");
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json", "x-request-id": "request-1" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const request = new NextRequest("http://console.test/api/admin/risk-policy", {
      method: "PATCH",
      headers: {
        origin: "http://console.test",
        cookie: `${ACCESS_COOKIE}=server-access-token; ${CSRF_COOKIE}=csrf-1; browser_cookie=drop-me`,
        "x-csrf-token": "csrf-1",
        authorization: "Bearer browser-attacker-token",
        connection: "keep-alive",
        "idempotency-key": "operation-key-1",
        "if-match": "7",
        "content-type": "application/json",
      },
      body: JSON.stringify({ active_rule_cap: 20 }),
    });

    const response = await proxyBackendRequest(request, "admin/risk-policy");
    expect(response.status).toBe(200);
    expect(response.headers.get("x-request-id")).toBe("request-1");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// Regression coverage for the design r9 sections 13.1 and 19 Phase 6 BFF policy.
// Frozen baseline: Requirements r3 / Design r9 §13.1.
// The allowlist is the browser's complete backend surface: a route is not
// reachable because it exists, only because it is declared here with an exact
// method, capability and timeout.
describe("analytics v3 route policy", () => {
  const TRENDS_PATH = "analytics/v3/trends";
  const LIVE_PATH = "analytics/v3/live";

  it("allows read-only GET and HEAD for trends", () => {
    for (const method of ["GET", "HEAD"]) {
      const policy = matchBffRoutePolicy(TRENDS_PATH, method);
      expect(policy, `${method} ${TRENDS_PATH} must be declared`).not.toBeNull();
      expect(policy?.capability).toBe("read");
      expect(policy?.timeoutMs).toBeGreaterThan(0);
    }
  });

  it("allows read-only GET and HEAD for live", () => {
    for (const method of ["GET", "HEAD"]) {
      const policy = matchBffRoutePolicy(LIVE_PATH, method);
      expect(policy, `${method} ${LIVE_PATH} must be declared`).not.toBeNull();
      expect(policy?.capability).toBe("read");
      expect(policy?.timeoutMs).toBeGreaterThan(0);
    }
  });

  it("refuses every mutating method on analytics routes", () => {
    for (const path of [TRENDS_PATH, LIVE_PATH]) {
      for (const method of ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"]) {
        expect(matchBffRoutePolicy(path, method), `${method} ${path} must not be reachable`).toBeNull();
      }
    }
  });

  it("does not open a wildcard under analytics", () => {
    for (const path of [
      "analytics",
      "analytics/v3",
      "analytics/v3/trends/extra",
      "analytics/v3/raw",
      "analytics/v4/trends",
      "analytics/v4/live",
      "analytics/v3/trends;extra",
    ]) {
      expect(matchBffRoutePolicy(path, "GET"), `${path} must stay closed`).toBeNull();
    }
  });

  it("declares exactly two analytics policies with bounded timeouts", () => {
    const analytics = BFF_ROUTE_POLICIES.filter((policy) => policy.path.source.includes("analytics"));
    expect(analytics).toHaveLength(2);
    for (const policy of analytics) {
      expect(policy.methods).toEqual(["GET", "HEAD"]);
      expect(policy.timeoutMs).toBeLessThanOrEqual(30_000);
    }
  });

  it("keeps the retired Event-v2 dashboard endpoints unreachable for mutation", () => {
    // The design r9 dashboard cutover stops reading these; they must never
    // become browser-writable in the process.
    for (const path of ["events", "events/stats"]) {
      for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
        expect(matchBffRoutePolicy(path, method), `${method} ${path} must stay closed`).toBeNull();
      }
    }
  });
});

describe("analysis-trace and flow-evidence v3 route policy", () => {
  const TRACE_PATH = "workflows/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/revisions/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/nodes/cccccccc-cccc-cccc-cccc-cccccccccccc/analysis-trace";
  const FLOW_EVIDENCE_PATH = "flow-evidence/v3";
  const DEMO_PROJECTION_PATH = "demo/runs/op-123/projection";

  it("allows read-only GET and HEAD for analysis-trace", () => {
    for (const method of ["GET", "HEAD"]) {
      const policy = matchBffRoutePolicy(TRACE_PATH, method);
      expect(policy, `${method} must be declared`).not.toBeNull();
      expect(policy?.capability).toBe("read");
    }
  });

  it("allows read-only GET and HEAD for flow-evidence/v3", () => {
    for (const method of ["GET", "HEAD"]) {
      const policy = matchBffRoutePolicy(FLOW_EVIDENCE_PATH, method);
      expect(policy, `${method} must be declared`).not.toBeNull();
      expect(policy?.capability).toBe("read");
    }
  });

  it("allows read-only GET and HEAD for demo run projection", () => {
    for (const method of ["GET", "HEAD"]) {
      const policy = matchBffRoutePolicy(DEMO_PROJECTION_PATH, method);
      expect(policy, `${method} must be declared`).not.toBeNull();
      expect(policy?.capability).toBe("read");
    }
  });

  it("refuses mutating methods on read-only Phase 6 routes", () => {
    for (const path of [TRACE_PATH, FLOW_EVIDENCE_PATH, DEMO_PROJECTION_PATH]) {
      for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
        expect(matchBffRoutePolicy(path, method), `${method} ${path} must stay closed`).toBeNull();
      }
    }
  });
});
