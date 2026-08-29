import type { zh } from "@/lib/messages.zh";

export const LOCALES = ["zh-CN", "en-US"] as const;
export type Locale = (typeof LOCALES)[number];
export type TranslationKey = keyof typeof zh;
export type MessageCatalog = Readonly<Record<TranslationKey, string>>;

export const DEFAULT_LOCALE: Locale = "zh-CN";
export const LOCALE_STORAGE_KEY = "nids-locale";

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && LOCALES.includes(value as Locale);
}

const runtimeCatalogs: Partial<Record<Locale, MessageCatalog>> = {};

/**
 * Register the one catalog serialized by the server for this browser session.
 * Keeping the catalog behind this small registry lets non-React error helpers
 * translate structured error codes without importing both full dictionaries.
 */
export function registerRuntimeCatalog(locale: Locale, catalog: MessageCatalog) {
  runtimeCatalogs[locale] = catalog;
}

export function runtimeMessage(locale: Locale, key: TranslationKey) {
  return runtimeCatalogs[locale]?.[key];
}
