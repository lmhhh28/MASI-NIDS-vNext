import {
  DEFAULT_LOCALE,
  runtimeMessage,
  type Locale,
  type TranslationKey,
} from "@/lib/messages";
import { isHttpErrorLike } from "@/lib/http-error";

export interface ApiErrorInfo {
  status?: number;
  code?: string;
  message: string;
  errors: string[];
  requestId?: string;
  deploymentId?: string;
  operationStatus?: string;
  lease?: string;
  retryable?: boolean;
  idempotencyKey?: string;
  manualRetryRequired?: boolean;
}

function translatedApiMessage(code: string | undefined, locale: Locale) {
  if (!code) {
    return undefined;
  }
  const key = `api.${code}` as TranslationKey;
  return runtimeMessage(locale, key);
}

function requestFailedMessage(locale: Locale) {
  return runtimeMessage(locale, "api.requestFailed") ??
    (locale === "zh-CN" ? "请求失败" : "Request failed");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function firstString(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }
  return undefined;
}

function firstBoolean(...values: unknown[]): boolean | undefined {
  for (const value of values) {
    if (typeof value === "boolean") return value;
  }
  return undefined;
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => (typeof item === "string" ? item : JSON.stringify(item)))
    .filter(Boolean);
}

function buildApiErrorInfo(
  payload: unknown,
  locale: Locale,
  status?: number,
  fallbackMessage?: string
): ApiErrorInfo {
  const requestFailed = requestFailedMessage(locale);
  const data = isRecord(payload) ? payload : undefined;
  const detail = isRecord(data?.detail) ? data.detail : undefined;
  const code = firstString(data?.code, detail?.code);
  const errors = [...stringArray(data?.errors), ...stringArray(detail?.errors)];
  const detailText = typeof data?.detail === "string" ? data.detail : undefined;
  const message =
    translatedApiMessage(code, locale) ??
    firstString(
      data?.message,
      detail?.message,
      detailText,
      errors.join("; "),
      fallbackMessage
    ) ??
    requestFailed;

  return {
    status,
    code,
    message,
    errors,
    requestId: firstString(data?.request_id, detail?.request_id),
    deploymentId: firstString(data?.deployment_id, detail?.deployment_id),
    operationStatus: firstString(data?.operation_status, detail?.operation_status, detail?.status),
    lease: firstString(data?.lease, data?.lease_token, detail?.lease, detail?.lease_token),
    retryable: firstBoolean(data?.retryable, detail?.retryable),
    idempotencyKey: firstString(data?.idempotency_key, detail?.idempotency_key),
  };
}

async function readResponsePayload(response: Response): Promise<unknown> {
  try {
    const text = await response.clone().text();
    if (!text) {
      return undefined;
    }
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  } catch {
    return undefined;
  }
}

export async function parseFetchApiError(
  response: Response,
  locale: Locale = DEFAULT_LOCALE
): Promise<ApiErrorInfo> {
  const payload = await readResponsePayload(response);
  return buildApiErrorInfo(payload, locale, response.status, response.statusText);
}

export function parseApiError(
  error: unknown,
  locale: Locale = DEFAULT_LOCALE
): ApiErrorInfo {
  const requestFailed = requestFailedMessage(locale);
  const fallback = error instanceof Error ? error.message : requestFailed;
  if (!isHttpErrorLike(error)) {
    return { message: fallback, errors: [] };
  }

  const info = buildApiErrorInfo(error.response?.data, locale, error.response?.status, fallback);
  if (error.requestId && !info.requestId) info.requestId = error.requestId;
  if (error.manualRetryRequired) info.manualRetryRequired = true;
  return info;
}

export function isApiErrorInfo(value: unknown): value is ApiErrorInfo {
  return (
    isRecord(value) &&
    typeof value.message === "string" &&
    Array.isArray(value.errors) &&
    (value.status === undefined || typeof value.status === "number") &&
    (value.code === undefined || typeof value.code === "string")
  );
}

export function apiErrorMessage(
  error: unknown | ApiErrorInfo,
  locale: Locale = DEFAULT_LOCALE
): string {
  if (isApiErrorInfo(error)) {
    return error.code ? `${error.code}: ${error.message}` : error.message;
  }
  const info = parseApiError(error, locale);
  return info.code ? `${info.code}: ${info.message}` : info.message;
}
