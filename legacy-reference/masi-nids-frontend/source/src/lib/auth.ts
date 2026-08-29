"use client";

import { useEffect } from "react";
import { create } from "zustand";

import { parseApiError, parseFetchApiError, type ApiErrorInfo } from "@/lib/api-errors";
import { DEFAULT_LOCALE, type Locale } from "@/lib/messages";
import type { UserInfo } from "@/types/api";

export type SessionStatus = "idle" | "loading" | "authenticated" | "unauthenticated";

interface SessionPayload {
  authenticated: boolean;
  user: UserInfo | null;
  csrf_token: string;
  refresh_required?: boolean;
}

interface AuthState {
  sessionStatus: SessionStatus;
  user: UserInfo | null;
  csrfToken: string | null;
  bootstrap: () => Promise<boolean>;
  login: (
    username: string,
    password: string,
    locale?: Locale
  ) => Promise<{ ok: true } | { ok: false; error: ApiErrorInfo }>;
  refresh: () => Promise<boolean>;
  logout: () => Promise<void>;
  markUnauthenticated: () => void;
  isAdmin: () => boolean;
}

let bootstrapPromise: Promise<boolean> | null = null;
let refreshPromise: Promise<boolean> | null = null;
const REFRESH_EPOCH_KEY = "nids-session-refresh-epoch";
const REFRESH_LOCK_NAME = "nids-session-refresh";
const REFRESH_LEASE_KEY = "nids-session-refresh-lease";
const REFRESH_CHANNEL_NAME = "nids-session-refresh-events";
const REFRESH_LEASE_MS = 12_000;

async function readSession(response: Response): Promise<SessionPayload | null> {
  if (!response.ok) return null;
  return (await response.json()) as SessionPayload;
}

function applySession(payload: SessionPayload | null): boolean {
  if (!payload?.authenticated || !payload.user) {
    useAuthStore.setState({
      sessionStatus: "unauthenticated",
      user: null,
      csrfToken: payload?.csrf_token ?? null,
    });
    return false;
  }
  useAuthStore.setState({
    sessionStatus: "authenticated",
    user: payload.user,
    csrfToken: payload.csrf_token,
  });
  return true;
}

async function fetchSession(): Promise<SessionPayload | null> {
  try {
    return await readSession(
      await fetch("/api/session", { cache: "no-store", credentials: "same-origin" })
    );
  } catch {
    return null;
  }
}

async function postRefresh(): Promise<boolean> {
  const csrf = useAuthStore.getState().csrfToken;
  if (!csrf) return applySession(await fetchSession());
  try {
    const response = await fetch("/api/session/refresh", {
      method: "POST",
      headers: { "x-csrf-token": csrf },
      credentials: "same-origin",
    });
    if (response.status === 409) {
      // Another BFF instance rotated the one-time token. It cannot return the
      // successor token to this response; wait for the winning tab's shared
      // cookie update, then bootstrap from the explicit session endpoint.
      const deadline = Date.now() + 2_000;
      while (Date.now() < deadline) {
        const session = await fetchSession();
        if (session?.authenticated) return applySession(session);
        await new Promise((resolve) => window.setTimeout(resolve, 50));
      }
      return false;
    }
    const payload = await readSession(response);
    if (!payload) {
      if (response.status === 401) applySession(null);
      return false;
    }
    if (typeof window !== "undefined") {
      publishRefreshComplete();
    }
    return applySession(payload);
  } catch {
    return false;
  }
}

interface RefreshLease {
  owner: string;
  expiresAt: number;
}

function readRefreshLease(): RefreshLease | null {
  try {
    const value = JSON.parse(window.localStorage.getItem(REFRESH_LEASE_KEY) ?? "null") as unknown;
    if (
      typeof value === "object" &&
      value !== null &&
      typeof (value as RefreshLease).owner === "string" &&
      typeof (value as RefreshLease).expiresAt === "number"
    ) {
      return value as RefreshLease;
    }
  } catch {
    // A malformed lease is treated as expired and overwritten atomically by
    // the next contender.
  }
  return null;
}

function publishRefreshComplete(): void {
  const epoch = String(Date.now());
  window.localStorage.setItem(REFRESH_EPOCH_KEY, epoch);
  if (typeof BroadcastChannel !== "undefined") {
    const channel = new BroadcastChannel(REFRESH_CHANNEL_NAME);
    channel.postMessage({ type: "complete", epoch });
    channel.close();
  }
}

function waitForRefreshSignal(timeoutMs: number): Promise<void> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      window.removeEventListener("storage", onStorage);
      channel?.close();
      window.clearTimeout(timer);
      resolve();
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === REFRESH_EPOCH_KEY || event.key === REFRESH_LEASE_KEY) finish();
    };
    const channel = typeof BroadcastChannel === "undefined"
      ? null
      : new BroadcastChannel(REFRESH_CHANNEL_NAME);
    if (channel) channel.onmessage = finish;
    window.addEventListener("storage", onStorage, { once: true });
    const timer = window.setTimeout(finish, timeoutMs);
  });
}

async function coordinatedRefreshWithoutWebLocks(observedEpoch: number): Promise<boolean> {
  const owner = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random()}`;
  const deadline = Date.now() + REFRESH_LEASE_MS + 2_000;
  while (Date.now() < deadline) {
    const currentEpoch = Number(window.localStorage.getItem(REFRESH_EPOCH_KEY) ?? 0);
    if (currentEpoch > observedEpoch) return applySession(await fetchSession());

    const now = Date.now();
    const lease = readRefreshLease();
    if (!lease || lease.expiresAt <= now) {
      const candidate: RefreshLease = { owner, expiresAt: now + REFRESH_LEASE_MS };
      window.localStorage.setItem(REFRESH_LEASE_KEY, JSON.stringify(candidate));
      if (readRefreshLease()?.owner === owner) {
        try {
          return await postRefresh();
        } finally {
          if (readRefreshLease()?.owner === owner) {
            window.localStorage.removeItem(REFRESH_LEASE_KEY);
          }
        }
      }
    }
    const waitMs = Math.max(25, Math.min(250, (lease?.expiresAt ?? now + 250) - now));
    await waitForRefreshSignal(waitMs);
  }
  return applySession(await fetchSession());
}

async function coordinatedRefresh(): Promise<boolean> {
  const observedEpoch =
    typeof window === "undefined"
      ? 0
      : Number(window.localStorage.getItem(REFRESH_EPOCH_KEY) ?? 0);
  const run = async () => {
    const currentEpoch =
      typeof window === "undefined"
        ? 0
        : Number(window.localStorage.getItem(REFRESH_EPOCH_KEY) ?? 0);
    if (currentEpoch > observedEpoch) {
      return applySession(await fetchSession());
    }
    return postRefresh();
  };
  if (typeof navigator !== "undefined" && navigator.locks?.request) {
    return navigator.locks.request(REFRESH_LOCK_NAME, run);
  }
  return coordinatedRefreshWithoutWebLocks(observedEpoch);
}

export const useAuthStore = create<AuthState>()((set, get) => ({
  sessionStatus: "idle",
  user: null,
  csrfToken: null,

  bootstrap: async () => {
    if (bootstrapPromise) return bootstrapPromise;
    set({ sessionStatus: "loading" });
    bootstrapPromise = fetchSession()
      .then(async (payload) => {
        if (payload?.refresh_required) {
          set({ csrfToken: payload.csrf_token });
          return coordinatedRefresh();
        }
        return applySession(payload);
      })
      .finally(() => {
        bootstrapPromise = null;
      });
    return bootstrapPromise;
  },

  login: async (
    username: string,
    password: string,
    locale: Locale = DEFAULT_LOCALE
  ) => {
    let csrf = get().csrfToken;
    if (!csrf) {
      const session = await fetchSession();
      applySession(session);
      csrf = session?.csrf_token ?? null;
    }
    if (!csrf) {
      return {
        ok: false as const,
        error: { message: "Unable to initialize a secure session.", errors: [] },
      };
    }
    try {
      const response = await fetch("/api/session/login", {
        method: "POST",
        headers: { "content-type": "application/json", "x-csrf-token": csrf },
        credentials: "same-origin",
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) {
        return { ok: false as const, error: await parseFetchApiError(response, locale) };
      }
      applySession((await response.json()) as SessionPayload);
      return { ok: true as const };
    } catch (error) {
      set({ sessionStatus: "unauthenticated", user: null });
      return { ok: false as const, error: parseApiError(error, locale) };
    }
  },

  refresh: async () => {
    if (refreshPromise) return refreshPromise;
    refreshPromise = coordinatedRefresh().finally(() => {
      refreshPromise = null;
    });
    return refreshPromise;
  },

  logout: async () => {
    const csrf = get().csrfToken;
    set({ sessionStatus: "unauthenticated", user: null, csrfToken: null });
    try {
      if (csrf) {
        await fetch("/api/session/logout", {
          method: "POST",
          headers: { "x-csrf-token": csrf },
          credentials: "same-origin",
        });
      }
    } catch {
      // Local logout is authoritative even when revocation cannot be reached.
    } finally {
      set({ sessionStatus: "unauthenticated", user: null, csrfToken: null });
    }
  },

  markUnauthenticated: () =>
    set({ sessionStatus: "unauthenticated", user: null, csrfToken: null }),
  isAdmin: () => get().user?.role === "admin",
}));

export function useAuthHydrated(): boolean {
  const status = useAuthStore((state) => state.sessionStatus);
  const bootstrap = useAuthStore((state) => state.bootstrap);
  useEffect(() => {
    if (status === "idle") void bootstrap();
  }, [bootstrap, status]);
  return status !== "idle" && status !== "loading";
}
