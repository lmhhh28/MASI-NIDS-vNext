const MAX_SAFE_RUN_ID_LENGTH = 128;
const DEFAULT_DEMO_ONLINE_RUN_ID_BASE = "online-demo";

function trimRunIdSeparators(value: string) {
  return value.replace(/^[._-]+/, "").replace(/[._-]+$/, "");
}

export function normalizeDemoRunIdBase(
  value: string,
  fallback = DEFAULT_DEMO_ONLINE_RUN_ID_BASE,
): string {
  const normalized = trimRunIdSeparators(
    value
      .trim()
      .replace(/[^A-Za-z0-9._-]+/g, "-")
      .replace(/[._-]{2,}/g, "-"),
  );
  if (/^[A-Za-z0-9]/.test(normalized)) {
    return normalized;
  }
  return fallback === value ? DEFAULT_DEMO_ONLINE_RUN_ID_BASE : normalizeDemoRunIdBase(fallback);
}

function utcTimestamp(now: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return [
    now.getUTCFullYear(),
    pad(now.getUTCMonth() + 1),
    pad(now.getUTCDate()),
    "t",
    pad(now.getUTCHours()),
    pad(now.getUTCMinutes()),
    pad(now.getUTCSeconds()),
    "z",
  ].join("");
}

function defaultRandomSuffix(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi?.randomUUID) {
    return cryptoApi.randomUUID().replace(/[^A-Za-z0-9]/g, "").slice(0, 8);
  }
  if (cryptoApi?.getRandomValues) {
    const bytes = new Uint8Array(4);
    cryptoApi.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  return Math.random().toString(36).replace(/[^A-Za-z0-9]/g, "").slice(0, 8);
}

export function createDemoOnlineRunId(
  base: string,
  options: { now?: Date; randomSuffix?: () => string } = {},
): string {
  const timestamp = utcTimestamp(options.now ?? new Date());
  const suffix = (options.randomSuffix?.() ?? defaultRandomSuffix())
    .replace(/[^A-Za-z0-9]/g, "")
    .slice(0, 8) || "00000000";
  const maxBaseLength = MAX_SAFE_RUN_ID_LENGTH - timestamp.length - suffix.length - 2;
  const basePrefix = trimRunIdSeparators(normalizeDemoRunIdBase(base).slice(0, maxBaseLength));
  return `${basePrefix || DEFAULT_DEMO_ONLINE_RUN_ID_BASE}-${timestamp}-${suffix}`;
}
