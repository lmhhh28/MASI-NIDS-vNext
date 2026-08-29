import { beforeEach, describe, expect, it, vi } from "vitest";

import { useAuthStore } from "@/lib/auth";

const user = { id: "u1", username: "analyst", role: "analyze" as const };

describe("session auth store", () => {
  beforeEach(() => {
    useAuthStore.setState({
      sessionStatus: "idle",
      user: null,
      csrfToken: null,
    });
    vi.unstubAllGlobals();
  });

  it("bootstraps only user and CSRF state and never persists bearer tokens", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ authenticated: true, user, csrf_token: "csrf-1" }),
          { status: 200, headers: { "content-type": "application/json" } }
        )
      )
    );
    await useAuthStore.getState().bootstrap();
    expect(useAuthStore.getState()).toMatchObject({
      sessionStatus: "authenticated",
      user,
      csrfToken: "csrf-1",
    });
    expect(JSON.stringify(window.localStorage)).not.toContain("access_token");
    expect(window.localStorage.getItem("nids-auth")).toBeNull();
  });

  it("coalesces concurrent refresh calls into one request", async () => {
    useAuthStore.setState({ csrfToken: "csrf-1", sessionStatus: "authenticated", user });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ authenticated: true, user, csrf_token: "csrf-1" }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );
    vi.stubGlobal("fetch", fetchMock);
    const [first, second] = await Promise.all([
      useAuthStore.getState().refresh(),
      useAuthStore.getState().refresh(),
    ]);
    expect(first).toBe(true);
    expect(second).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("logs in through the same-origin session endpoint without storing tokens", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ authenticated: false, user: null, csrf_token: "csrf-login" }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ authenticated: true, user, csrf_token: "csrf-login" }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(useAuthStore.getState().login("analyst", "secret"))
      .resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/session/login",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "x-csrf-token": "csrf-login" }),
        body: JSON.stringify({ username: "analyst", password: "secret" }),
      }),
    );
    expect(useAuthStore.getState()).toMatchObject({
      sessionStatus: "authenticated",
      user,
      csrfToken: "csrf-login",
    });
    expect(Object.keys(window.localStorage)).not.toEqual(
      expect.arrayContaining(["access_token", "refresh_token", "nids-auth"]),
    );
  });

  it("clears local session state even when logout revocation is unavailable", async () => {
    useAuthStore.setState({ csrfToken: "csrf-1", sessionStatus: "authenticated", user });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));

    await expect(useAuthStore.getState().logout()).resolves.toBeUndefined();
    expect(useAuthStore.getState()).toMatchObject({
      sessionStatus: "unauthenticated",
      user: null,
      csrfToken: null,
    });
  });
});
