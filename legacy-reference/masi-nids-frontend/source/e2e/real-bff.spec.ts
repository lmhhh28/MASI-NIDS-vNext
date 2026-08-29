import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer as createHttpServer, request as httpRequest } from "node:http";
import { createServer as createHttpsServer } from "node:https";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Server } from "node:net";

import { expect, test } from "@playwright/test";

const backendPort = 18090;
const proxyPort = 3443;
const nextPort = 3100;
const frontendOrigin = `https://127.0.0.1:${proxyPort}`;

interface BackendCall {
  method: string;
  path: string;
  authorization: string | null;
}

const calls: BackendCall[] = [];
let backend: Server;
let proxy: Server;
let certificateDirectory = "";
let refreshCalls = 0;

function listen(server: Server, port: number): Promise<void> {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => {
      server.off("error", reject);
      resolve();
    });
  });
}

function close(server: Server | undefined): Promise<void> {
  if (!server) return Promise.resolve();
  return new Promise((resolve) => {
    if ("closeAllConnections" in server) {
      (server as Server & { closeAllConnections(): void }).closeAllConnections();
    }
    server.close(() => resolve());
  });
}

test.beforeAll(async () => {
  backend = createHttpServer((request, response) => {
    const url = new URL(request.url ?? "/", `http://127.0.0.1:${backendPort}`);
    calls.push({
      method: request.method ?? "GET",
      path: url.pathname,
      authorization: typeof request.headers.authorization === "string"
        ? request.headers.authorization
        : null,
    });
    const json = (status: number, payload: unknown) => {
      response.writeHead(status, { "content-type": "application/json" });
      response.end(JSON.stringify(payload));
    };

    if (url.pathname === "/api/auth/login" && request.method === "POST") {
      json(200, {
        access_token: "fake-access-initial",
        refresh_token: "fake-refresh-initial",
        expires_in_seconds: 3600,
      });
      return;
    }
    if (url.pathname === "/api/auth/refresh" && request.method === "POST") {
      refreshCalls += 1;
      json(200, {
        access_token: "fake-access-rotated",
        refresh_token: "fake-refresh-rotated",
        expires_in_seconds: 3600,
      });
      return;
    }
    if (url.pathname === "/api/auth/logout" && request.method === "POST") {
      json(200, { status: "ok" });
      return;
    }
    if (url.pathname === "/api/me") {
      const authorization = request.headers.authorization ?? "";
      if (!String(authorization).startsWith("Bearer fake-access-")) {
        json(401, { detail: { code: "AUTH_REQUIRED" } });
        return;
      }
      json(200, { id: "admin-1", username: "admin", role: "admin", enabled: true });
      return;
    }
    if (url.pathname === "/api/config/runtime") {
      json(200, { workflow_maintenance: false, p4_maintenance: false });
      return;
    }
    if (url.pathname === "/api/events") {
      json(200, []);
      return;
    }
    if (url.pathname === "/api/admin/llm-config") {
      json(200, { nested: { api_key: "fake-sensitive-marker" } });
      return;
    }
    if (url.pathname === "/api/workflows" && request.method === "POST") {
      json(401, { detail: { code: "AUTH_REQUIRED" } });
      return;
    }
    json(404, { detail: { code: "NOT_FOUND" } });
  });
  await listen(backend, backendPort);

  certificateDirectory = mkdtempSync(join(tmpdir(), "masi-real-bff-"));
  const keyPath = join(certificateDirectory, "key.pem");
  const certificatePath = join(certificateDirectory, "certificate.pem");
  execFileSync(
    "openssl",
    [
      "req",
      "-x509",
      "-newkey",
      "rsa:2048",
      "-nodes",
      "-keyout",
      keyPath,
      "-out",
      certificatePath,
      "-subj",
      "/CN=127.0.0.1",
      "-addext",
      "subjectAltName=IP:127.0.0.1",
      "-days",
      "1",
    ],
    { stdio: "ignore" },
  );
  proxy = createHttpsServer(
    { key: readFileSync(keyPath), cert: readFileSync(certificatePath) },
    (request, response) => {
      const upstream = httpRequest(
        {
          hostname: "127.0.0.1",
          port: nextPort,
          method: request.method,
          path: request.url,
          headers: {
            ...request.headers,
            host: request.headers.host ?? `127.0.0.1:${proxyPort}`,
            "x-forwarded-proto": "https",
          },
        },
        (upstreamResponse) => {
          response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers);
          upstreamResponse.pipe(response);
        },
      );
      upstream.on("error", () => {
        response.writeHead(502, { "content-type": "application/json" });
        response.end(JSON.stringify({ detail: { code: "PROXY_UPSTREAM_UNAVAILABLE" } }));
      });
      request.pipe(upstream);
    },
  );
  await listen(proxy, proxyPort);
});

test.afterAll(async () => {
  await Promise.all([close(proxy), close(backend)]);
  if (certificateDirectory) rmSync(certificateDirectory, { recursive: true, force: true });
});

test("production BFF enforces HTTPS cookie and proxy trust boundaries", async ({ page, context }) => {
  const browserConsole: string[] = [];
  page.on("console", (message) => browserConsole.push(message.text()));
  await page.goto("/login");
  await page.waitForLoadState("networkidle");

  const session = await page.evaluate(async () => {
    const response = await fetch("/api/session", { cache: "no-store" });
    return response.json() as Promise<{ csrf_token: string }>;
  });
  const bootstrapCookies = await context.cookies(frontendOrigin);
  const bootstrapCookieNames = bootstrapCookies.map((cookie) => cookie.name);
  expect(bootstrapCookieNames).toContain("__Host-nids_csrf");
  expect(
    bootstrapCookies.find((cookie) => cookie.name === "__Host-nids_csrf")?.value
      === session.csrf_token,
  ).toBe(true);
  const login = await page.evaluate(async ({ csrf }) => {
    const response = await fetch("/api/session/login", {
      method: "POST",
      headers: { "content-type": "application/json", "x-csrf-token": csrf },
      body: JSON.stringify({ username: "admin", password: "password123" }),
    });
    return { status: response.status, text: await response.text() };
  }, { csrf: session.csrf_token });
  expect(login.status, login.text).toBe(200);
  expect(login.text).not.toMatch(/fake-access|fake-refresh|access_token|refresh_token/i);

  const cookies = await context.cookies(frontendOrigin);
  const sessionCookies = cookies.filter((cookie) =>
    ["__Host-nids_access", "__Host-nids_refresh", "__Host-nids_csrf"].includes(cookie.name)
  );
  expect(sessionCookies).toHaveLength(3);
  for (const cookie of sessionCookies) {
    expect(cookie.secure, `${cookie.name} must be Secure`).toBe(true);
    expect(cookie.httpOnly, `${cookie.name} must be HttpOnly`).toBe(true);
    expect(cookie.sameSite, `${cookie.name} must be SameSite=Lax`).toBe("Lax");
    expect(cookie.path, `${cookie.name} must be host-wide`).toBe("/");
  }
  const browserState = await page.evaluate(() => ({
    local: JSON.stringify(window.localStorage),
    session: JSON.stringify(window.sessionStorage),
    visibleCookies: document.cookie,
  }));
  expect(JSON.stringify(browserState)).not.toMatch(/fake-access|fake-refresh|access_token|refresh_token/i);

  const callsBeforeForbidden = calls.length;
  const forbidden = await page.evaluate(async ({ csrf }) => {
    const requests = [
      ["/api/auth/login", "POST"],
      ["/api/auth/refresh", "POST"],
      ["/api/auth/logout", "POST"],
      ["/api/me", "GET"],
      ["/api/mcp/tools/call", "POST"],
    ] as const;
    return Promise.all(requests.map(async ([path, method]) => {
      const response = await fetch(path, {
        method,
        headers: { "content-type": "application/json", "x-csrf-token": csrf },
        body: method === "GET" ? undefined : "{}",
      });
      return response.status;
    }));
  }, { csrf: session.csrf_token });
  expect(forbidden).toEqual([404, 404, 404, 404, 404]);
  expect(calls).toHaveLength(callsBeforeForbidden);

  const eventsStatus = await page.evaluate(async () => {
    const response = await fetch("/api/events", {
      headers: { authorization: "Bearer browser-supplied-token" },
    });
    return response.status;
  });
  expect(eventsStatus).toBe(200);
  const eventCall = calls.filter((call) => call.path === "/api/events").at(-1);
  expect(eventCall?.authorization).toBe("Bearer fake-access-initial");

  const sensitive = await page.evaluate(async () => {
    const response = await fetch("/api/admin/llm-config");
    return { status: response.status, text: await response.text() };
  });
  expect(sensitive.status).toBe(502);
  expect(sensitive.text).toContain("BFF_SENSITIVE_RESPONSE_BLOCKED");
  expect(sensitive.text).not.toContain("fake-sensitive-marker");

  const workflowsBefore = calls.filter((call) => call.path === "/api/workflows").length;
  const mutation = await page.evaluate(async ({ csrf }) => {
    const response = await fetch("/api/workflows", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "idempotency-key": "real-bff-mutation-once",
        "x-csrf-token": csrf,
      },
      body: JSON.stringify({ template_name: "inspect_only" }),
    });
    return response.status;
  }, { csrf: session.csrf_token });
  expect(mutation).toBe(401);
  expect(calls.filter((call) => call.path === "/api/workflows")).toHaveLength(workflowsBefore + 1);

  const refresh = await page.evaluate(async ({ csrf }) => {
    const response = await fetch("/api/session/refresh", {
      method: "POST",
      headers: { "x-csrf-token": csrf },
    });
    return { status: response.status, text: await response.text() };
  }, { csrf: session.csrf_token });
  expect(refresh.status).toBe(200);
  expect(refresh.text).not.toMatch(/fake-access|fake-refresh|access_token|refresh_token/i);
  expect(refreshCalls).toBe(1);

  const logout = await page.evaluate(async ({ csrf }) => {
    const response = await fetch("/api/session/logout", {
      method: "POST",
      headers: { "x-csrf-token": csrf },
    });
    return response.status;
  }, { csrf: session.csrf_token });
  expect(logout).toBe(200);
  const remainingCookies = await context.cookies(frontendOrigin);
  expect(remainingCookies.some((cookie) => cookie.name.startsWith("__Host-nids_"))).toBe(false);
  expect(browserConsole.join("\n")).not.toMatch(/fake-access|fake-refresh|fake-sensitive-marker/i);
});
