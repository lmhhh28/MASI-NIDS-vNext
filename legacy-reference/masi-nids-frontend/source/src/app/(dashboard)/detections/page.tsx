"use client";

import Link from "next/link";
import { Suspense, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  TriangleAlert,
} from "lucide-react";

import { EmptyState, ErrorState, LoadingState } from "@/components/async-state";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { useEventsV3 } from "@/hooks/use-events-v3";
import { useI18n } from "@/lib/i18n";
import { shortIdentity } from "@/lib/pipeline-v3";
import {
  DetectionsV3ListShell,
  useDetectionsTargetFilter,
} from "./detections-v3-list-shell";
import type {
  EventV3Kind,
  EventV3Summary,
  ModelRoleV3,
  NidsDecision,
} from "@/types/api";

function decisionVariant(decision: NidsDecision) {
  if (decision === "normal") return "success" as const;
  if (decision === "anomaly") return "destructive" as const;
  return "warning" as const;
}

function eventKindVariant(kind: EventV3Kind) {
  if (kind === "RECOVERY" || kind === "NORMAL_HEARTBEAT") return "success" as const;
  if (kind === "ANOMALY_ENTER" || kind === "ANOMALY_UPDATE") return "destructive" as const;
  return "warning" as const;
}

function EventCard({ event }: { event: EventV3Summary }) {
  const { t, formatDateTime, formatNumber } = useI18n();
  return (
    <Card size="sm" className="transition-colors hover:ring-primary/35 focus-within:ring-primary/50">
      <CardHeader className="grid-cols-[minmax(0,1fr)_auto]">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={eventKindVariant(event.event_kind)}>{event.event_kind}</Badge>
            <Badge variant={decisionVariant(event.decision)}>{event.decision}</Badge>
            <Badge variant="outline">{event.model_role}</Badge>
          </div>
          <CardTitle className="truncate text-sm">
            {event.nearest_class || t("common.unknown")} · {event.dst_ip}:{event.dst_port ?? "*"}
          </CardTitle>
        </div>
        <span className="text-xs tabular-nums text-muted-foreground">
          {formatDateTime(event.window_end_at)}
        </span>
      </CardHeader>
      <CardContent className="space-y-3">
        <dl className="grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2 xl:grid-cols-4">
          <div>
            <dt className="text-muted-foreground">{t("detections.v3.target")}</dt>
            <dd className="mt-0.5 font-mono" title={event.target_uuid}>{shortIdentity(event.target_uuid)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">{t("detections.v3.generation")}</dt>
            <dd className="mt-0.5 font-mono tabular-nums">{formatNumber(event.runtime_generation)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">{t("detections.v3.compatibility")}</dt>
            <dd className="mt-0.5 tabular-nums">{formatNumber(event.compatibility, { style: "percent", maximumFractionDigits: 1 })}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">{t("detections.v3.completeness")}</dt>
            <dd className="mt-0.5 tabular-nums">{formatNumber(event.completeness, { style: "percent", maximumFractionDigits: 1 })}</dd>
          </div>
        </dl>
        <Link
          href={`/detections/${event.event_id}`}
          prefetch={false}
          className={buttonVariants({
            variant: "outline",
            size: "sm",
            className: "min-h-11 sm:min-h-9",
          })}
        >
          {t("detections.v3.viewEvent")}
        </Link>
      </CardContent>
    </Card>
  );
}

function DetectionsV3Content() {
  const { t, formatTime } = useI18n();
  const [decision, setDecision] = useState<NidsDecision | "all">("all");
  const [modelRole, setModelRole] = useState<ModelRoleV3 | "all">("all");
  const [cursorHistory, setCursorHistory] = useState<(string | null)[]>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const targetFilter = useDetectionsTargetFilter(resetEventCursor);

  const eventsQuery = useEventsV3({
    cursor: cursorHistory[pageIndex],
    decision,
    modelRole,
    targetUuid: targetFilter.appliedTargetUuid,
    limit: 50,
  });

  function resetEventCursor() {
    setCursorHistory([null]);
    setPageIndex(0);
  }

  function nextPage() {
    const nextCursor = eventsQuery.data?.next_cursor;
    if (!nextCursor) return;
    setCursorHistory((previous) => [...previous.slice(0, pageIndex + 1), nextCursor]);
    setPageIndex((previous) => previous + 1);
  }

  return (
    <DetectionsV3ListShell
      activeView="events"
      filter={targetFilter}
      isRefreshing={eventsQuery.isFetching}
      onRefresh={() => void eventsQuery.refetch()}
    >
      <section className="space-y-4" aria-label={t("detections.v3.eventsTab")}>
        <div className="flex flex-col gap-3 rounded-xl border bg-card p-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="event-v3-decision">{t("detections.v3.decision")}</Label>
              <select
                id="event-v3-decision"
                value={decision}
                onChange={(event) => { setDecision(event.target.value as NidsDecision | "all"); resetEventCursor(); }}
                className="min-h-11 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50 sm:min-h-9 sm:w-44"
              >
                <option value="all">{t("common.all")}</option>
                <option value="anomaly">anomaly</option>
                <option value="unknown">unknown</option>
                <option value="normal">normal</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="event-v3-role">{t("detections.v3.modelRole")}</Label>
              <select
                id="event-v3-role"
                value={modelRole}
                onChange={(event) => { setModelRole(event.target.value as ModelRoleV3 | "all"); resetEventCursor(); }}
                className="min-h-11 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50 sm:min-h-9 sm:w-44"
              >
                <option value="all">{t("common.all")}</option>
                <option value="champion">champion</option>
                <option value="shadow">shadow</option>
              </select>
            </div>
          </div>
          {eventsQuery.dataUpdatedAt ? (
            <p className="text-xs text-muted-foreground" aria-live="polite">
              {eventsQuery.isFetching ? t("detections.v3.refreshing") : t("detections.v3.updatedAt", { time: formatTime(eventsQuery.dataUpdatedAt) })}
            </p>
          ) : null}
        </div>

        {eventsQuery.isLoading ? <LoadingState label={t("detections.v3.loadingEvents")} rows={5} /> : null}
        {eventsQuery.isError && !eventsQuery.data ? (
          <ErrorState error={eventsQuery.error} title={t("detections.v3.eventsLoadFailed")} onRetry={() => void eventsQuery.refetch()} />
        ) : null}
        {eventsQuery.isError && eventsQuery.data ? (
          <Alert variant="destructive">
            <TriangleAlert aria-hidden="true" />
            <AlertTitle>{t("detections.v3.staleData")}</AlertTitle>
            <AlertDescription>{t("detections.v3.staleDataDescription")}</AlertDescription>
          </Alert>
        ) : null}
        {eventsQuery.data?.items.length === 0 ? (
          <EmptyState title={t("detections.v3.noEvents")} description={t("detections.v3.noEventsDescription")} />
        ) : null}
        {eventsQuery.data?.items.length ? (
          <div className="space-y-3">
            {eventsQuery.data.items.map((event) => <EventCard key={event.event_id} event={event} />)}
          </div>
        ) : null}
        {eventsQuery.data?.items.length ? (
          <nav className="flex items-center justify-between" aria-label={t("detections.v3.eventPagination")}>
            <Button className="min-h-11 sm:min-h-9" variant="outline" size="sm" onClick={() => setPageIndex((value) => Math.max(0, value - 1))} disabled={pageIndex === 0 || eventsQuery.isFetching}>
              <ChevronLeft aria-hidden="true" />{t("common.back")}
            </Button>
            <span className="text-xs tabular-nums text-muted-foreground">{t("detections.v3.pageNumber", { page: pageIndex + 1 })}</span>
            <Button className="min-h-11 sm:min-h-9" variant="outline" size="sm" onClick={nextPage} disabled={!eventsQuery.data.next_cursor || eventsQuery.isFetching}>
              {t("common.next")}<ChevronRight aria-hidden="true" />
            </Button>
          </nav>
        ) : null}
      </section>
    </DetectionsV3ListShell>
  );
}

export default function DetectionsV3Page() {
  return (
    <Suspense fallback={<LoadingState rows={5} />}>
      <DetectionsV3Content />
    </Suspense>
  );
}
