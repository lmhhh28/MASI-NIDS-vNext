// @vitest-environment node

import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  ACCESS_COOKIE,
  CSRF_COOKIE,
  proxyBackendRequest,
  resetRuntimePreflightCacheForTests,
} from "@/lib/server/bff";

function mutation(path: string, method = "POST") {
  return new NextRequest(`http://console.test/api/${path}`, {
    method,
    headers: {
      origin: "http://console.test",
      cookie: `${ACCESS_COOKIE}=runtime-access; ${CSRF_COOKIE}=runtime-csrf`,
      "x-csrf-token": "runtime-csrf",
      "content-type": "application/json",
    },
    body: "{}",
  });
}

describe("BFF runtime mutation preflight", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    resetRuntimePreflightCacheForTests();
  });

  it.each([
    ["workflows", { workflow_maintenance: true, p4_maintenance: false }, "WORKFLOW_MAINTENANCE"],
    ["p4/switches", { workflow_maintenance: false, p4_maintenance: true }, "P4_MAINTENANCE"],
  ])("blocks %s before forwarding when its capability is maintained", async (path, runtime, code) => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(runtime), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await proxyBackendRequest(mutation(path), path);

    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toMatchObject({ detail: { code } });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]?.[0])).toMatch(/\/api\/config\/runtime$/);
  });

  it("fails closed for admin writes when runtime state is unavailable", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new DOMException("timeout", "TimeoutError"));
    vi.stubGlobal("fetch", fetchMock);

    const response = await proxyBackendRequest(
      mutation("admin/risk-policy", "PATCH"),
      "admin/risk-policy",
    );

    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toMatchObject({
      detail: { code: "RUNTIME_STATE_UNKNOWN" },
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not preflight reads or read/validate console calls", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const read = new NextRequest("http://console.test/api/events", {
      headers: { cookie: `${ACCESS_COOKIE}=runtime-access` },
    });
    const consoleCall = mutation("mcp/console/tools/call");

    expect((await proxyBackendRequest(read, "events")).status).toBe(200);
    expect((await proxyBackendRequest(consoleCall, "mcp/console/tools/call")).status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map((call) => String(call[0]))).not.toContainEqual(
      expect.stringMatching(/\/api\/config\/runtime$/),
    );
  });

  it("uses a two-second cache while still forwarding each mutation only once", async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      if (String(input).endsWith("/api/config/runtime")) {
        return new Response(
          JSON.stringify({ workflow_maintenance: false, p4_maintenance: false }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    expect((await proxyBackendRequest(mutation("workflows"), "workflows")).status).toBe(200);
    expect((await proxyBackendRequest(mutation("workflows"), "workflows")).status).toBe(200);

    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.filter((url) => url.endsWith("/api/config/runtime"))).toHaveLength(1);
    expect(urls.filter((url) => url.endsWith("/api/workflows"))).toHaveLength(2);
  });
});
