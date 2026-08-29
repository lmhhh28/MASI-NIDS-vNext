"use client";

import { useAuthStore } from "@/lib/auth";
import { ApiClientError, isHttpErrorLike } from "@/lib/http-error";

export { type ApiErrorInfo, apiErrorMessage, parseApiError } from "@/lib/api-errors";

type QueryValue = string | number | boolean | null | undefined;
// Keep unannotated endpoint calls compatible while typed endpoints migrate
// incrementally to explicit response generics.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type UntypedApiData = any;

interface RequestConfig {
  params?: Record<string, QueryValue | QueryValue[]>;
  signal?: AbortSignal;
  headers?: HeadersInit;
  timeout?: number;
  _authRetry?: boolean;
}

interface RequestOptions extends RequestConfig {
  url: string;
  method?: string;
  data?: unknown;
}

export interface ApiResponse<T> {
  data: T;
  status: number;
  headers: Headers;
}

function appendQuery(url: string, params?: RequestConfig["params"]) {
  if (!params) return url;
  const search = new URLSearchParams();
  for (const [key, rawValue] of Object.entries(params)) {
    const values = Array.isArray(rawValue) ? rawValue : [rawValue];
    for (const value of values) {
      if (value != null) search.append(key, String(value));
    }
  }
  const query = search.toString();
  return query ? `${url}${url.includes("?") ? "&" : "?"}${query}` : url;
}

function responseMessage(payload: unknown, fallback: string) {
  if (typeof payload !== "object" || payload === null) return fallback;
  const data = payload as Record<string, unknown>;
  if (typeof data.message === "string") return data.message;
  if (typeof data.detail === "string") return data.detail;
  if (typeof data.detail === "object" && data.detail !== null) {
    const detail = data.detail as Record<string, unknown>;
    if (typeof detail.message === "string") return detail.message;
    if (typeof detail.code === "string") return detail.code;
  }
  return fallback;
}

async function readPayload(response: Response) {
  if (response.status === 204) return undefined;
  const text = await response.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function request<T = UntypedApiData>(options: RequestOptions): Promise<ApiResponse<T>> {
  const method = (options.method ?? "GET").toUpperCase();
  const path = options.url.startsWith("/") ? options.url : `/${options.url}`;
  const timeoutMs = options.timeout ?? (/^\/(p4|admin)(\/|$)/.test(path) ? 30_000 : 15_000);
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(options.signal?.reason);
  options.signal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  const headers = new Headers(options.headers);
  headers.delete("Authorization");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = useAuthStore.getState().csrfToken;
    if (csrf) headers.set("X-CSRF-Token", csrf);
    if (options.data !== undefined && !(options.data instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
  }

  const config = { method: method.toLowerCase() };
  try {
    const response = await fetch(appendQuery(`/api${path}`, options.params), {
      method,
      headers,
      credentials: "same-origin",
      signal: controller.signal,
      body:
        options.data === undefined || ["GET", "HEAD"].includes(method)
          ? undefined
          : options.data instanceof FormData
            ? options.data
            : JSON.stringify(options.data),
    });
    const payload = await readPayload(response);

    if (response.status === 401 && !options._authRetry) {
      const refreshed = await useAuthStore.getState().refresh();
      if (["GET", "HEAD"].includes(method) && refreshed) {
        return request<T>({ ...options, _authRetry: true });
      }
      if (!["GET", "HEAD"].includes(method)) {
        const requestId = response.headers.get("x-request-id") ?? undefined;
        throw new ApiClientError("Authentication changed; confirm this operation and retry.", {
          code: "AUTH_MUTATION_RETRY_REQUIRED",
          config,
          response: { data: payload, status: 401 },
          requestId,
          manualRetryRequired: true,
        });
      }
      if (!refreshed) {
        useAuthStore.getState().markUnauthenticated();
        if (window.location.pathname !== "/login") window.location.assign("/login");
      }
    }

    if (!response.ok) {
      throw new ApiClientError(responseMessage(payload, response.statusText || "Request failed"), {
        code: "ERR_BAD_RESPONSE",
        config,
        response: { data: payload, status: response.status },
      });
    }
    return { data: payload as T, status: response.status, headers: response.headers };
  } catch (error) {
    if (error instanceof ApiClientError) throw error;
    throw new ApiClientError(timedOut ? "Request timed out" : "Network request failed", {
      code: timedOut ? "ECONNABORTED" : controller.signal.aborted ? "ERR_CANCELED" : "ERR_NETWORK",
      config,
      cause: error,
    });
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abortFromCaller);
  }
}

const api = {
  request,
  get: <T = UntypedApiData>(url: string, config: RequestConfig = {}) =>
    request<T>({ ...config, url, method: "GET" }),
  post: <T = UntypedApiData>(url: string, data?: unknown, config: RequestConfig = {}) =>
    request<T>({ ...config, url, method: "POST", data }),
  put: <T = UntypedApiData>(url: string, data?: unknown, config: RequestConfig = {}) =>
    request<T>({ ...config, url, method: "PUT", data }),
  patch: <T = UntypedApiData>(url: string, data?: unknown, config: RequestConfig = {}) =>
    request<T>({ ...config, url, method: "PATCH", data }),
  delete: <T = UntypedApiData>(url: string, config: RequestConfig = {}) =>
    request<T>({ ...config, url, method: "DELETE" }),
};

export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2 || !isHttpErrorLike(error)) return false;
  const method = (error.config?.method ?? "get").toLowerCase();
  if (method !== "get") return false;
  if (!error.response) return error.code !== "ERR_CANCELED";
  return error.response.status === 502 || error.response.status === 503;
}

export default api;
