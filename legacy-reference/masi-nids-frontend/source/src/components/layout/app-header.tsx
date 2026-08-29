"use client";

import { usePathname } from "next/navigation";
import Link from "next/link";
import { ChevronRight } from "lucide-react";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { AppearanceControls } from "@/components/layout/appearance-controls";
import { useI18n, type TranslationKey } from "@/lib/i18n";

const nameMap: Record<string, TranslationKey> = {
  "": "common.dashboard",
  workflows: "common.workflows",
  reviews: "common.reviews",
  audit: "common.auditLog",
  p4: "common.p4Operations",
  mcp: "common.mcpTools",
  admin: "common.admin",
  users: "common.users",
  llm: "common.llmConfig",
  risk: "common.riskPolicy",
  targets: "common.protectedTargets",
  templates: "common.templates",
  demo: "common.demoTraffic",
};

export function AppHeader() {
  const pathname = usePathname();
  const { t } = useI18n();
  const segments = pathname.split("/").filter(Boolean);

  const crumbs = segments.map((seg, i) => ({
    label: nameMap[seg] ? t(nameMap[seg]) : seg,
    href: "/" + segments.slice(0, i + 1).join("/"),
    isLast: i === segments.length - 1,
  }));

  return (
    <header className="sticky top-0 z-20 flex h-12 min-w-0 items-center gap-3 border-b border-border bg-background/80 px-3 backdrop-blur-sm sm:px-4">
      <SidebarTrigger />
      <nav aria-label="Breadcrumb" className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden text-sm">
        <Link
          href="/"
          prefetch={false}
          aria-current={segments.length === 0 ? "page" : undefined}
          className="shrink-0 text-muted-foreground transition-colors hover:text-foreground"
        >
          {t("common.home")}
        </Link>
        {crumbs.map((c) => (
          <span key={c.href} className="flex min-w-0 items-center gap-1">
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground/50" />
            {c.isLast ? (
              <span aria-current="page" className="truncate font-medium text-foreground">
                {c.label}
              </span>
            ) : (
              <Link
                href={c.href}
                prefetch={false}
                className="truncate text-muted-foreground transition-colors hover:text-foreground"
              >
                {c.label}
              </Link>
            )}
          </span>
        ))}
      </nav>
      <AppearanceControls />
    </header>
  );
}
