// @vitest-environment node

import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  ACCESS_COOKIE,
  CSRF_COOKIE,
  REFRESH_COOKIE,
  matchBffRoutePolicy,
  proxyBackendRequest,
  resetRuntimePreflightCacheForTests,
} from "@/lib/server/bff";

describe("system-audit BFF security regressions", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    resetRuntimePreflightCacheForTests();
  });

  it("does not proxy token-minting auth endpoints into browser JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          access_token: "must-never-reach-browser",
          refresh_token: "must-never-reach-browser",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const request = new NextRequest("http://console.test/api/auth/login", {
      method: "POST",
      headers: {
        origin: "http://console.test",
        cookie: `${ACCESS_COOKIE}=browser-created-placeholder; ${CSRF_COOKIE}=csrf-audit`,
        "x-csrf-token": "csrf-audit",
        "content-type": "application/json",
      },
      body: JSON.stringify({ username: "admin", password: "valid-password" }),
    });

    const response = await proxyBackendRequest(request, "auth/login");
    expect([403, 404]).toContain(response.status);
    expect(fetchMock).not.toHaveBeenCalled();
    await expect(response.text()).resolves.not.toMatch(/access_token|refresh_token/);
  });

  it.each([
    ["auth/login", "POST"],
    ["auth/refresh", "POST"],
    ["auth/logout", "POST"],
    ["me", "GET"],
    ["session/refresh", "POST"],
    ["mcp/tools/call", "POST"],
    ["admin/users", "PUT"],
    ["unknown/route", "GET"],
    ["events/v3/ingest", "POST"],
    ["runtime-status/v1", "POST"],
    ["internal/runtime-status/v1/telemetry_v3", "GET"],
    ["workflows/event-v4/00000000-0000-4000-8000-000000000000", "DELETE"],
    ["p4/control-v3/targets/00000000-0000-4000-8000-000000000000/generation-observations", "POST"],
  ])("rejects undeclared route %s %s before touching upstream", async (path, method) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const request = new NextRequest(`http://console.test/api/${path}`, {
      method,
      headers: { origin: "http://console.test" },
    });

    const response = await proxyBackendRequest(request, path);

    expect(response.status).toBe(404);
    await expect(response.json()).resolves.toMatchObject({
      detail: { code: "BFF_ROUTE_NOT_ALLOWED" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("exposes only the browser-facing v3 control and detection routes", () => {
    expect(matchBffRoutePolicy("events/v3", "GET")?.capability).toBe("read");
    expect(matchBffRoutePolicy("runtime-status/v1", "GET")?.capability).toBe("read");
    expect(matchBffRoutePolicy("runtime-status/v1", "POST")).toBeNull();
    expect(matchBffRoutePolicy("internal/runtime-status/v1/telemetry_v3", "GET")).toBeNull();
    expect(matchBffRoutePolicy("events/v3/00000000-0000-4000-8000-000000000000", "GET")?.capability).toBe("read");
    expect(matchBffRoutePolicy("incidents/v3", "GET")?.capability).toBe("read");
    expect(
      matchBffRoutePolicy(
        "workflows/event-v4/00000000-0000-4000-8000-000000000000",
        "POST",
      )?.capability,
    ).toBe("workflow");
    expect(
      matchBffRoutePolicy(
        "workflows/event-v4/00000000-0000-4000-8000-000000000000",
        "GET",
      )?.capability,
    ).toBe("workflow");
    expect(matchBffRoutePolicy("p4/control-v3/pipeline-bundles/import", "POST")?.capability).toBe("p4");
    expect(matchBffRoutePolicy("p4/control-v3/targets/00000000-0000-4000-8000-000000000000", "PUT")?.capability).toBe("p4");
    expect(
      matchBffRoutePolicy(
        "p4/control-v3/targets/00000000-0000-4000-8000-000000000000/activations",
        "GET",
      )?.capability,
    ).toBe("read");
    expect(
      matchBffRoutePolicy(
        "p4/control-v3/targets/00000000-0000-4000-8000-000000000000/activations",
        "POST",
      )?.capability,
    ).toBe("p4");
    expect(matchBffRoutePolicy("events/v3/ingest", "POST")).toBeNull();
    expect(
      matchBffRoutePolicy(
        "p4/control-v3/targets/00000000-0000-4000-8000-000000000000/generation-observations",
        "POST",
      ),
    ).toBeNull();
  });

  it("blocks any runtime-status token or lease field before it reaches the browser", async () => {
    const marker = "internal-runtime-token-must-not-escape";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ schema: "masi.runtime-status-public.v1", lease_token: marker }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const request = new NextRequest("http://console.test/api/runtime-status/v1", {
      method: "GET",
      headers: { cookie: `${ACCESS_COOKIE}=access` },
    });

    const response = await proxyBackendRequest(request, "runtime-status/v1");

    expect(response.status).toBe(502);
    const text = await response.text();
    expect(text).toContain("BFF_SENSITIVE_RESPONSE_BLOCKED");
    expect(text).not.toContain(marker);
    expect(JSON.stringify(errorSpy.mock.calls)).not.toContain(marker);
    errorSpy.mockRestore();
  });

  it("blocks nested sensitive JSON without copying its values", async () => {
    const marker = "credential-value-that-must-not-escape";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ result: [{ profile: { api_key: marker } }] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const request = new NextRequest("http://console.test/api/mcp/console/tools/call", {
      method: "POST",
      headers: {
        origin: "http://console.test",
        cookie: `${ACCESS_COOKIE}=access; ${CSRF_COOKIE}=csrf-audit`,
        "x-csrf-token": "csrf-audit",
        "content-type": "application/json",
      },
      body: JSON.stringify({ tool_name: "get_recent_nids_events", arguments: {} }),
    });

    const response = await proxyBackendRequest(request, "mcp/console/tools/call");

    expect(response.status).toBe(502);
    const text = await response.text();
    expect(text).toContain("BFF_SENSITIVE_RESPONSE_BLOCKED");
    expect(text).not.toContain(marker);
    expect(JSON.stringify(errorSpy.mock.calls)).not.toContain(marker);
    errorSpy.mockRestore();
  });

  it("fails closed for production mutations without a configured public origin", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NIDS_FRONTEND_PUBLIC_ORIGIN", "");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const request = new NextRequest("https://console.test/api/workflows", {
      method: "POST",
      headers: {
        origin: "https://console.test",
        cookie: `${ACCESS_COOKIE}=access; ${CSRF_COOKIE}=csrf-audit`,
        "x-csrf-token": "csrf-audit",
      },
      body: "{}",
    });

    const response = await proxyBackendRequest(request, "workflows");

    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toMatchObject({
      detail: { code: "BFF_PUBLIC_ORIGIN_NOT_CONFIGURED" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not replay an unsafe mutation after an upstream 401", async () => {
    let mutationCalls = 0;
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.endsWith("/api/config/runtime")) {
        return new Response(
          JSON.stringify({ workflow_maintenance: false, p4_maintenance: false }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.endsWith("/api/auth/refresh")) {
        return new Response(
          JSON.stringify({
            access_token: "refreshed-access",
            refresh_token: "rotated-refresh",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.endsWith("/api/me")) {
        return new Response(
          JSON.stringify({ id: "admin-1", username: "admin", role: "admin" }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      mutationCalls += 1;
      return mutationCalls === 1
        ? new Response(JSON.stringify({ detail: { code: "AUTH_REQUIRED" } }), {
            status: 401,
            headers: { "content-type": "application/json" },
          })
        : new Response(JSON.stringify({ mutated: true }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
    });
    vi.stubGlobal("fetch", fetchMock);
    const request = new NextRequest("http://console.test/api/admin/users/user-1", {
      method: "PATCH",
      headers: {
        origin: "http://console.test",
        cookie: `${ACCESS_COOKIE}=expired-access; ${REFRESH_COOKIE}=refresh-once; ${CSRF_COOKIE}=csrf-audit`,
        "x-csrf-token": "csrf-audit",
        "content-type": "application/json",
      },
      body: JSON.stringify({ enabled: false }),
    });

    const response = await proxyBackendRequest(request, "admin/users/user-1");
    expect(response.status).toBe(401);
    expect(mutationCalls).toBe(1);
  });
});
