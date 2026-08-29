import { NextRequest, NextResponse } from "next/server";

import {
  clearSessionCookies,
  csrfToken,
  refreshSession,
  refreshToken,
  setCsrfCookie,
  setSessionCookies,
  validateMutationRequest,
} from "@/lib/server/bff";

export async function POST(request: NextRequest) {
  const rejected = validateMutationRequest(request);
  if (rejected) return rejected;
  const token = refreshToken(request);
  if (!token) {
    return NextResponse.json({ detail: { code: "INVALID_REFRESH_TOKEN" } }, { status: 401 });
  }
  const result = await refreshSession(token, request.headers.get("x-request-id") ?? undefined);
  if (!result.ok) {
    const csrf = csrfToken(request);
    const response = NextResponse.json(
      result.failure.status === 409
        ? {
            authenticated: false,
            user: null,
            csrf_token: csrf,
            refresh_required: true,
            detail: { code: "REFRESH_ALREADY_ROTATED", retryable: true },
          }
        : result.failure.payload ?? { detail: { code: "INVALID_REFRESH_TOKEN" } },
      { status: result.failure.status }
    );
    if (result.failure.status === 401) clearSessionCookies(response);
    else setCsrfCookie(response, csrf);
    return response;
  }
  const csrf = csrfToken(request);
  const response = NextResponse.json({
    authenticated: true,
    user: result.session.user,
    csrf_token: csrf,
  });
  setSessionCookies(response, result.session, csrf);
  return response;
}
