export interface HttpErrorResponse {
  data?: unknown;
  status?: number;
}

export interface HttpErrorConfig {
  method?: string;
}

export interface HttpErrorLike extends Error {
  code?: string;
  config?: HttpErrorConfig;
  response?: HttpErrorResponse;
  requestId?: string;
  manualRetryRequired?: boolean;
  isAxiosError?: boolean;
}

export class ApiClientError extends Error implements HttpErrorLike {
  readonly code?: string;
  readonly config?: HttpErrorConfig;
  readonly response?: HttpErrorResponse;
  readonly requestId?: string;
  readonly manualRetryRequired: boolean;

  constructor(
    message: string,
    options: {
      code?: string;
      config?: HttpErrorConfig;
      response?: HttpErrorResponse;
      requestId?: string;
      manualRetryRequired?: boolean;
      cause?: unknown;
    } = {},
  ) {
    super(message, { cause: options.cause });
    this.name = "ApiClientError";
    this.code = options.code;
    this.config = options.config;
    this.response = options.response;
    this.requestId = options.requestId;
    this.manualRetryRequired = options.manualRetryRequired ?? false;
  }
}

export function isHttpErrorLike(value: unknown): value is HttpErrorLike {
  if (value instanceof ApiClientError) return true;
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<HttpErrorLike>;
  return candidate.isAxiosError === true || (
    typeof candidate.name === "string" &&
    candidate.name === "AxiosError"
  );
}

export function hasNoHttpResponse(value: unknown) {
  return isHttpErrorLike(value) && !value.response;
}
