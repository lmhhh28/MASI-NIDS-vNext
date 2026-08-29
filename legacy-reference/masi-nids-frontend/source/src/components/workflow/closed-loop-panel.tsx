"use client";

import { useState } from "react";
import { Activity, Loader2, Eye } from "lucide-react";
import { toast } from "sonner";

import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useObservations, useStartObservation } from "@/hooks/use-closed-loop";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";

interface ClosedLoopPanelProps {
  deploymentId: string | null;
}

export function ClosedLoopPanel({ deploymentId }: ClosedLoopPanelProps) {
  const { t, locale, formatDateTime } = useI18n();
  const [windowSeconds, setWindowSeconds] = useState("60");
  const observationsQ = useObservations(deploymentId);
  const startMut = useStartObservation(deploymentId ?? "");
  const runtimeSafety = useRuntimeSafety("p4");

  if (!deploymentId) return null;

  function handleStart() {
    if (!runtimeSafety.guard()) return;
    const numericSeconds = Number(windowSeconds);
    if (!Number.isFinite(numericSeconds) || numericSeconds < 5 || numericSeconds > 3600) {
      toast.error(t("admin.llm.invalidNumbers"));
      return;
    }
    startMut.mutate(
      { observe_seconds: Math.floor(numericSeconds) },
      {
        onError: (err) =>
          toast.error(t("common.failed"), {
            description: apiErrorMessage(err, locale),
          }),
      }
    );
  }

  const observations = observationsQ.data ?? [];

  return (
    <div className="min-w-0 rounded-xl border border-border bg-card p-4">
      <div className="mb-3 flex min-w-0 flex-wrap items-center gap-2">
        <Activity className="h-4 w-4 text-chart-5" aria-hidden />
        <p className="text-xs font-medium">{t("workflow.closedLoop.title")}</p>
        <Badge variant="outline" className="ml-auto max-w-full text-xs font-mono">
          dep {deploymentId.slice(0, 6)}…
        </Badge>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <Label htmlFor={`closed-loop-window-${deploymentId}`} className="text-xs uppercase tracking-wide text-muted-foreground">
            {t("workflow.closedLoop.windowSeconds")}
          </Label>
          <Input
            id={`closed-loop-window-${deploymentId}`}
            className="h-8 w-24 text-xs"
            type="number"
            min={5}
            max={3600}
            step={5}
            value={windowSeconds}
            onChange={(e) => setWindowSeconds(e.target.value)}
          />
        </div>
        <Button
          size="sm"
          className="h-8 cursor-pointer"
          onClick={handleStart}
          disabled={!runtimeSafety.allowed || startMut.isPending}
        >
          {startMut.isPending ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />
          ) : (
            <Eye className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          )}
          {t("workflow.closedLoop.startWindow", { seconds: windowSeconds })}
        </Button>
      </div>

      <div className="mt-4 space-y-2">
        <p className="text-xs uppercase tracking-wide text-muted-foreground">
          {t("workflow.closedLoop.history")}
        </p>
        {observationsQ.isLoading ? (
          <p className="text-xs text-muted-foreground">…</p>
        ) : observations.length === 0 ? (
          <p className="text-xs text-muted-foreground">{t("workflow.closedLoop.empty")}</p>
        ) : (
          <ul className="space-y-1.5">
            {observations.map((obs) => {
              const inconclusive = obs.notes.includes("baseline_zero");
              return (
                <li
                  key={obs.id}
                  className="min-w-0 rounded-lg border border-border bg-secondary/20 p-2.5"
                >
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <Badge
                      variant={obs.status === "completed" ? "default" : "secondary"}
                      className="text-xs"
                    >
                      {obs.status === "completed"
                        ? t("workflow.closedLoop.completed")
                        : t("workflow.closedLoop.running")}
                    </Badge>
                    {inconclusive && (
                      <Badge variant="outline" className="text-xs">
                        {t("workflow.closedLoop.inconclusive")}
                      </Badge>
                    )}
                    <span className="ml-auto break-all text-xs font-mono text-muted-foreground">
                      {formatDateTime(obs.started_at, {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                  </div>
                  <div className="mt-1.5 grid min-w-0 grid-cols-1 gap-2 text-xs font-mono sm:grid-cols-3">
                    <span className="min-w-0 break-words">
                      {t("workflow.closedLoop.baseline")}: {obs.anomaly_count_before}
                    </span>
                    <span className="min-w-0 break-words">
                      {t("workflow.closedLoop.after")}:{" "}
                      {obs.anomaly_count_after ?? "—"}
                    </span>
                    <span className="min-w-0 break-words text-muted-foreground">
                      {obs.observe_window_seconds}s
                    </span>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
