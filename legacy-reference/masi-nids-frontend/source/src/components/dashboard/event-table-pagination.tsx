"use client";

import * as React from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useI18n } from "@/lib/i18n";

interface EventTablePaginationProps {
  page: number; // 1-based
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
  isLoading?: boolean;
}

/**
 * Page size options for the dashboard event table.
 *
 * Capped at 100 in MVP because we render rows un-virtualised.
 * TODO: introduce TanStack Virtual when we lift this above 100.
 */
const PAGE_SIZE_OPTIONS = [25, 50, 100] as const;

/**
 * Pagination strip rendered below the events table.
 *
 * - Three-region flex layout: showing range · page-size select · prev/next.
 * - Buttons + Select are keyboard-reachable and aria-labelled (Pre-Delivery
 *   Checklist: focus-visible ring, aria-label on form controls).
 * - When `total` is zero we still render the controls so the operator can
 *   still pick a page size before any data lands.
 */
export function EventTablePagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  isLoading = false,
}: EventTablePaginationProps) {
  const { t } = useI18n();

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(Math.max(1, page), totalPages);
  const from = total === 0 ? 0 : (safePage - 1) * pageSize + 1;
  const to = Math.min(total, safePage * pageSize);

  return (
    <div
      className="flex flex-col items-center justify-between gap-3 px-1 py-2 sm:flex-row"
      aria-label={t("events.allEvents.title")}
    >
      <div
        className="text-xs text-muted-foreground tabular-nums"
        aria-live="polite"
      >
        {t("events.showingRange", { from, to })} ·{" "}
        {t("events.totalCount", { n: total })}
      </div>

      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">
          {t("events.pageSize.label")}
        </span>
        <Select
          value={String(pageSize)}
          onValueChange={(value) => {
            const next = Number(value);
            if (!Number.isNaN(next)) onPageSizeChange(next);
          }}
        >
          <SelectTrigger
            size="sm"
            className="h-8 w-[5.5rem] cursor-pointer text-xs"
            aria-label={t("events.pageSize.label")}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PAGE_SIZE_OPTIONS.map((opt) => (
              <SelectItem key={opt} value={String(opt)} className="text-xs">
                {opt}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex items-center gap-1">
        <Button
          size="icon-sm"
          variant="outline"
          className="cursor-pointer"
          aria-label={t("events.previousPage")}
          disabled={isLoading || safePage <= 1}
          onClick={() => onPageChange(safePage - 1)}
        >
          <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
        </Button>
        <span
          className="min-w-[6rem] text-center text-xs text-muted-foreground tabular-nums"
          aria-live="polite"
        >
          {t("events.page", { current: safePage, total: totalPages })}
        </span>
        <Button
          size="icon-sm"
          variant="outline"
          className="cursor-pointer"
          aria-label={t("events.nextPage")}
          disabled={isLoading || safePage >= totalPages}
          onClick={() => onPageChange(safePage + 1)}
        >
          <ChevronRight className="h-3.5 w-3.5" aria-hidden />
        </Button>
      </div>
    </div>
  );
}
