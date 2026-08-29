import { NextRequest, NextResponse } from "next/server";

import {
  accessToken,
  backendMe,
  clearSessionCookies,
  csrfToken,
  refreshToken,
  setCsrfCookie,
} from "@/lib/server/bff";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const csrf = csrfToken(request);
  const access = accessToken(request);
  if (access) {
    try {
      const me = await backendMe(access);
      if (me.ok) {
        const response = NextResponse.json({
          authenticated: true,
          user: await me.json(),
          csrf_token: csrf,
        });
        setCsrfCookie(response, csrf);
        return response;
      }
      if (me.status !== 401) {
        const response = NextResponse.json(
          { detail: { code: "SESSION_STATE_UNKNOWN" } },
          { status: 503 }
        );
        setCsrfCookie(response, csrf);
        return response;
      }
    } catch {
      const response = NextResponse.json(
        { detail: { code: "SESSION_STATE_UNKNOWN" } },
        { status: 503 }
      );
      setCsrfCookie(response, csrf);
      return response;
    }
  }

  const refresh = refreshToken(request);
  if (refresh) {
    const response = NextResponse.json({
      authenticated: false,
      user: null,
      csrf_token: csrf,
      refresh_required: true,
    });
    setCsrfCookie(response, csrf);
    return response;
  }

  const response = NextResponse.json({ authenticated: false, user: null, csrf_token: csrf });
  clearSessionCookies(response);
  setCsrfCookie(response, csrf);
  return response;
}
