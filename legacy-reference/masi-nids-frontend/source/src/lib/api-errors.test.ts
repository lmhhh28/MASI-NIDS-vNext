import { describe, expect, it } from "vitest";

import { parseApiError } from "@/lib/api-errors";
import { ApiClientError } from "@/lib/http-error";

describe("parseApiError", () => {
  it("preserves recovery metadata from structured backend details", () => {
    const error = new ApiClientError("conflict", {
      code: "ERR_BAD_RESPONSE",
      response: {
        data: {
          detail: {
            code: "IDEMPOTENCY_NEEDS_RECONCILE",
            request_id: "req-1",
            deployment_id: "dep-1",
            status: "unknown",
            lease_token: "lease-1",
            retryable: false,
          },
        },
        status: 409,
      },
    });

    expect(parseApiError(error)).toMatchObject({
      status: 409,
      code: "IDEMPOTENCY_NEEDS_RECONCILE",
      requestId: "req-1",
      deploymentId: "dep-1",
      operationStatus: "unknown",
      lease: "lease-1",
      retryable: false,
    });
  });
});
