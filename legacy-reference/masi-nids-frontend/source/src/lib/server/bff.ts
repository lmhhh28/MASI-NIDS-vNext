import "server-only";

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";

// `__Host-` cookies are valid only when the Secure attribute is present. Use
// that stronger prefix in production and ordinary host-only names for local
// HTTP development so browsers do not silently reject the session cookies.
const COOKIE_PREFIX = process.env.NODE_ENV === "production" ? "__Host-" : "";
const ACCESS_COOKIE = `${COOKIE_PREFIX}nids_access`;
const REFRESH_COOKIE = `${COOKIE_PREFIX}nids_refresh`;
const CSRF_COOKIE = `${COOKIE_PREFIX}nids_csrf`;
const DEFAULT_TIMEOUT_MS = 15_000;
const EXTENDED_TIMEOUT_MS = 30_000;
const PIPELINE_IMPORT_TIMEOUT_MS = 120_000;
const RUNTIME_PREFLIGHT_TIMEOUT_MS = 2_000;
const RUNTIME_PREFLIGHT_CACHE_MS = 2_000;
const REFRESH_MAX_AGE_SECONDS = 7 * 24 * 60 * 60;

export type BffCapability = "read" | "workflow" | "p4" | "admin" | "operations";

export interface BffRoutePolicy {
  readonly path: RegExp;
  readonly methods: readonly string[];
  readonly capability: BffCapability;
  readonly timeoutMs: number;
}

const READ = ["GET", "HEAD"] as const;
const ID = "[^/]+";

// This is the browser console's complete backend surface. A backend route is
// not browser-reachable merely because it exists: it must be declared here
// with an exact method, capability and timeout budget.
export const BFF_ROUTE_POLICIES: readonly BffRoutePolicy[] = [
  { path: /^analytics\/v3\/trends$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^analytics\/v3\/live$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^alerts$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^audit$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^config\/(?:risk-policy|runtime)$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^flow-captures$/, methods: ["POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^flow-captures/${ID}$`), methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^directional-evidence/${ID}$`), methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^directional-evidence:resolve$/, methods: ["POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^directional-evidence/${ID}/review$`), methods: ["POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^events$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^events\/v3$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^events/v3/${ID}$`), methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^incidents\/v3$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^events\/stats$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^events\/sources$/, methods: ["GET", "HEAD", "POST"], capability: "admin", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^events/sources/${ID}$`), methods: ["PATCH", "DELETE"], capability: "admin", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^events/sources/${ID}/runs$`), methods: ["POST"], capability: "admin", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^events\/ingest:once$/, methods: ["POST"], capability: "admin", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^events/${ID}$`), methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^mcp\/tools$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^mcp\/console\/tools\/call$/, methods: ["POST"], capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^notices$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^operations\/summary$/, methods: READ, capability: "operations", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^runtime-status\/v1$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^operator-batches$/, methods: ["POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^operator-batches/${ID}$`), methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^operator-batches/${ID}/cancel$`), methods: ["POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^templates$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^workflows$/, methods: ["GET", "HEAD", "POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^workflows/flow-evidence-v3/${ID}/block$`), methods: ["GET", "POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^workflows/event-v4/${ID}$`), methods: ["GET", "POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^workflows/${ID}$`), methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^workflows/${ID}/summary$`), methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^workflows/${ID}/revisions/${ID}/nodes/${ID}/analysis-trace$`), methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^workflows/${ID}/(?:validate|review|cancel)$`), methods: ["POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^workflows/${ID}/nodes/${ID}/retry$`), methods: ["POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^workflows/${ID}/deployments$`), methods: ["POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^workflows/${ID}/deployments/${ID}/retry$`), methods: ["POST"], capability: "workflow", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^p4\/switches$/, methods: ["GET", "HEAD", "POST"], capability: "p4", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/switches/${ID}$`), methods: ["PATCH", "DELETE"], capability: "p4", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/switches/${ID}/(?:connect|enable-writes)$`), methods: ["POST"], capability: "p4", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/switches/${ID}/health$`), methods: READ, capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/switches/${ID}/tables$`), methods: READ, capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/switches/${ID}/tables/${ID}$`), methods: READ, capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/switches/${ID}/tables/${ID}/(?:form-schema|entries)$`), methods: READ, capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/switches/${ID}/tables/${ID}/entries:validate$`), methods: ["POST"], capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/switches/${ID}/tables/${ID}/entries$`), methods: ["POST"], capability: "p4", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/switches/${ID}/snapshots$`), methods: ["POST"], capability: "p4", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^p4\/deployments$/, methods: READ, capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^p4\/deployment-requests\/resolve$/, methods: READ, capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/deployments/${ID}$`), methods: READ, capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/deployments/${ID}/rollback$`), methods: ["POST"], capability: "p4", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/deployments/${ID}/observations$`), methods: READ, capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/deployments/${ID}/observe$`), methods: ["POST"], capability: "p4", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^p4\/ttl-cleanup:run$/, methods: ["POST"], capability: "p4", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^p4\/control-v3\/pipeline-bundles$/, methods: READ, capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^p4\/control-v3\/pipeline-bundles\/import$/, methods: ["POST"], capability: "p4", timeoutMs: PIPELINE_IMPORT_TIMEOUT_MS },
  { path: new RegExp(`^p4/control-v3/pipeline-bundles/${ID}$`), methods: READ, capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^p4\/control-v3\/targets$/, methods: ["GET", "HEAD", "POST"], capability: "p4", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/control-v3/targets/${ID}$`), methods: ["PUT"], capability: "p4", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/control-v3/targets/${ID}/activations$`), methods: READ, capability: "read", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^p4/control-v3/targets/${ID}/activations$`), methods: ["POST"], capability: "p4", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^admin\/users$/, methods: ["GET", "HEAD", "POST"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^admin/users/${ID}$`), methods: ["PATCH"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^admin\/risk-policy$/, methods: ["GET", "HEAD", "PATCH"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^admin\/llm-config$/, methods: ["GET", "HEAD", "POST"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^admin/llm-config/${ID}$`), methods: ["PATCH", "DELETE"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^admin/llm-config/${ID}/(?:test|probe)$`), methods: ["POST"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^admin\/protected-targets$/, methods: ["GET", "HEAD", "POST"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^admin/protected-targets/${ID}$`), methods: ["PATCH", "DELETE"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^admin\/templates$/, methods: ["GET", "HEAD", "POST"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^admin/templates/${ID}$`), methods: ["PATCH", "DELETE"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^admin\/demo-traffic\/status$/, methods: READ, capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^flow-evidence\/v3$/, methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^demo/runs/${ID}/projection$`), methods: READ, capability: "read", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: /^admin\/demo-traffic\/cleanup\/preview$/, methods: ["POST"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^admin\/demo-traffic\/(?:run|stop|cleanup|recover|seal)$/, methods: ["POST"], capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: /^admin\/demo-traffic\/operations\/resolve$/, methods: READ, capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^admin/demo-traffic/operations/${ID}$`), methods: READ, capability: "admin", timeoutMs: EXTENDED_TIMEOUT_MS },
  { path: new RegExp(`^admin/event-runs/${ID}/retention-holds$`), methods: ["GET", "HEAD", "POST"], capability: "admin", timeoutMs: DEFAULT_TIMEOUT_MS },
  { path: new RegExp(`^admin/event-runs/${ID}/retention-holds/${ID}$`), methods: ["DELETE"], capability: "admin", timeoutMs: DEFAULT_TIMEOUT_MS },
] as const;

const PERMANENTLY_FORBIDDEN_PATH = /^(?:auth(?:\/|$)|me$|session(?:\/|$)|mcp\/tools\/call$)/;
const SENSITIVE_RESPONSE_KEYS = new Set([
  "access_token",
  "refresh_token",
  "password_hash",
  "api_key",
  "authorization",
  "event_ingest_token",
  "lease_token",
  "p4_agent_token",
  "private_key",
  "runtime_status_token",
  "seed",
  "token",
]);

interface BackendTokens {
  access_token: string;
  refresh_token: string;
  expires_in_seconds?: number;
}

interface SessionResult {
  tokens: BackendTokens;
  user: Record<string, unknown>;
}

interface RefreshFailure {
  status: number;
  payload: unknown;
}

type RefreshResult =
  | { ok: true; session: SessionResult }
  | { ok: false; failure: RefreshFailure };

const refreshFlights = new Map<string, Promise<RefreshResult>>();

interface RuntimePreflightState {
  workflow_maintenance: boolean;
  p4_maintenance: boolean;
}

interface RuntimeCacheEntry {
  expiresAt: number;
  state: RuntimePreflightState;
}

const runtimePreflightCache = new Map<string, RuntimeCacheEntry>();
const runtimePreflightFlights = new Map<string, Promise<RuntimePreflightState>>();

function backendBaseUrl(): string {
  return (process.env.NIDS_BACKEND_INTERNAL_URL ?? "http://127.0.0.1:8090").replace(
    /\/+$/,
    ""
  );
}

function secureCookies(): boolean {
  return process.env.NODE_ENV === "production";
}

function cookieOptions(maxAge?: number) {
  return {
    httpOnly: true,
    sameSite: "lax" as const,
    secure: secureCookies(),
    path: "/",
    ...(maxAge === undefined ? {} : { maxAge }),
  };
}

export function csrfToken(request: NextRequest): string {
  return request.cookies.get(CSRF_COOKIE)?.value ?? randomBytes(32).toString("base64url");
}

export function setCsrfCookie(response: NextResponse, token: string): void {
  response.cookies.set(CSRF_COOKIE, token, cookieOptions(REFRESH_MAX_AGE_SECONDS));
}

export function setSessionCookies(
  response: NextResponse,
  session: SessionResult,
  csrf: string
): void {
  response.cookies.set(
    ACCESS_COOKIE,
    session.tokens.access_token,
    cookieOptions(session.tokens.expires_in_seconds ?? 60 * 60)
  );
  response.cookies.set(
    REFRESH_COOKIE,
    session.tokens.refresh_token,
    cookieOptions(REFRESH_MAX_AGE_SECONDS)
  );
  setCsrfCookie(response, csrf);
}

export function clearSessionCookies(response: NextResponse): void {
  for (const name of [ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE]) {
    response.cookies.set(name, "", cookieOptions(0));
  }
}

export function accessToken(request: NextRequest): string | undefined {
  return request.cookies.get(ACCESS_COOKIE)?.value;
}

export function refreshToken(request: NextRequest): string | undefined {
  return request.cookies.get(REFRESH_COOKIE)?.value;
}

function safeEqual(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left);
  const rightBytes = Buffer.from(right);
  return leftBytes.length === rightBytes.length && timingSafeEqual(leftBytes, rightBytes);
}

export function validateMutationRequest(request: NextRequest): NextResponse | null {
  if (["GET", "HEAD", "OPTIONS"].includes(request.method.toUpperCase())) {
    return null;
  }
  const configuredOrigin = process.env.NIDS_FRONTEND_PUBLIC_ORIGIN?.trim();
  if (process.env.NODE_ENV === "production" && !configuredOrigin) {
    return NextResponse.json(
      { detail: { code: "BFF_PUBLIC_ORIGIN_NOT_CONFIGURED" } },
      { status: 503 }
    );
  }
  const expectedOrigin = configuredOrigin ?? request.nextUrl.origin;
  const origin = request.headers.get("origin");
  if (!origin || origin !== expectedOrigin) {
    return NextResponse.json(
      { detail: { code: "BFF_ORIGIN_REJECTED", message: "Request origin is not allowed." } },
      { status: 403 }
    );
  }
  const cookieValue = request.cookies.get(CSRF_COOKIE)?.value;
  const headerValue = request.headers.get("x-csrf-token");
  if (!cookieValue || !headerValue || !safeEqual(cookieValue, headerValue)) {
    return NextResponse.json(
      { detail: { code: "BFF_CSRF_REJECTED", message: "CSRF token is missing or stale." } },
      { status: 403 }
    );
  }
  return null;
}

async function readPayload(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function backendHeaders(requestId?: string): Headers {
  const headers = new Headers({
    accept: "application/json",
    "content-type": "application/json",
  });
  if (requestId) headers.set("x-request-id", requestId);
  return headers;
}

function isRuntimePreflightState(value: unknown): value is RuntimePreflightState {
  if (typeof value !== "object" || value === null) return false;
  const state = value as Record<string, unknown>;
  return typeof state.workflow_maintenance === "boolean"
    && typeof state.p4_maintenance === "boolean";
}

async function fetchRuntimePreflight(token: string, requestId?: string): Promise<RuntimePreflightState> {
  const response = await fetch(`${backendBaseUrl()}/api/config/runtime`, {
    headers: {
      authorization: `Bearer ${token}`,
      accept: "application/json",
      ...(requestId ? { "x-request-id": requestId } : {}),
    },
    cache: "no-store",
    signal: AbortSignal.timeout(RUNTIME_PREFLIGHT_TIMEOUT_MS),
  });
  if (response.status === 401) {
    throw new RuntimePreflightError("AUTH_REQUIRED", 401);
  }
  if (response.status === 403) {
    throw new RuntimePreflightError("FORBIDDEN", 403);
  }
  if (!response.ok) throw new RuntimePreflightError("RUNTIME_STATE_UNKNOWN", 503);
  const payload = await response.json() as unknown;
  if (!isRuntimePreflightState(payload)) {
    throw new RuntimePreflightError("RUNTIME_STATE_UNKNOWN", 503);
  }
  return payload;
}

class RuntimePreflightError extends Error {
  constructor(readonly code: string, readonly status: number) {
    super(code);
  }
}

async function runtimePreflight(token: string, requestId?: string): Promise<RuntimePreflightState> {
  const key = createHash("sha256").update(token).digest("base64url");
  const cached = runtimePreflightCache.get(key);
  if (cached && cached.expiresAt > Date.now()) return cached.state;
  const existing = runtimePreflightFlights.get(key);
  if (existing) return existing;
  const pending = fetchRuntimePreflight(token, requestId)
    .then((state) => {
      runtimePreflightCache.set(key, {
        state,
        expiresAt: Date.now() + RUNTIME_PREFLIGHT_CACHE_MS,
      });
      return state;
    })
    .finally(() => runtimePreflightFlights.delete(key));
  runtimePreflightFlights.set(key, pending);
  return pending;
}

function isMutationMethod(method: string): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase());
}

async function enforceRuntimeSafety(
  request: NextRequest,
  policy: BffRoutePolicy,
  token: string,
): Promise<NextResponse | null> {
  if (!isMutationMethod(request.method) || policy.capability === "read") return null;
  let runtime: RuntimePreflightState;
  try {
    runtime = await runtimePreflight(token, request.headers.get("x-request-id") ?? undefined);
  } catch (error) {
    const known = error instanceof RuntimePreflightError ? error : undefined;
    return NextResponse.json(
      { detail: { code: known?.code ?? "RUNTIME_STATE_UNKNOWN" } },
      { status: known?.status ?? 503 },
    );
  }
  if (policy.capability === "workflow" && runtime.workflow_maintenance) {
    return NextResponse.json(
      { detail: { code: "WORKFLOW_MAINTENANCE" } },
      { status: 503 },
    );
  }
  if (policy.capability === "p4" && runtime.p4_maintenance) {
    return NextResponse.json(
      { detail: { code: "P4_MAINTENANCE" } },
      { status: 503 },
    );
  }
  return null;
}

export function resetRuntimePreflightCacheForTests(): void {
  runtimePreflightCache.clear();
  runtimePreflightFlights.clear();
}

export async function backendMe(token: string): Promise<Response> {
  return fetch(`${backendBaseUrl()}/api/me`, {
    headers: { authorization: `Bearer ${token}`, accept: "application/json" },
    cache: "no-store",
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
  });
}

async function refreshBackendSession(token: string, requestId?: string): Promise<RefreshResult> {
  const response = await fetch(`${backendBaseUrl()}/api/auth/refresh`, {
    method: "POST",
    headers: backendHeaders(requestId),
    body: JSON.stringify({ refresh_token: token }),
    cache: "no-store",
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
  });
  if (!response.ok) {
    return { ok: false, failure: { status: response.status, payload: await readPayload(response) } };
  }
  const tokens = (await response.json()) as BackendTokens;
  const meResponse = await backendMe(tokens.access_token);
  if (!meResponse.ok) {
    return {
      ok: false,
      failure: { status: meResponse.status, payload: await readPayload(meResponse) },
    };
  }
  return {
    ok: true,
    session: { tokens, user: (await meResponse.json()) as Record<string, unknown> },
  };
}

export function refreshSession(token: string, requestId?: string): Promise<RefreshResult> {
  const key = createHash("sha256").update(token).digest("base64url");
  const existing = refreshFlights.get(key);
  if (existing) return existing;
  const pending = refreshBackendSession(token, requestId)
    .catch(
      (error): RefreshResult => ({
        ok: false,
        failure: {
          status: 502,
          payload: {
            detail: {
              code: error instanceof DOMException && error.name === "TimeoutError"
                ? "BFF_UPSTREAM_TIMEOUT"
                : "BFF_UPSTREAM_UNAVAILABLE",
            },
          },
        },
      })
    )
    .finally(() => refreshFlights.delete(key));
  refreshFlights.set(key, pending);
  return pending;
}

function allowedForwardHeaders(request: NextRequest): Headers {
  const headers = new Headers();
  for (const name of [
    "accept",
    "accept-language",
    "content-type",
    "idempotency-key",
    "x-request-id",
    "if-match",
  ]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  return headers;
}

function canonicalProxyPath(path: string): string | null {
  const rawParts = path.replace(/^\/+|\/+$/g, "").split("/");
  if (!rawParts.length || rawParts.some((part) => !part)) return null;
  try {
    const parts = rawParts.map((part) => decodeURIComponent(part));
    if (parts.some((part) => part === "." || part === ".." || /[\\/\u0000-\u001f]/.test(part))) {
      return null;
    }
    return parts.join("/");
  } catch {
    return null;
  }
}

export function matchBffRoutePolicy(path: string, method: string): BffRoutePolicy | null {
  const canonical = canonicalProxyPath(path);
  if (!canonical || PERMANENTLY_FORBIDDEN_PATH.test(canonical)) return null;
  const upperMethod = method.toUpperCase();
  return BFF_ROUTE_POLICIES.find(
    (policy) => policy.path.test(canonical) && policy.methods.includes(upperMethod)
  ) ?? null;
}

async function forwardOnce(
  request: NextRequest,
  path: string,
  token: string,
  body: ArrayBuffer | undefined,
  timeoutMs: number
): Promise<Response> {
  const url = new URL(
    `/api/${path
      .split("/")
      .filter(Boolean)
      .map((part) => encodeURIComponent(decodeURIComponent(part)))
      .join("/")}`,
    backendBaseUrl()
  );
  url.search = request.nextUrl.search;
  const headers = allowedForwardHeaders(request);
  headers.set("authorization", `Bearer ${token}`);
  return fetch(url, {
    method: request.method,
    headers,
    body,
    cache: "no-store",
    redirect: "manual",
    signal: AbortSignal.timeout(timeoutMs),
  });
}

function sensitiveJsonKeys(value: unknown, found = new Set<string>()): Set<string> {
  if (Array.isArray(value)) {
    for (const item of value) sensitiveJsonKeys(item, found);
  } else if (typeof value === "object" && value !== null) {
    for (const [key, item] of Object.entries(value)) {
      if (SENSITIVE_RESPONSE_KEYS.has(key)) found.add(key);
      sensitiveJsonKeys(item, found);
    }
  }
  return found;
}

async function copyResponse(
  upstream: Response,
  context: { path: string; method: string }
): Promise<NextResponse> {
  const body = await upstream.arrayBuffer();
  const contentType = upstream.headers.get("content-type") ?? "";
  if (/\b(?:application|text)\/(?:[^;]+\+)?json\b/i.test(contentType) && body.byteLength) {
    try {
      const payload = JSON.parse(new TextDecoder().decode(body)) as unknown;
      const keys = [...sensitiveJsonKeys(payload)].sort();
      if (keys.length) {
        console.error("BFF sensitive upstream response blocked", {
          path: context.path,
          method: context.method,
          sensitiveKeys: keys,
        });
        return NextResponse.json(
          { detail: { code: "BFF_SENSITIVE_RESPONSE_BLOCKED" } },
          { status: 502 }
        );
      }
    } catch {
      // Invalid JSON is copied as an ordinary upstream response. The scanner
      // never guesses about values in non-JSON payloads.
    }
  }
  const headers = new Headers();
  for (const name of ["content-type", "content-disposition", "x-request-id"]) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new NextResponse(body, { status: upstream.status, headers });
}

export async function proxyBackendRequest(request: NextRequest, path: string): Promise<NextResponse> {
  // Route authorization intentionally precedes CSRF, cookies and body reads.
  const policy = matchBffRoutePolicy(path, request.method);
  if (!policy) {
    return NextResponse.json(
      { detail: { code: "BFF_ROUTE_NOT_ALLOWED" } },
      { status: 404 }
    );
  }
  const rejected = validateMutationRequest(request);
  if (rejected) return rejected;
  const token = accessToken(request);
  if (!token) {
    return NextResponse.json({ detail: { code: "AUTH_REQUIRED" } }, { status: 401 });
  }
  const runtimeRejected = await enforceRuntimeSafety(request, policy, token);
  if (runtimeRejected) return runtimeRejected;
  const body = ["GET", "HEAD"].includes(request.method.toUpperCase())
    ? undefined
    : await request.arrayBuffer();
  try {
    const upstream = await forwardOnce(request, path, token, body, policy.timeoutMs);
    return copyResponse(upstream, { path, method: request.method });
  } catch (error) {
    const timedOut =
      error instanceof DOMException && ["TimeoutError", "AbortError"].includes(error.name);
    return NextResponse.json(
      {
        detail: {
          code: timedOut ? "BFF_UPSTREAM_TIMEOUT" : "BFF_UPSTREAM_UNAVAILABLE",
          retryable: request.method === "GET",
        },
      },
      { status: timedOut ? 504 : 502 }
    );
  }
}

export function backendUrl(path: string): string {
  return `${backendBaseUrl()}${path}`;
}

export { ACCESS_COOKIE, CSRF_COOKIE, REFRESH_COOKIE, type SessionResult };
