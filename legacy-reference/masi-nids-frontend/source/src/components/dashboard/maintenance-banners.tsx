"use client";

import { AlertTriangle, RefreshCw, WifiOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { useI18n } from "@/lib/i18n";

export function MaintenanceBanners() {
  const safety = useRuntimeSafety("admin");
  const runtime = safety.runtime;
  const { t } = useI18n();
  const workflow = runtime.data?.workflow_maintenance === true;
  const p4 = runtime.data?.p4_maintenance === true;

  if (safety.allowed && !workflow && !p4) return null;

  return (
    <div className="space-y-2 px-4 pt-2 md:px-6" role="status" aria-live="polite">
      {!safety.allowed && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-warning/50 bg-warning/10 px-3 py-2 text-sm" role="alert">
          <WifiOff className="h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
          <p id="runtime-safety-status" className="min-w-0 flex-1 font-medium">{t("operations.runtimeUnknown")}</p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            aria-busy={runtime.isFetching}
            onClick={() => { if (!runtime.isFetching) void safety.retry(); }}
          >
            <RefreshCw className={runtime.isFetching ? "animate-spin motion-reduce:animate-none" : ""} aria-hidden="true" />
            {t("common.refresh")}
          </Button>
        </div>
      )}
      {workflow && (
        <div className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
          <div>
            <p className="font-medium">{t("maintenance.workflow.title")}</p>
            <p className="text-xs text-muted-foreground">{t("maintenance.workflow.description")}</p>
          </div>
        </div>
      )}
      {p4 && (
        <div className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
          <div>
            <p className="font-medium">{t("maintenance.p4.title")}</p>
            <p className="text-xs text-muted-foreground">{t("maintenance.p4.description")}</p>
          </div>
        </div>
      )}
    </div>
  );
}
