"use client";

import * as React from "react";
import {
  CheckCircle2,
  CircleDashed,
  Loader2,
  XCircle,
  MinusCircle,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useI18n } from "@/lib/i18n";
import type {
  BulkItemResult,
  BulkItemStatus,
  BulkState,
} from "@/hooks/use-bulk-action";

interface BulkProgressCardProps<I extends { id: string }, T> {
  state: BulkState<I, T>;
  titleKey:
    | "events.bulk.progress.title.evidence"
    | "events.bulk.progress.title.report"
    | "events.bulk.progress.title.block";
  /** Format an event id into a short stable label (default: first 8 chars). */
  formatLabel?: (id: string) => string;
  /** Translate a per-item failure code into a localised message. */
  formatErrorCode?: (code: string | undefined) => string | null;
  /** Translate a skip-reason key into a localised message. */
  formatSkipReason?: (reason: string | undefined) => string | null;
  onAbort: () => void;
  onClose: () => void;
}

const STATUS_ICON: Record<BulkItemStatus, React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>> = {
  pending: CircleDashed,
  running: Loader2,
  success: CheckCircle2,
  failed: XCircle,
  skipped: MinusCircle,
};

const STATUS_COLOR: Record<BulkItemStatus, string> = {
  pending: "text-muted-foreground",
  running: "text-primary",
  success: "text-primary",
  failed: "text-destructive",
  skipped: "text-warning",
};

function ItemRow<T>({
  result,
  label,
  errorMessage,
  skipMessage,
}: {
  result: BulkItemResult<T>;
  label: string;
  errorMessage: string | null;
  skipMessage: string | null;
}) {
  const Icon = STATUS_ICON[result.status];
  return (
    <li className="flex items-center gap-2 px-2 py-1.5 text-xs">
      <Icon
        className={`h-3.5 w-3.5 shrink-0 ${STATUS_COLOR[result.status]} ${
          result.status === "running" ? "animate-spin motion-reduce:animate-none" : ""
        }`}
        aria-hidden
      />
      <span className="font-mono text-xs text-muted-foreground">{label}</span>
      {result.status === "failed" && (errorMessage ?? result.errorMessage) ? (
        <span className="ml-2 truncate text-destructive">
          {errorMessage ?? result.errorMessage}
        </span>
      ) : null}
      {result.status === "skipped" && (skipMessage ?? result.skipReason) ? (
        <span className="ml-2 truncate text-warning">
          {skipMessage ?? result.skipReason}
        </span>
      ) : null}
    </li>
  );
}

const MemoItemRow = React.memo(ItemRow) as typeof ItemRow;

/**
 * Renders the live progress of a `useBulkAction` batch.
 *
 * Pre-Delivery Checklist conformance:
 * - The container is `aria-live="polite"` so SR users hear progress without focus jumps.
 * - Progress bar has `role="progressbar"` with `aria-valuenow/min/max`.
 * - Spinner uses `motion-reduce:animate-none` for `prefers-reduced-motion`.
 * - Per-item list is keyboard-scrollable; items are memoised to keep
 *   re-renders cheap during long batches.
 */
export function BulkProgressCard<I extends { id: string }, T>({
  state,
  titleKey,
  formatLabel = (id) => id.slice(0, 8),
  formatErrorCode,
  formatSkipReason,
  onAbort,
  onClose,
}: BulkProgressCardProps<I, T>) {
  const { t } = useI18n();

  // Ticking "now" so ETA updates while running. We avoid calling
  // `Date.now()` directly during render (React 19 purity rule); the
  // effect below increments it once per second only while running.
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (state.status !== "running") return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [state.status]);

  if (state.status === "idle") return null;

  const percent =
    state.total > 0 ? Math.round((state.completed / state.total) * 100) : 0;
  const elapsedSec =
    state.startedAt != null
      ? Math.max(0, Math.round(((state.finishedAt ?? now) - state.startedAt) / 1000))
      : 0;
  const etaSec =
    state.status === "running" && state.completed > 0
      ? Math.round(
          (elapsedSec / state.completed) * (state.total - state.completed),
        )
      : null;

  const statusLabel =
    state.status === "running"
      ? t("events.bulk.progress.running")
      : state.status === "aborted"
        ? t("events.bulk.progress.aborted")
        : t("events.bulk.progress.done");

  // Render in the original input order based on the dispatch results map.
  const orderedResults: BulkItemResult<T>[] = Object.values(state.results);

  return (
    <Card className="border-border bg-card" aria-live="polite">
      <CardHeader className="flex flex-row items-center justify-between gap-3 pb-3">
        <div className="space-y-1">
          <CardTitle className="text-sm font-medium">{t(titleKey)}</CardTitle>
          <p className="text-xs text-muted-foreground">
            <Badge variant="outline" className="mr-2 text-xs font-mono">
              {statusLabel}
            </Badge>
            {t("events.bulk.progress.summary", {
              ok: state.okCount,
              fail: state.failedCount,
              skip: state.skippedCount,
            })}
            {etaSec != null ? (
              <span className="ml-2">
                · {t("events.bulk.progress.eta", { seconds: etaSec })}
              </span>
            ) : null}
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          {state.status === "running" ? (
            <Button
              size="sm"
              variant="outline"
              className="cursor-pointer text-xs"
              onClick={onAbort}
            >
              {t("events.bulk.progress.abort")}
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              className="cursor-pointer text-xs"
              onClick={onClose}
            >
              {t("events.bulk.progress.close")}
            </Button>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        <div
          role="progressbar"
          aria-valuenow={state.completed}
          aria-valuemin={0}
          aria-valuemax={state.total}
          aria-label={t(titleKey)}
          className="h-2 w-full overflow-hidden rounded-full bg-secondary"
        >
          <div
            className="h-full bg-primary transition-[width] duration-200 motion-reduce:transition-none"
            style={{ width: `${percent}%` }}
          />
        </div>

        <ul className="max-h-56 divide-y divide-border overflow-y-auto rounded-md border border-border bg-muted/20">
          {orderedResults.map((result) => (
            <MemoItemRow
              key={result.id}
              result={result}
              label={formatLabel(result.id)}
              errorMessage={
                result.errorCode && formatErrorCode
                  ? formatErrorCode(result.errorCode)
                  : null
              }
              skipMessage={
                result.skipReason && formatSkipReason
                  ? formatSkipReason(result.skipReason)
                  : null
              }
            />
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
