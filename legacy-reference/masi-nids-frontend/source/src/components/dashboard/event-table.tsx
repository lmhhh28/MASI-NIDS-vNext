"use client";

import * as React from "react";
import Link from "next/link";
import {
  Download,
  FileCheck2,
  FileSearch,
  GitBranchPlus,
  Loader2,
  ShieldAlert,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useI18n, type I18nContextValue } from "@/lib/i18n";
import { isKnownAttackAnomaly } from "@/lib/nids-events";
import type { NidsEvent } from "@/types/api";

interface EventTableProps {
  events: NidsEvent[] | undefined;
  isLoading: boolean;
  isFetching?: boolean;
  isPlaceholderData?: boolean;
  selectedIds: Set<string>;
  onToggleOne: (id: string) => void;
  onToggleAllOnPage: (idsOnPage: string[]) => void;
  onIngest?: () => void;
  isIngesting?: boolean;
  canIngest?: boolean;
  ingestDisabledReason?: string;
  onResolveEvidence?: (event: NidsEvent) => void;
  onStartInspectWorkflow?: (event: NidsEvent) => void;
  onStartBlockWorkflow?: (event: NidsEvent) => void;
  workflowMutationsDisabled?: boolean;
  pendingEventAction?: string | null;
  totalCount?: number;
  showingFrom?: number;
  showingTo?: number;
}

interface EventItemProps {
  event: NidsEvent;
  isSelected: boolean;
  selectionDisabled: boolean;
  eventActionPending: boolean;
  pendingEventAction?: string | null;
  onToggleOne: (id: string) => void;
  onResolveEvidence?: (event: NidsEvent) => void;
  onStartInspectWorkflow?: (event: NidsEvent) => void;
  onStartBlockWorkflow?: (event: NidsEvent) => void;
  workflowMutationsDisabled: boolean;
  t: I18nContextValue["t"];
  formatTime: I18nContextValue["formatTime"];
}

interface SelectionCheckboxProps {
  ariaLabel: string;
  checked: boolean;
  disabled?: boolean;
  indeterminate?: boolean;
  onChange: () => void;
}

function SelectionCheckbox({
  ariaLabel,
  checked,
  disabled,
  indeterminate = false,
  onChange,
}: SelectionCheckboxProps) {
  const inputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    if (inputRef.current) {
      inputRef.current.indeterminate = indeterminate;
    }
  }, [indeterminate]);

  return (
    <label className="inline-flex min-h-9 min-w-9 cursor-pointer items-center justify-center rounded-md focus-within:ring-2 focus-within:ring-ring/60 has-[:disabled]:cursor-not-allowed [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11">
      <input
        ref={inputRef}
        type="checkbox"
        aria-label={ariaLabel}
        checked={checked}
        disabled={disabled}
        onChange={onChange}
        className="size-4 cursor-pointer accent-primary disabled:cursor-not-allowed disabled:opacity-50"
      />
    </label>
  );
}

function flowLabel(event: NidsEvent) {
  return event.flow_id ?? `${event.src_ip ?? "?"}->${event.dst_ip ?? "?"}`;
}

function eventTime(
  event: NidsEvent,
  formatTime: I18nContextValue["formatTime"],
) {
  return event.ingested_at
    ? formatTime(event.ingested_at, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "-";
}

function decisionBadge(decision: string | null, t: I18nContextValue["t"]) {
  switch (decision) {
    case "anomaly":
      return (
        <Badge variant="destructive" className="text-xs font-mono">
          {t("events.decisionAnomaly")}
        </Badge>
      );
    case "normal":
      return (
        <Badge className="border-primary/20 bg-primary/15 text-xs font-mono text-primary hover:bg-primary/20">
          {t("events.decisionNormal")}
        </Badge>
      );
    case "unknown":
      return (
        <Badge className="border-warning/30 bg-warning/10 text-xs font-mono text-warning hover:bg-warning/15">
          {t("events.decisionUnknown")}
        </Badge>
      );
    default:
      return (
        <Badge variant="outline" className="text-xs font-mono">
          {decision ?? "-"}
        </Badge>
      );
  }
}

function evidenceBadge(
  status: string | null | undefined,
  t: I18nContextValue["t"],
) {
  if (status === "resolved" || status === "reviewed") {
    return (
      <Badge className="border-primary/20 bg-primary/15 text-xs font-mono text-primary">
        <FileCheck2 className="mr-1 h-2.5 w-2.5" aria-hidden />
        {status === "reviewed"
          ? t("events.evidenceStatus.reviewed")
          : t("events.evidenceStatus.resolved")}
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className="text-xs font-mono text-muted-foreground">
      {t("events.evidenceStatus.pending")}
    </Badge>
  );
}

const EventActions = React.memo(function EventActions({
  event,
  eventActionPending,
  pendingEventAction,
  onResolveEvidence,
  onStartInspectWorkflow,
  onStartBlockWorkflow,
  workflowMutationsDisabled,
  t,
  layout,
}: Omit<EventItemProps, "isSelected" | "selectionDisabled" | "onToggleOne" | "formatTime"> & {
  layout: "table" | "card";
}) {
  const isKnownAttack = isKnownAttackAnomaly(event);
  const hasResolvedEvidence =
    event.directional_evidence_status === "resolved" ||
    event.directional_evidence_status === "reviewed";

  const buttonClass =
    layout === "card"
      ? "h-auto min-h-10 w-full min-w-0 shrink cursor-pointer justify-start whitespace-normal px-2 py-2 text-left text-xs leading-tight"
      : "h-8 max-w-full min-w-0 shrink cursor-pointer whitespace-normal px-2 text-xs leading-tight";
  const splitButtonClass =
    layout === "card"
      ? `${buttonClass} flex-col items-start gap-0`
      : `${buttonClass} flex-col items-start gap-0 py-1`;
  const actionContainerClass =
    layout === "card"
      ? "grid grid-cols-1 gap-2 sm:grid-cols-2"
      : "flex min-w-0 flex-wrap justify-end gap-1.5";
  const inspectButton = (
    <Button
      size="sm"
      variant="outline"
      className={splitButtonClass}
      disabled={workflowMutationsDisabled || eventActionPending}
      onClick={() => onStartInspectWorkflow?.(event)}
    >
      <span className="flex min-w-0 items-center gap-1">
        {pendingEventAction === `inspect:${event.id}` ? (
          <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" aria-hidden />
        ) : (
          <GitBranchPlus className="h-3 w-3" aria-hidden />
        )}
        <span className="min-w-0 break-words">{t("events.generateReport")}</span>
      </span>
      <span className="font-mono text-xs text-muted-foreground/80">
        {t("events.actionInspectSubtitle")}
      </span>
    </Button>
  );
  const reviewLink = (
    <Link
      href="/workflows"
      className={
        layout === "card"
          ? "inline-flex min-h-10 w-full min-w-0 items-center justify-center rounded-md border border-border px-2 py-2 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          : "inline-flex h-8 max-w-full min-w-0 shrink items-center rounded-md px-2 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
      }
    >
      {t("events.viewReview")}
    </Link>
  );

  if (!isKnownAttack) {
    if (event.decision !== "unknown" && event.decision !== "anomaly") {
      return (
        <span className={layout === "table" ? "block text-right text-xs text-muted-foreground" : "text-xs text-muted-foreground"}>
          -
        </span>
      );
    }
    return (
      <div className={actionContainerClass}>
        {inspectButton}
        {reviewLink}
      </div>
    );
  }

  return (
    <div className={actionContainerClass}>
      <Button
        size="sm"
        variant="outline"
        className={buttonClass}
        disabled={eventActionPending || hasResolvedEvidence}
        onClick={() => onResolveEvidence?.(event)}
      >
        {pendingEventAction === `evidence:${event.id}` ? (
          <Loader2 className="mr-1 h-3 w-3 animate-spin motion-reduce:animate-none" aria-hidden />
        ) : hasResolvedEvidence ? (
          <FileCheck2 className="mr-1 h-3 w-3" aria-hidden />
        ) : (
          <FileSearch className="mr-1 h-3 w-3" aria-hidden />
        )}
        {hasResolvedEvidence
          ? t("events.evidenceAlreadyResolved")
          : t("events.resolveEvidence")}
      </Button>
      {inspectButton}
      <Button
        size="sm"
        variant="outline"
        className={`${splitButtonClass} border-warning/40 text-warning hover:text-warning`}
        disabled={workflowMutationsDisabled || eventActionPending}
        onClick={() => onStartBlockWorkflow?.(event)}
      >
        <span className="flex min-w-0 items-center gap-1">
          {pendingEventAction === `block:${event.id}` ? (
            <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" aria-hidden />
          ) : (
            <ShieldAlert className="h-3 w-3" aria-hidden />
          )}
          <span className="min-w-0 break-words">{t("events.createBlockWorkflow")}</span>
        </span>
        <span className="font-mono text-xs text-muted-foreground/80">
          {t("events.actionBlockSubtitle")}
        </span>
      </Button>
      {reviewLink}
    </div>
  );
});

const EventTableRow = React.memo(function EventTableRow(props: EventItemProps) {
  const {
    event,
    isSelected,
    selectionDisabled,
    eventActionPending,
    pendingEventAction,
    onToggleOne,
    onResolveEvidence,
    onStartInspectWorkflow,
    onStartBlockWorkflow,
    workflowMutationsDisabled,
    t,
    formatTime,
  } = props;
  const label = flowLabel(event);

  return (
    <TableRow
      data-state={isSelected ? "selected" : undefined}
      className="border-border transition-colors hover:bg-secondary/30"
    >
      <TableCell className="w-[4%]">
        <SelectionCheckbox
          ariaLabel={t("events.row.checkboxLabel", { flow: label })}
          checked={isSelected}
          disabled={selectionDisabled}
          onChange={() => onToggleOne(event.id)}
        />
      </TableCell>
      <TableCell className="w-[12%] whitespace-nowrap font-mono text-xs text-muted-foreground">
        {eventTime(event, formatTime)}
      </TableCell>
      <TableCell className="min-w-0 overflow-hidden font-mono text-xs">
        <div className="truncate" title={label}>
          {label}
        </div>
      </TableCell>
      <TableCell className="w-[10%] min-w-0 overflow-hidden font-mono text-xs">
        <div className="truncate" title={event.nearest_class ?? undefined}>
          {event.nearest_class ?? "-"}
        </div>
      </TableCell>
      <TableCell className="w-[8%] text-right font-mono text-xs tabular-nums">
        {event.legacy_score != null ? event.legacy_score.toFixed(4) : "-"}
      </TableCell>
      <TableCell className="w-[8%] text-center">
        {decisionBadge(event.decision, t)}
      </TableCell>
      <TableCell className="w-[9%] text-center">
        {evidenceBadge(event.directional_evidence_status, t)}
      </TableCell>
      <TableCell className="w-[31%] align-top">
        <EventActions
          event={event}
          eventActionPending={eventActionPending}
          pendingEventAction={pendingEventAction}
          onResolveEvidence={onResolveEvidence}
          onStartInspectWorkflow={onStartInspectWorkflow}
          onStartBlockWorkflow={onStartBlockWorkflow}
          workflowMutationsDisabled={workflowMutationsDisabled}
          t={t}
          layout="table"
        />
      </TableCell>
    </TableRow>
  );
});

const EventCard = React.memo(function EventCard(props: EventItemProps) {
  const {
    event,
    isSelected,
    selectionDisabled,
    eventActionPending,
    pendingEventAction,
    onToggleOne,
    onResolveEvidence,
    onStartInspectWorkflow,
    onStartBlockWorkflow,
    workflowMutationsDisabled,
    t,
    formatTime,
  } = props;
  const label = flowLabel(event);

  return (
    <article
      data-state={isSelected ? "selected" : undefined}
      className="rounded-md border border-border bg-background/40 p-3 transition-colors data-[state=selected]:border-primary/50 data-[state=selected]:bg-primary/5"
    >
      <div className="flex items-start gap-3">
        <SelectionCheckbox
          ariaLabel={t("events.row.checkboxLabel", { flow: label })}
          checked={isSelected}
          disabled={selectionDisabled}
          onChange={() => onToggleOne(event.id)}
        />
        <div className="min-w-0 flex-1 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs text-muted-foreground">
              {eventTime(event, formatTime)}
            </span>
            {decisionBadge(event.decision, t)}
            {evidenceBadge(event.directional_evidence_status, t)}
          </div>
          <div className="space-y-1">
            <p className="break-all font-mono text-xs text-foreground">
              {label}
            </p>
            <div className="grid grid-cols-2 gap-2 text-xs">
              <div className="min-w-0">
                <p className="text-muted-foreground">{t("events.class")}</p>
                <p className="truncate font-mono" title={event.nearest_class ?? undefined}>
                  {event.nearest_class ?? "-"}
                </p>
              </div>
              <div className="min-w-0 text-right">
                <p className="text-muted-foreground">{t("events.score")}</p>
                <p className="font-mono tabular-nums">
                  {event.legacy_score != null ? event.legacy_score.toFixed(4) : "-"}
                </p>
              </div>
            </div>
          </div>
          <EventActions
            event={event}
            eventActionPending={eventActionPending}
            pendingEventAction={pendingEventAction}
            onResolveEvidence={onResolveEvidence}
            onStartInspectWorkflow={onStartInspectWorkflow}
            onStartBlockWorkflow={onStartBlockWorkflow}
            workflowMutationsDisabled={workflowMutationsDisabled}
            t={t}
            layout="card"
          />
        </div>
      </div>
    </article>
  );
});

export function EventTable({
  events,
  isLoading,
  isFetching = false,
  isPlaceholderData = false,
  selectedIds,
  onToggleOne,
  onToggleAllOnPage,
  onIngest,
  isIngesting = false,
  canIngest = true,
  ingestDisabledReason,
  onResolveEvidence,
  onStartInspectWorkflow,
  onStartBlockWorkflow,
  workflowMutationsDisabled = false,
  pendingEventAction,
  totalCount,
  showingFrom,
  showingTo,
}: EventTableProps) {
  const { t, formatTime } = useI18n();
  const eventActionPending = pendingEventAction != null;
  const ingestDisabled = isIngesting || !canIngest;
  const selectionDisabled = isPlaceholderData;

  const idsOnPage = React.useMemo(
    () => (events ?? []).map((event) => event.id),
    [events],
  );
  const selectedOnPage = React.useMemo(
    () => idsOnPage.filter((id) => selectedIds.has(id)).length,
    [idsOnPage, selectedIds],
  );
  const allOnPageSelected =
    idsOnPage.length > 0 && selectedOnPage === idsOnPage.length;
  const someOnPageSelected = selectedOnPage > 0 && !allOnPageSelected;

  const showingLabel =
    typeof showingFrom === "number" &&
    typeof showingTo === "number" &&
    typeof totalCount === "number"
      ? `${t("events.showingRange", { from: showingFrom, to: showingTo })} · ${t("events.totalCount", { n: totalCount })}`
      : undefined;

  return (
    <Card className="border-border bg-card">
      <CardHeader className="flex flex-col gap-3 space-y-0 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-1">
          <CardTitle className="text-sm font-medium text-foreground">
            {t("events.allEvents.title")}
          </CardTitle>
          <p className="text-xs text-muted-foreground">
            {t("events.allEvents.subtitle")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {showingLabel ? (
            <span
              className="text-xs text-muted-foreground tabular-nums"
              aria-live="polite"
            >
              {showingLabel}
            </span>
          ) : null}
          {onIngest ? (
            <Button
              size="sm"
              variant="outline"
              onClick={onIngest}
              disabled={ingestDisabled}
              className="h-8 cursor-pointer text-xs"
            >
              {isIngesting ? (
                <Loader2 className="mr-1.5 h-3 w-3 animate-spin motion-reduce:animate-none" aria-hidden />
              ) : (
                <Download className="mr-1.5 h-3 w-3" aria-hidden />
              )}
              {t("events.ingestNow")}
            </Button>
          ) : null}
        </div>
      </CardHeader>

      <CardContent className="min-w-0">
        {!canIngest && ingestDisabledReason ? (
          <div className="mb-3 rounded-lg border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning">
            {ingestDisabledReason}
          </div>
        ) : null}

        {isLoading ? (
          <div className="space-y-2" aria-busy="true">
            {[1, 2, 3, 4, 5].map((i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : !events || events.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-10 text-muted-foreground">
            <p className="text-sm">{t("events.empty")}</p>
            <p className="mt-1 text-xs">{t("events.emptyHint")}</p>
          </div>
        ) : (
          <>
            <div
              className="space-y-3 lg:hidden"
              data-stale={isFetching ? "true" : undefined}
            >
              {events.map((event) => (
                <EventCard
                  key={event.id}
                  event={event}
                  isSelected={selectedIds.has(event.id)}
                  selectionDisabled={selectionDisabled}
                  eventActionPending={eventActionPending}
                  pendingEventAction={pendingEventAction}
                  onToggleOne={onToggleOne}
                  onResolveEvidence={onResolveEvidence}
                  onStartInspectWorkflow={onStartInspectWorkflow}
                  onStartBlockWorkflow={onStartBlockWorkflow}
                  workflowMutationsDisabled={workflowMutationsDisabled}
                  t={t}
                  formatTime={formatTime}
                />
              ))}
            </div>

            <div
              className="hidden min-w-0 lg:block"
              data-stale={isFetching ? "true" : undefined}
            >
              <Table className="w-full table-fixed">
                <TableHeader>
                  <TableRow className="border-border hover:bg-transparent">
                    <TableHead className="w-[4%] text-xs">
                      <SelectionCheckbox
                        ariaLabel={t("events.row.headerCheckboxLabel")}
                        checked={allOnPageSelected}
                        indeterminate={someOnPageSelected}
                        disabled={selectionDisabled}
                        onChange={() => onToggleAllOnPage(idsOnPage)}
                      />
                    </TableHead>
                    <TableHead className="w-[12%] text-xs">{t("common.time")}</TableHead>
                    <TableHead className="w-[18%] text-xs">{t("events.flow")}</TableHead>
                    <TableHead className="w-[10%] text-xs">{t("events.class")}</TableHead>
                    <TableHead className="w-[8%] text-right text-xs">{t("events.score")}</TableHead>
                    <TableHead className="w-[8%] text-center text-xs">{t("events.decision")}</TableHead>
                    <TableHead className="w-[9%] text-center text-xs">
                      {t("events.evidenceStatus.pending")}
                    </TableHead>
                    <TableHead className="w-[31%] text-right text-xs">{t("common.actions")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {events.map((event) => (
                    <EventTableRow
                      key={event.id}
                      event={event}
                      isSelected={selectedIds.has(event.id)}
                      selectionDisabled={selectionDisabled}
                      eventActionPending={eventActionPending}
                      pendingEventAction={pendingEventAction}
                      onToggleOne={onToggleOne}
                      onResolveEvidence={onResolveEvidence}
                      onStartInspectWorkflow={onStartInspectWorkflow}
                      onStartBlockWorkflow={onStartBlockWorkflow}
                      workflowMutationsDisabled={workflowMutationsDisabled}
                      t={t}
                      formatTime={formatTime}
                    />
                  ))}
                </TableBody>
              </Table>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
