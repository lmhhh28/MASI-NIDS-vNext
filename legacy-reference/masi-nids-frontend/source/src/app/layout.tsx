import type { Metadata } from "next";
import { cookies } from "next/headers";

import { ThemeProvider } from "@/components/theme-provider";
import { Toaster } from "@/components/ui/sonner";
import { I18nProvider } from "@/lib/i18n";
import { SkipLink } from "@/components/layout/skip-link";
import { DEFAULT_LOCALE, isLocale } from "@/lib/messages";
import { loadMessageCatalog } from "@/lib/server/message-catalog";
import "./globals.css";

export const metadata: Metadata = {
  title: "MASI-NIDS Console",
  description:
    "Network Intrusion Detection System — Autoencoder-based anomaly detection console with P4 switch management.",
};

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const cookieStore = await cookies();
  const requestedLocale = cookieStore.get("nids-locale")?.value;
  const locale = isLocale(requestedLocale) ? requestedLocale : DEFAULT_LOCALE;
  const messageCatalog = await loadMessageCatalog(locale);
  return (
    <html lang={locale} className="h-full antialiased" suppressHydrationWarning>
      <body className="flex min-h-dvh flex-col bg-background text-foreground">
        <SkipLink>
          {messageCatalog["common.skipToContent"]}
        </SkipLink>
        <ThemeProvider>
          <I18nProvider initialLocale={locale} initialMessages={messageCatalog}>
            {children}
            <Toaster richColors position="top-right" />
          </I18nProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
