"use client";

import Link from "next/link";
import { CheckCircle2, CircleDashed, Loader2, MinusCircle, XCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useI18n } from "@/lib/i18n";
import type { OperatorBatch, OperatorBatchItem } from "@/types/api";

const TITLE_KEYS = {
  resolve_evidence: "events.bulk.progress.title.evidence",
  start_inspect_workflow: "events.bulk.progress.title.report",
  start_block_workflow: "events.bulk.progress.title.block",
} as const;

function ItemIcon({ status }: { status: string }) {
  if (status === "claimed") {
    return <Loader2 className="size-4 shrink-0 animate-spin text-primary motion-reduce:animate-none" aria-hidden="true" />;
  }
  if (status === "succeeded") {
    return <CheckCircle2 className="size-4 shrink-0 text-success" aria-hidden="true" />;
  }
  if (status === "failed") {
    return <XCircle className="size-4 shrink-0 text-destructive" aria-hidden="true" />;
  }
  if (status === "cancelled") {
    return <MinusCircle className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />;
  }
  return <CircleDashed className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />;
}

function ResultLink({ item }: { item: OperatorBatchItem }) {
  if (!item.result_id) return null;
  if (item.result_type === "workflow") {
    return (
      <Link className="ml-auto text-xs text-primary underline-offset-4 hover:underline" href={`/workflows/${encodeURIComponent(item.result_id)}`}>
        {item.result_id.slice(0, 8)}…
      </Link>
    );
  }
  return <span className="ml-auto font-mono text-xs text-muted-foreground">{item.result_id.slice(0, 8)}…</span>;
}

export function OperatorBatchProgress({
  batch,
  isRefreshing,
  isStopping,
  stopDisabled,
  onStop,
  onClose,
}: {
  batch: OperatorBatch;
  isRefreshing: boolean;
  isStopping: boolean;
  stopDisabled?: boolean;
  onStop: () => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const completed = batch.succeeded_count + batch.failed_count + batch.cancelled_count;
  const percentage = batch.total_count > 0 ? Math.round((completed / batch.total_count) * 100) : 0;
  const terminal = ["completed", "completed_with_errors", "cancelled"].includes(batch.status);

  return (
    <Card aria-live="polite">
      <CardHeader className="flex flex-row items-start justify-between gap-4 pb-3">
        <div className="space-y-1">
          <CardTitle className="text-base">{t(TITLE_KEYS[batch.kind])}</CardTitle>
          <p className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <Badge variant="outline">{batch.status}</Badge>
            <span>{t("events.bulk.progress.summary", { ok: batch.succeeded_count, fail: batch.failed_count, skip: batch.cancelled_count })}</span>
            {isRefreshing && !terminal ? <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-label={t("common.refresh")} /> : null}
          </p>
        </div>
        {terminal ? (
          <Button size="sm" variant="outline" onClick={onClose}>{t("events.bulk.progress.close")}</Button>
        ) : (
          <Button size="sm" variant="outline" disabled={stopDisabled || isStopping || batch.stop_requested} onClick={onStop}>
            {isStopping ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}
            {batch.stop_requested ? t("events.bulk.progress.stopping") : t("events.bulk.progress.stopAfterCurrent")}
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {batch.stop_requested ? (
          <p className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
            {t("events.bulk.progress.stopSemantics")}
          </p>
        ) : null}
        <div
          className="h-2 overflow-hidden rounded-full bg-secondary"
          role="progressbar"
          aria-label={t(TITLE_KEYS[batch.kind])}
          aria-valuemin={0}
          aria-valuemax={batch.total_count}
          aria-valuenow={completed}
        >
          <div className="h-full bg-primary transition-[width] motion-reduce:transition-none" style={{ width: `${percentage}%` }} />
        </div>
        <ul className="max-h-64 divide-y overflow-y-auto rounded-md border">
          {batch.items.map((item) => (
            <li key={item.id} className="flex min-h-10 items-center gap-2 px-3 py-2 text-sm">
              <ItemIcon status={item.status} />
              <span className="font-mono text-xs">{item.event_id.slice(0, 12)}…</span>
              <Badge variant="secondary" className="text-xs">{item.status}</Badge>
              {item.error_code ? <span className="truncate text-xs text-destructive" title={item.error_detail ?? item.error_code}>{item.error_code}</span> : null}
              <ResultLink item={item} />
            </li>
          ))}
        </ul>
        <p className="break-all font-mono text-xs text-muted-foreground">batch {batch.id} · state {batch.state_version}</p>
      </CardContent>
    </Card>
  );
}
