import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Parse an unknown value as a positive integer, returning null for any
 * non-positive or non-integer input. Trims whitespace from string input
 * and rejects values containing non-digit characters.
 *
 * Used by structured TTL inputs across workflow create/review forms and
 * manual P4 entry forms; keep behavior stable so all surfaces validate
 * identically.
 */
export function asPositiveInteger(value: unknown): number | null {
  if (typeof value === "number" && Number.isInteger(value) && value > 0) {
    return value;
  }
  if (typeof value === "string" && /^\d+$/.test(value.trim())) {
    const parsed = Number(value.trim());
    return parsed > 0 ? parsed : null;
  }
  return null;
}

export function parseFiniteNumberDraft(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") {
    return null;
  }
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

export function parsePositiveIntegerDraft(value: string): number | null {
  const parsed = parseFiniteNumberDraft(value);
  return parsed !== null && Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}
