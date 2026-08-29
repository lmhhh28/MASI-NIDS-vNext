"use client";

import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, RefreshCw, RotateCcw, X } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  getDemoOperation,
  resolveDemoOperation,
  retryTrackedDemoOperation,
} from "@/hooks/use-demo-traffic";
import { invalidateEventQueries } from "@/hooks/use-events";
import { useAuthStore } from "@/lib/auth";
import { parseApiError } from "@/lib/api-errors";
import {
  actorDemoOperations,
  isDemoOperationActive,
  isDemoOperationTerminal,
  useDemoOperationRegistry,
  type TrackedDemoOperation,
} from "@/lib/demo-operations";
import { useI18n } from "@/lib/i18n";

const RETRY_DELAYS = [5_000, 10_000, 30_000, 60_000] as const;

function operationKindLabel(kind: TrackedDemoOperation["kind"], t: ReturnType<typeof useI18n>["t"]) {
  if (kind === "run") return t("admin.demo.operationRun");
  if (kind === "stop") return t("admin.demo.operationStop");
  return t("admin.demo.operationCleanup");
}

function statusVariant(status: TrackedDemoOperation["status"]) {
  if (status === "succeeded") return "default" as const;
  if (status === "failed" || status === "cancelled") {
    return "destructive" as const;
  }
  return status === "outcome_unknown" ? "outline" as const : "secondary" as const;
}

function numberResult(operation: TrackedDemoOperation, key: string): number | null {
  const value = operation.result[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function OperationResult({ operation }: { operation: TrackedDemoOperation }) {
  const { t, formatNumber } = useI18n();
  if (operation.kind !== "cleanup" || operation.status !== "succeeded") return null;
  const values = [
    ["admin.demo.deletedEvents", numberResult(operation, "deleted_events")],
    ["admin.demo.deletedAlerts", numberResult(operation, "deleted_alerts")],
    ["admin.demo.deletedWorkflows", numberResult(operation, "deleted_workflows")],
    ["admin.demo.deletedSourceRuns", numberResult(operation, "deleted_source_runs")],
    ["admin.demo.globalRemainingEvents", numberResult(operation, "remaining_global_events")],
  ] as const;
  return (
    <dl className="grid gap-2 rounded-md border bg-muted/20 p-3 text-xs sm:grid-cols-2 lg:grid-cols-5">
      {values.map(([label, value]) => (
        <div key={label}>
          <dt className="text-muted-foreground">{t(label)}</dt>
          <dd className="mt-1 font-mono text-sm font-semibold tabular-nums">
            {value === null ? "—" : formatNumber(value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function invalidateTerminalQueries(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: ["demo-traffic-status"] });
  invalidateEventQueries(queryClient);
  queryClient.invalidateQueries({ queryKey: ["event-stats"] });
  queryClient.invalidateQueries({ queryKey: ["alerts"] });
  queryClient.invalidateQueries({ queryKey: ["workflows"] });
  queryClient.invalidateQueries({ queryKey: ["event-sources"] });
  queryClient.invalidateQueries({ queryKey: ["operations-summary"] });
}

function OperationRow({ operation }: { operation: TrackedDemoOperation }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const applyResponse = useDemoOperationRegistry((state) => state.applyResponse);
  const markOperationMissing = useDemoOperationRegistry((state) => state.markOperationMissing);
  const requestResolution = useDemoOperationRegistry((state) => state.requestResolution);
  const markTerminalHandled = useDemoOperationRegistry((state) => state.markTerminalHandled);
  const remove = useDemoOperationRegistry((state) => state.remove);
  const [retrying, setRetrying] = useState(false);
  const canPoll = Boolean(operation.operationId || operation.needsResolve) &&
    (!isDemoOperationTerminal(operation) || operation.needsResolve);
  const operationQ = useQuery({
    queryKey: [
      "demo-traffic-operation",
      operation.actorUserId,
      operation.operationId ?? operation.key,
      operation.needsResolve ? "resolve" : "get",
    ],
    queryFn: ({ signal }) => operation.operationId && !operation.needsResolve
      ? getDemoOperation(operation.operationId, signal)
      : resolveDemoOperation(operation.key, signal),
    enabled: canPoll,
    retry: false,
    refetchInterval: (query) => {
      const response = query.state.data;
      if (response && ["succeeded", "failed", "cancelled", "outcome_unknown"].includes(response.status)) {
        return false;
      }
      const failures = query.state.fetchFailureCount;
      return failures > 0
        ? RETRY_DELAYS[Math.min(failures - 1, RETRY_DELAYS.length - 1)]
        : 2_000;
    },
    refetchIntervalInBackground: false,
  });

  useEffect(() => {
    if (operationQ.data) {
      applyResponse(operation.actorUserId, operation.key, operationQ.data);
    }
  }, [applyResponse, operation.actorUserId, operation.key, operationQ.data]);

  useEffect(() => {
    if (!operationQ.error) return;
    const info = parseApiError(operationQ.error);
    if (info.status !== 404) return;
    if (operation.operationId && !operation.needsResolve) {
      requestResolution(operation.actorUserId, operation.key, info.code);
    } else {
      markOperationMissing(operation.actorUserId, operation.key, info.code);
    }
  }, [
    markOperationMissing,
    operation.actorUserId,
    operation.key,
    operation.needsResolve,
    operation.operationId,
    operationQ.error,
    requestResolution,
  ]);

  useEffect(() => {
    if (!isDemoOperationTerminal(operation) || operation.terminalHandled) return;
    invalidateTerminalQueries(queryClient);
    const description = [operation.operationId, operation.errorCode].filter(Boolean).join(" · ");
    if (operation.status === "succeeded") {
      const title = operation.kind === "run"
        ? t("admin.demo.runCompleted")
        : operation.kind === "stop"
          ? t("admin.demo.stopCompleted")
          : t("admin.demo.cleaned");
      toast.success(title, { description: description || undefined });
    } else if (operation.status === "outcome_unknown") {
      toast.warning(t("admin.demo.operationOutcomeUnknown"), {
        description: description || t("admin.demo.operationUnknownHint"),
      });
    } else {
      toast.error(t("admin.demo.operationFailed"), { description: description || undefined });
    }
    markTerminalHandled(operation.actorUserId, operation.key);
  }, [markTerminalHandled, operation, queryClient, t]);

  async function retryWithSameKey() {
    setRetrying(true);
    try {
      const admission = await retryTrackedDemoOperation(operation);
      toast.info(t("admin.demo.operationSubmitted"), {
        description: `${admission.operation.operation_id} · ${admission.operation.status}`,
      });
    } catch {
      // The registry owns error/unknown state so the terminal observer emits
      // exactly one notification and the row supplies the recovery action.
    } finally {
      setRetrying(false);
    }
  }

  const connectionError = operationQ.error && parseApiError(operationQ.error).status !== 404;
  return (
    <article className="space-y-3 rounded-lg border p-4" aria-live="polite">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{operationKindLabel(operation.kind, t)}</span>
            <Badge
              variant={statusVariant(operation.status)}
              className={operation.status === "outcome_unknown" ? "border-warning/50 text-warning" : undefined}
            >
              {operation.status}
            </Badge>
            {operation.needsResolve ? (
              <Badge variant="outline">{t("admin.demo.operationResolving")}</Badge>
            ) : null}
          </div>
          <p className="break-all font-mono text-xs text-muted-foreground">
            {operation.operationId ?? `${t("admin.demo.idempotencyKey")}: ${operation.key}`}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {canPoll ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="min-h-11 sm:min-h-9"
              disabled={operationQ.isFetching}
              onClick={() => operationQ.refetch()}
            >
              <RefreshCw
                className={operationQ.isFetching ? "animate-spin motion-reduce:animate-none" : ""}
                aria-hidden="true"
              />
              {t("common.refresh")}
            </Button>
          ) : null}
          {operation.manualRetryRequired ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="min-h-11 sm:min-h-9"
              disabled={retrying}
              onClick={() => void retryWithSameKey()}
            >
              {retrying ? (
                <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              ) : (
                <RotateCcw aria-hidden="true" />
              )}
              {t("admin.demo.retrySameKey")}
            </Button>
          ) : null}
          {isDemoOperationTerminal(operation) ? (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="min-h-11 min-w-11 sm:min-h-9 sm:min-w-9"
              aria-label={t("admin.demo.dismissOperation")}
              onClick={() => remove(operation.actorUserId, operation.key)}
            >
              <X aria-hidden="true" />
            </Button>
          ) : null}
        </div>
      </div>
      <dl className="grid gap-2 text-xs sm:grid-cols-3">
        <div>
          <dt className="text-muted-foreground">{t("admin.demo.onlineRun")}</dt>
          <dd className="mt-1 break-all font-mono">{operation.onlineRunId ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t("admin.demo.operationPhase")}</dt>
          <dd className="mt-1 font-mono">{operation.phase ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t("admin.demo.operationAttempts")}</dt>
          <dd className="mt-1 font-mono tabular-nums">{operation.attemptCount}</dd>
        </div>
      </dl>
      {connectionError ? (
        <p className="flex items-center gap-2 text-xs text-warning" role="status">
          <AlertTriangle className="size-4 shrink-0" aria-hidden="true" />
          {t("admin.demo.operationPollRetry")}
        </p>
      ) : null}
      {operation.errorCode ? (
        <p className="break-all rounded-md border border-destructive/30 bg-destructive/5 p-2 font-mono text-xs text-destructive">
          {operation.errorCode}
        </p>
      ) : null}
      {operation.status === "outcome_unknown" && !operation.manualRetryRequired ? (
        <p className="text-xs text-muted-foreground">{t("admin.demo.operationUnknownHint")}</p>
      ) : null}
      <OperationResult operation={operation} />
      {Object.keys(operation.result).length > 0 ? (
        <details className="rounded-md border p-3 text-xs">
          <summary className="cursor-pointer font-medium">{t("admin.demo.operationRawResult")}</summary>
          <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all">
            {JSON.stringify(operation.result, null, 2)}
          </pre>
        </details>
      ) : null}
    </article>
  );
}

export function DemoOperationActivity() {
  const { t } = useI18n();
  const actorUserId = useAuthStore((state) => state.user?.id ?? null);
  const operations = useDemoOperationRegistry((state) => state.operations);
  const prune = useDemoOperationRegistry((state) => state.prune);
  const actorOperations = useMemo(
    () => actorDemoOperations(operations, actorUserId),
    [actorUserId, operations],
  );
  const visibleOperations = useMemo(() => {
    const active = actorOperations.filter(isDemoOperationActive);
    const terminal = actorOperations.filter(isDemoOperationTerminal).slice(0, 3);
    return [...active, ...terminal].sort((left, right) => right.createdAt.localeCompare(left.createdAt));
  }, [actorOperations]);

  useEffect(() => prune(), [prune]);
  if (visibleOperations.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("admin.demo.operationActivity")}</CardTitle>
        <CardDescription>{t("admin.demo.operationActivityDescription")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {visibleOperations.map((operation) => (
          <OperationRow key={operation.key} operation={operation} />
        ))}
      </CardContent>
    </Card>
  );
}
