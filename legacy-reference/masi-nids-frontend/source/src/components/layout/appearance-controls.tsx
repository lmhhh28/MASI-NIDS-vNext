"use client";

import { Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n";

export function AppearanceControls() {
  const { resolvedTheme, setTheme } = useTheme();
  const { locale, setLocale, t } = useI18n();
  const isDark = resolvedTheme !== "light";
  const nextTheme = isDark ? "light" : "dark";

  return (
    <div className="flex shrink-0 items-center gap-1.5">
      <Button
        type="button"
        variant="outline"
        size="icon"
        aria-label={t("appearance.themeToggle")}
        title={isDark ? t("appearance.themeDark") : t("appearance.themeLight")}
        className="h-11 w-11 sm:h-8 sm:w-8"
        onClick={() => setTheme(nextTheme)}
      >
        {isDark ? (
          <Moon className="h-4 w-4" aria-hidden="true" />
        ) : (
          <Sun className="h-4 w-4" aria-hidden="true" />
        )}
      </Button>

      <div
        aria-label={t("appearance.languageLabel")}
        title={t("appearance.languageLabel")}
        className="inline-flex min-h-11 overflow-hidden rounded-lg border border-border bg-background text-xs sm:min-h-8"
        role="group"
      >
        <button
          type="button"
          className={`min-h-11 min-w-11 px-2.5 font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background sm:min-h-8 ${
            locale === "zh-CN"
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:bg-muted hover:text-foreground"
          }`}
          aria-label={t("appearance.languageZh")}
          onClick={() => setLocale("zh-CN")}
        >
          中文
        </button>
        <button
          type="button"
          className={`min-h-11 min-w-11 border-l border-border px-2.5 font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background sm:min-h-8 ${
            locale === "en-US"
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:bg-muted hover:text-foreground"
          }`}
          aria-label={t("appearance.languageEn")}
          onClick={() => setLocale("en-US")}
        >
          EN
        </button>
      </div>
    </div>
  );
}
