"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  type ReactNode,
} from "react";
import {
  DEFAULT_LOCALE,
  registerRuntimeCatalog,
  type Locale,
  type MessageCatalog,
  type TranslationKey,
} from "@/lib/messages";

type TranslationParams = Record<string, string | number | null | undefined>;
type DateInput = string | number | Date | null | undefined;

export interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: TranslationKey, params?: TranslationParams) => string;
  formatDate: (value: DateInput, options?: Intl.DateTimeFormatOptions) => string;
  formatTime: (value: DateInput, options?: Intl.DateTimeFormatOptions) => string;
  formatDateTime: (
    value: DateInput,
    options?: Intl.DateTimeFormatOptions
  ) => string;
  formatNumber: (value: number | null | undefined, options?: Intl.NumberFormatOptions) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);
function interpolate(template: string, params?: TranslationParams) {
  if (!params) {
    return template;
  }
  return template.replace(/\{(\w+)\}/g, (_, key: string) => {
    const value = params[key];
    return value == null ? "" : String(value);
  });
}

function asDate(value: DateInput) {
  if (value == null || value === "") {
    return null;
  }
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function I18nProvider({
  children,
  initialLocale = DEFAULT_LOCALE,
  initialMessages,
}: {
  children: ReactNode;
  initialLocale?: Locale;
  initialMessages: MessageCatalog;
}) {
  const locale = initialLocale;
  registerRuntimeCatalog(locale, initialMessages);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((nextLocale: Locale) => {
    document.cookie = `nids-locale=${encodeURIComponent(nextLocale)}; Path=/; Max-Age=31536000; SameSite=Lax`;
    document.documentElement.lang = nextLocale;
    window.location.reload();
  }, []);

  const value = useMemo<I18nContextValue>(() => {
    const t = (key: TranslationKey, params?: TranslationParams) => {
      const template = initialMessages[key] ?? key;
      return interpolate(template, params);
    };

    const formatDate = (
      input: DateInput,
      options: Intl.DateTimeFormatOptions = { year: "numeric", month: "short", day: "numeric" }
    ) => {
      const date = asDate(input);
      return date ? new Intl.DateTimeFormat(locale, options).format(date) : "—";
    };

    const formatTime = (
      input: DateInput,
      options: Intl.DateTimeFormatOptions = {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }
    ) => {
      const date = asDate(input);
      return date ? new Intl.DateTimeFormat(locale, options).format(date) : "—";
    };

    const formatDateTime = (
      input: DateInput,
      options: Intl.DateTimeFormatOptions = {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }
    ) => {
      const date = asDate(input);
      return date ? new Intl.DateTimeFormat(locale, options).format(date) : "—";
    };

    const formatNumber = (
      input: number | null | undefined,
      options?: Intl.NumberFormatOptions
    ) =>
      input == null || Number.isNaN(input)
        ? "—"
        : new Intl.NumberFormat(locale, options).format(input);

    return {
      locale,
      setLocale,
      t,
      formatDate,
      formatTime,
      formatDateTime,
      formatNumber,
    };
  }, [initialMessages, locale, setLocale]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const context = useContext(I18nContext);
  if (!context) {
    throw new Error("useI18n must be used inside I18nProvider");
  }
  return context;
}

export type { Locale, TranslationKey };
