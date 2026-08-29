import { beforeEach, describe, expect, it, vi } from "vitest";

import api, { shouldRetryQuery } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";
import { ApiClientError } from "@/lib/http-error";

const user = { id: "u1", username: "operator", role: "analyze" as const };

describe("same-origin API client", () => {
  beforeEach(() => {
    useAuthStore.setState({
      sessionStatus: "authenticated",
      user,
      csrfToken: "csrf-api",
    });
    vi.unstubAllGlobals();
  });

  it("injects CSRF on mutations and strips browser authorization", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.post("/workflows", { intent: "inspect" }, {
      headers: {
        Authorization: "Bearer browser-token",
        "Idempotency-Key": "operation-1",
      },
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(url).toBe("/api/workflows");
    expect(headers.get("authorization")).toBeNull();
    expect(headers.get("x-csrf-token")).toBe("csrf-api");
    expect(headers.get("idempotency-key")).toBe("operation-1");
    expect(init.body).toBe(JSON.stringify({ intent: "inspect" }));
  });

  it("coalesces concurrent 401 refreshes before replaying GET requests", async () => {
    let refreshed = false;
    let refreshCalls = 0;
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url === "/api/session/refresh") {
        refreshCalls += 1;
        await new Promise((resolve) => window.setTimeout(resolve, 0));
        refreshed = true;
        return new Response(
          JSON.stringify({ authenticated: true, user, csrf_token: "csrf-api-2" }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (!refreshed) {
        return new Response(JSON.stringify({ detail: { code: "AUTH_REQUIRED" } }), {
          status: 401,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const [first, second] = await Promise.all([
      api.get<{ ok: boolean }>("/protected/a"),
      api.get<{ ok: boolean }>("/protected/b"),
    ]);

    expect(first.data.ok).toBe(true);
    expect(second.data.ok).toBe(true);
    expect(refreshCalls).toBe(1);
  });

  it("retries only eligible GET failures", () => {
    const unavailable = new ApiClientError("unavailable", {
      config: { method: "get" },
      response: { status: 503 },
    });
    const rejectedMutation = new ApiClientError("conflict", {
      config: { method: "post" },
      response: { status: 503 },
    });

    expect(shouldRetryQuery(0, unavailable)).toBe(true);
    expect(shouldRetryQuery(2, unavailable)).toBe(false);
    expect(shouldRetryQuery(0, rejectedMutation)).toBe(false);
  });
});
