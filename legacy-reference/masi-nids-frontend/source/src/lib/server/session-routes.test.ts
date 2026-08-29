// @vitest-environment node

import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { GET as getSession } from "@/app/api/session/route";
import { POST as refreshSession } from "@/app/api/session/refresh/route";
import { CSRF_COOKIE, REFRESH_COOKIE } from "@/lib/server/bff";

describe("explicit session rotation boundary", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("reports refresh_required without rotating from GET /api/session", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const request = new NextRequest("http://console.test/api/session", {
      headers: { cookie: `${REFRESH_COOKIE}=refresh-once` },
    });

    const response = await getSession(request);

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({
      authenticated: false,
      refresh_required: true,
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(response.headers.get("set-cookie")).not.toContain(`${REFRESH_COOKIE}=;`);
  });

  it("preserves the refresh cookie when another instance already rotated it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: { code: "REFRESH_ALREADY_ROTATED" } }), {
          status: 409,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    const request = new NextRequest("http://console.test/api/session/refresh", {
      method: "POST",
      headers: {
        origin: "http://console.test",
        cookie: `${REFRESH_COOKIE}=refresh-once; ${CSRF_COOKIE}=csrf-session`,
        "x-csrf-token": "csrf-session",
      },
    });

    const response = await refreshSession(request);

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toMatchObject({
      refresh_required: true,
      detail: { code: "REFRESH_ALREADY_ROTATED" },
    });
    expect(response.headers.get("set-cookie")).not.toContain(`${REFRESH_COOKIE}=;`);
  });
});
