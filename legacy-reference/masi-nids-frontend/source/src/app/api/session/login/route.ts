import { NextRequest, NextResponse } from "next/server";

import {
  backendMe,
  backendUrl,
  csrfToken,
  setCsrfCookie,
  setSessionCookies,
  type SessionResult,
  validateMutationRequest,
} from "@/lib/server/bff";

export async function POST(request: NextRequest) {
  const rejected = validateMutationRequest(request);
  if (rejected) return rejected;
  const csrf = csrfToken(request);
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return NextResponse.json({ detail: { code: "INVALID_JSON" } }, { status: 400 });
  }
  try {
    const login = await fetch(backendUrl("/api/auth/login"), {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
    if (!login.ok) {
      const response = new NextResponse(await login.arrayBuffer(), {
        status: login.status,
        headers: { "content-type": login.headers.get("content-type") ?? "application/json" },
      });
      setCsrfCookie(response, csrf);
      return response;
    }
    const tokens = await login.json();
    const me = await backendMe(tokens.access_token);
    if (!me.ok) {
      return NextResponse.json({ detail: { code: "SESSION_PROFILE_UNAVAILABLE" } }, { status: 502 });
    }
    const session: SessionResult = { tokens, user: await me.json() };
    const response = NextResponse.json({
      authenticated: true,
      user: session.user,
      csrf_token: csrf,
    });
    setSessionCookies(response, session, csrf);
    return response;
  } catch (error) {
    const timeout = error instanceof DOMException && error.name === "TimeoutError";
    return NextResponse.json(
      { detail: { code: timeout ? "BFF_UPSTREAM_TIMEOUT" : "BFF_UPSTREAM_UNAVAILABLE" } },
      { status: timeout ? 504 : 502 }
    );
  }
}
