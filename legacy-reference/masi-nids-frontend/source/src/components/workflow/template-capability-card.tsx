"use client";

import { useState } from "react";
import Link from "next/link";
import { ChevronDown } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import { useTemplates } from "@/hooks/use-templates";
import type { WorkflowTemplateOption } from "@/types/api";

type EntryRouteKey =
  | "dashboardInspect"
  | "dashboardBlock"
  | "p4Deployments"
  | "p4SwitchEntryForm";

const ENTRY_ROUTES: Record<
  string,
  { href: string; key: EntryRouteKey } | null
> = {
  inspect_only: { href: "/", key: "dashboardInspect" },
  block_exact_flow: { href: "/", key: "dashboardBlock" },
  restore_rule: { href: "/p4", key: "p4Deployments" },
  manual_p4_entry: { href: "/p4", key: "p4SwitchEntryForm" },
};

// Names for templates that have an end-to-end implementation: backend
// runner produces a plan and the frontend exposes the entry point. The
// `restore_rule` template was promoted in this PR (workflow runner + P4
// switch detail page).
const IMPLEMENTED = new Set([
  "inspect_only",
  "block_exact_flow",
  "restore_rule",
]);

// Templates whose only entry point is a direct admin path (no workflow
// review). `manual_p4_entry` ships through the P4 switch detail
// `EntryForm` because the backend has no WorkflowRunner branch for it.
const ADMIN_DIRECT = new Set(["manual_p4_entry"]);

function statusBadge(name: string, t: ReturnType<typeof useI18n>["t"]) {
  if (ADMIN_DIRECT.has(name)) {
    return (
      <Badge variant="secondary" className="text-xs">
        {t("workflows.templateCapability.statusAdminDirect")}
      </Badge>
    );
  }
  if (IMPLEMENTED.has(name)) {
    return (
      <Badge className="text-xs border-primary/40 bg-primary/10 text-primary hover:bg-primary/15">
        {t("workflows.templateCapability.statusImplemented")}
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className="text-xs border-warning/40 text-warning">
      {t("workflows.templateCapability.statusBackendOnly")}
    </Badge>
  );
}

function entryLink(
  name: string,
  t: ReturnType<typeof useI18n>["t"],
): { href: string; label: string } | null {
  const route = ENTRY_ROUTES[name];
  if (!route) return null;
  const labelKey = `workflows.templateCapability.entry.${route.key}` as TranslationKey;
  return { href: route.href, label: t(labelKey) };
}

/**
 * Folded overview of every backend-enabled semantic template.
 *
 * Data source: `GET /api/templates`. The backend already filters by RBAC
 * (analyze cannot see `manual_p4_entry`), so this component does not
 * filter further. Status (implemented vs admin direct) is hardcoded
 * because it reflects backend runner coverage, not the API response.
 */
export function TemplateCapabilityCard() {
  const { t } = useI18n();
  const templatesQ = useTemplates();
  const [open, setOpen] = useState(false);

  const items: WorkflowTemplateOption[] = templatesQ.data?.items ?? [];
  const total = items.length;
  const implemented = items.filter(
    (option) => IMPLEMENTED.has(option.name) || ADMIN_DIRECT.has(option.name),
  ).length;

  return (
    <Card className="border-border bg-card">
      <button
        type="button"
        aria-label={t("workflows.templateCapability.toggleAriaLabel")}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        className="flex w-full cursor-pointer items-center justify-between gap-3 rounded-lg p-4 text-left transition-colors duration-200 hover:bg-secondary/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <div className="min-w-0">
          <CardTitle className="text-sm font-medium">
            {t("workflows.templateCapability.title")}
          </CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">
            {templatesQ.isLoading
              ? "..."
              : t("workflows.templateCapability.summary", {
                  total,
                  implemented,
                })}
          </p>
        </div>
        <ChevronDown
          className={`h-4 w-4 shrink-0 transition-transform duration-200 motion-reduce:transition-none ${open ? "rotate-180" : ""}`}
          aria-hidden="true"
        />
      </button>
      {open ? (
          <CardContent className="pt-0">
            {templatesQ.isLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-9 w-full" />
              </div>
            ) : items.length === 0 ? (
              <p className="text-xs text-muted-foreground py-2">
                {t("workflows.templateCapability.empty")}
              </p>
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow className="border-border hover:bg-transparent">
                      <TableHead className="text-xs">
                        {t("workflows.templateCapability.columnTemplate")}
                      </TableHead>
                      <TableHead className="text-xs">
                        {t("workflows.templateCapability.columnDescription")}
                      </TableHead>
                      <TableHead className="text-xs">
                        {t("workflows.templateCapability.columnStatus")}
                      </TableHead>
                      <TableHead className="text-xs">
                        {t("workflows.templateCapability.columnEntry")}
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {items.map((option) => {
                      const link = entryLink(option.name, t);
                      return (
                        <TableRow key={option.name} className="border-border">
                          <TableCell className="text-xs">
                            <Badge
                              variant="secondary"
                              className="font-mono text-xs"
                            >
                              {option.name}
                            </Badge>
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground max-w-md">
                            {option.description || "—"}
                          </TableCell>
                          <TableCell className="text-xs">
                            {statusBadge(option.name, t)}
                          </TableCell>
                          <TableCell className="text-xs">
                            {link ? (
                              <Link
                                href={link.href}
                                className="text-primary hover:underline cursor-pointer transition-colors duration-200"
                              >
                                {link.label}
                              </Link>
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
          </CardContent>
      ) : null}
    </Card>
  );
}
