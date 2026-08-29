import { NextRequest, NextResponse } from "next/server";

import {
  accessToken,
  backendUrl,
  clearSessionCookies,
  refreshToken,
  validateMutationRequest,
} from "@/lib/server/bff";

export async function POST(request: NextRequest) {
  const rejected = validateMutationRequest(request);
  if (rejected) return rejected;
  const access = accessToken(request);
  const refresh = refreshToken(request);
  let revoked = false;
  if (access) {
    try {
      const response = await fetch(backendUrl("/api/auth/logout"), {
        method: "POST",
        headers: {
          authorization: `Bearer ${access}`,
          "content-type": "application/json",
          accept: "application/json",
        },
        body: JSON.stringify({ refresh_token: refresh ?? null }),
        cache: "no-store",
        signal: AbortSignal.timeout(15_000),
      });
      revoked = response.ok;
    } catch {
      revoked = false;
    }
  }
  const response = NextResponse.json({ status: "ok", revoked });
  clearSessionCookies(response);
  return response;
}
