"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Loader2, RefreshCw, XCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { resolveP4Operation } from "@/hooks/use-p4";
import { useI18n } from "@/lib/i18n";
import { useOperationRegistry, type OperationRecord } from "@/lib/operations";

const SUCCESS = new Set(["applied"]);
const FAILED = new Set(["p4_write_failed", "rejected"]);

function RecoveryItem({ operation }: { operation: OperationRecord }) {
  const { t } = useI18n();
  const transition = useOperationRegistry((state) => state.transition);
  const remove = useOperationRegistry((state) => state.remove);
  const query = useQuery({
    queryKey: ["p4-operation-resolve", operation.key],
    queryFn: ({ signal }) => resolveP4Operation(operation.key, signal),
    refetchInterval: (state) => {
      const status = state.state.data?.status;
      if (status && (SUCCESS.has(status) || FAILED.has(status))) return false;
      if (state.state.fetchFailureCount > 0) {
        return Math.min(60_000, 5_000 * 2 ** (state.state.fetchFailureCount - 1));
      }
      if (state.state.dataUpdateCount <= 1) return 5_000;
      if (state.state.dataUpdateCount === 2) return 10_000;
      return 30_000;
    },
    refetchIntervalInBackground: false,
    retry: false,
  });
  const resolvedStatus = query.data?.status;

  useEffect(() => {
    if (!resolvedStatus) return;
    const patch = {
      requestId: query.data?.request_id,
      deploymentId: query.data?.deployment?.id,
      operationStatus: resolvedStatus,
    };
    if (SUCCESS.has(resolvedStatus)) transition(operation.key, "succeeded", patch);
    else if (resolvedStatus === "rejected") transition(operation.key, "rejected", patch);
    else if (FAILED.has(resolvedStatus)) transition(operation.key, "failed", patch);
    else transition(operation.key, "outcome_unknown", patch);
  }, [operation.key, query.data, resolvedStatus, transition]);

  const terminal = Boolean(resolvedStatus && (SUCCESS.has(resolvedStatus) || FAILED.has(resolvedStatus)));
  const status = resolvedStatus ?? operation.operationStatus ?? operation.status;
  const Icon = terminal
    ? SUCCESS.has(resolvedStatus ?? "") ? CheckCircle2 : XCircle
    : query.isFetching ? Loader2 : AlertTriangle;

  return (
    <Card className={terminal && SUCCESS.has(resolvedStatus ?? "") ? "border-success/40" : "border-warning/50"}>
      <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <Icon className={`mt-0.5 size-5 shrink-0 ${query.isFetching ? "animate-spin motion-reduce:animate-none" : ""}`} aria-hidden="true" />
          <div className="min-w-0">
            <p className="font-medium">{terminal ? t("p4.operationResolved") : t("p4.operationOutcomeUnknown")}</p>
            <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{operation.target} · {status}</p>
            <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{operation.key}</p>
            {query.isError ? <p className="mt-2 text-sm text-warning" role="status">{t("p4.resolveNotYetAvailable")}</p> : null}
          </div>
        </div>
        <div className="flex shrink-0 gap-2">
          {!terminal ? (
            <Button size="sm" variant="outline" aria-busy={query.isFetching} onClick={() => { if (!query.isFetching) void query.refetch(); }}>
              <RefreshCw aria-hidden="true" />{t("p4.resolveOriginalKey")}
            </Button>
          ) : (
            <Button size="sm" variant="outline" onClick={() => remove(operation.key)}>{t("common.close")}</Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function P4OperationRecovery({ switchId }: { switchId: string }) {
  const operations = useOperationRegistry((state) => state.operations);
  const unresolved = Object.values(operations).filter(
    (operation) => operation.kind.startsWith("p4.")
      && operation.target.startsWith(`${switchId}:`)
      && ["prepared", "submitting", "outcome_unknown", "succeeded", "rejected", "failed"].includes(operation.status),
  );
  if (unresolved.length === 0) return null;
  return <section className="space-y-2" aria-label="P4 operation recovery">{unresolved.map((operation) => <RecoveryItem key={operation.key} operation={operation} />)}</section>;
}
