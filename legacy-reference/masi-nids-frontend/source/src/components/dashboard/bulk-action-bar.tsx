"use client";

import * as React from "react";
import { CheckSquare, FileSearch, GitBranchPlus, ShieldAlert, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n";
import { BULK_HARD_LIMIT } from "@/hooks/use-bulk-action";

interface BulkActionBarProps {
  count: number;
  isRunning: boolean;
  disabled?: boolean;
  evidenceDisabled?: boolean;
  reportDisabled?: boolean;
  blockDisabled?: boolean;
  onClearSelection: () => void;
  onStartEvidence: () => void;
  onStartReport: () => void;
  onStartBlock: () => void;
}

/**
 * Sticky toolbar surfaced above the events table when at least one row
 * is selected. Provides the three batch entry points; runs are launched
 * via parent-supplied callbacks and orchestrated by `useBulkAction`.
 *
 * Pre-Delivery Checklist conformance:
 * - aria-live="polite" on the selection count so screen-reader users
 *   hear updates without focus jumps.
 * - All buttons have visible text labels (no icon-only); every clickable
 *   carries `cursor-pointer` and a focus-visible ring (inherited from
 *   shared Button styles).
 * - Disabled state during a running batch prevents double submits;
 *   over-limit selections show a tooltip-like inline note rather than
 *   silently disabling.
 */
export function BulkActionBar({
  count,
  isRunning,
  disabled: externallyDisabled = false,
  evidenceDisabled = false,
  reportDisabled = false,
  blockDisabled = false,
  onClearSelection,
  onStartEvidence,
  onStartReport,
  onStartBlock,
}: BulkActionBarProps) {
  const { t } = useI18n();
  if (count === 0) return null;

  const overLimit = count > BULK_HARD_LIMIT;
  const disabled = isRunning || overLimit || externallyDisabled;

  return (
    <div
      className="sticky top-2 z-30 flex flex-col gap-2 rounded-lg border border-border bg-card/95 px-3 py-2 shadow-md backdrop-blur supports-[backdrop-filter]:bg-card/80 sm:flex-row sm:items-center sm:justify-between"
      role="region"
      aria-label={t("events.bulk.start.evidence")}
    >
      <div className="flex items-center gap-2">
        <CheckSquare
          className="h-4 w-4 text-primary"
          aria-hidden
        />
        <span
          className="text-sm font-medium tabular-nums"
          aria-live="polite"
        >
          {t("events.bulk.selected", { n: count })}
        </span>
        <Button
          size="xs"
          variant="ghost"
          className="cursor-pointer"
          onClick={onClearSelection}
          disabled={isRunning}
        >
          <X className="mr-1 h-3 w-3" aria-hidden />
          {t("events.bulk.clearSelection")}
        </Button>
        {overLimit ? (
          <span className="text-xs text-destructive">
            {t("events.bulk.limit.exceeded", { max: BULK_HARD_LIMIT })}
          </span>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <Button
          size="sm"
          variant="outline"
          className="cursor-pointer text-xs"
          disabled={disabled || evidenceDisabled}
          onClick={onStartEvidence}
        >
          <FileSearch className="mr-1 h-3 w-3" aria-hidden />
          {t("events.bulk.start.evidence")}
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="cursor-pointer text-xs"
          disabled={disabled || reportDisabled}
          onClick={onStartReport}
        >
          <GitBranchPlus className="mr-1 h-3 w-3" aria-hidden />
          {t("events.bulk.start.report")}
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="cursor-pointer border-warning/40 text-xs text-warning hover:text-warning"
          disabled={disabled || blockDisabled}
          onClick={onStartBlock}
        >
          <ShieldAlert className="mr-1 h-3 w-3" aria-hidden />
          {t("events.bulk.start.block")}
        </Button>
      </div>
    </div>
  );
}
