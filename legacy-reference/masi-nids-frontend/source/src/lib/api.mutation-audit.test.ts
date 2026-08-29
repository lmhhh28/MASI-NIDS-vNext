import { beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";
import { useAuthStore } from "@/lib/auth";

const user = { id: "u1", username: "operator", role: "analyze" as const };

describe("system-audit mutation replay regression", () => {
  beforeEach(() => {
    useAuthStore.setState({
      sessionStatus: "authenticated",
      user,
      csrfToken: "csrf-audit",
    });
    vi.unstubAllGlobals();
  });

  it("does not automatically replay POST after a 401", async () => {
    let mutationCalls = 0;
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url === "/api/session/refresh") {
        return new Response(
          JSON.stringify({ authenticated: true, user, csrf_token: "csrf-audit-2" }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      mutationCalls += 1;
      return mutationCalls === 1
        ? new Response(JSON.stringify({ detail: { code: "AUTH_REQUIRED" } }), {
            status: 401,
            headers: { "content-type": "application/json" },
          })
        : new Response(JSON.stringify({ created: true }), {
            status: 201,
            headers: { "content-type": "application/json" },
          });
    });
    vi.stubGlobal("fetch", fetchMock);

    let unexpectedlyResolved = false;
    try {
      await api.post("/admin/users", {
        username: "new-user",
        role: "analyze",
      });
      unexpectedlyResolved = true;
    } catch {
      // The original 401 is the safe outcome; the browser must not replay POST.
    }

    expect(mutationCalls).toBe(1);
    expect(unexpectedlyResolved).toBe(false);
  });

  it.each(["POST", "PUT", "PATCH", "DELETE"])(
    "refreshes session but requires manual confirmation for %s",
    async (method) => {
      let mutationCalls = 0;
      const fetchMock = vi.fn(async (input: string | URL | Request) => {
        if (String(input) === "/api/session/refresh") {
          return new Response(
            JSON.stringify({ authenticated: true, user, csrf_token: "csrf-audit-2" }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        mutationCalls += 1;
        return new Response(JSON.stringify({ detail: { code: "AUTH_REQUIRED" } }), {
          status: 401,
          headers: { "content-type": "application/json", "x-request-id": "request-audit" },
        });
      });
      vi.stubGlobal("fetch", fetchMock);

      await expect(
        api.request({ url: "/admin/users/user-1", method, data: { enabled: false } }),
      ).rejects.toMatchObject({
        manualRetryRequired: true,
        requestId: "request-audit",
        response: { status: 401 },
      });
      expect(mutationCalls).toBe(1);
    },
  );
});
