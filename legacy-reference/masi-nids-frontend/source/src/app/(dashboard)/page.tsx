"use client";

import { useState } from "react";
import { Activity, Radio, History } from "lucide-react";

import { SwitchStatus } from "@/components/dashboard/switch-status";
import { EventTablePagination } from "@/components/dashboard/event-table-pagination";
import { ErrorState, LoadingState } from "@/components/async-state";
import { RuntimeStatusPanel } from "@/components/runtime-status/runtime-status-panel";
import { StatusCardsV3 } from "@/components/dashboard/status-cards-v3";
import { TrendsChartV3 } from "@/components/dashboard/trends-chart-v3";
import { LiveSeriesChart, type LiveMetric } from "@/components/dashboard/live-series-chart";

import {
  useEventsV3,
  useIncidentsV3,
} from "@/hooks/use-events-v3";
import { useTrendsV3 } from "@/hooks/use-trends-v3";
import { useLiveV3 } from "@/hooks/use-live-v3";
import { useDemoRunProjection } from "@/hooks/use-demo-run-projection";
import { useDemoTrafficStatus } from "@/hooks/use-demo-traffic";
import { useSwitches } from "@/hooks/use-p4";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { useRuntimeStatusV1 } from "@/hooks/use-runtime-status-v1";
import { useOperationsSummary } from "@/hooks/use-operations";
import { useI18n } from "@/lib/i18n";
import { unavailableRuntimeProjection } from "@/lib/runtime-status-v1";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";

type DashboardMode = "live" | "history";

const DEFAULT_TARGET = "00000000-0000-4000-8000-000000000005";
const DEFAULT_GENERATION = 3;
const DEFAULT_MODEL_RELEASE = "demo-ae-v1";

export default function DashboardPage() {
  const [cursorHistory, setCursorHistory] = useState<(string | null)[]>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const [mode, setMode] = useState<DashboardMode>("live");
  const [metric, setMetric] = useState<LiveMetric>("pps");
  const { t } = useI18n();

  // Event-v3 data sources (design r9 §13.2 cutover)
  const incidentsV3Q = useIncidentsV3({ status: "all", limit: 100 });
  const trendsV3Q = useTrendsV3({
    from: null,
    to: null,
    bucket: "5m",
  });
  const liveV3Q = useLiveV3(mode === "live" ? {
    targetUuid: DEFAULT_TARGET,
    runtimeGeneration: DEFAULT_GENERATION,
    modelRole: "champion",
    modelReleaseId: DEFAULT_MODEL_RELEASE,
  } : null);
  const operationsQ = useOperationsSummary();
  const demoTrafficStatusQ = useDemoTrafficStatus();
  const switchesQ = useSwitches();
  const workflowSafety = useRuntimeSafety("workflow");
  const runtimeStatusQ = useRuntimeStatusV1();
  const demoProjectionQ = useDemoRunProjection(
    demoTrafficStatusQ.data?.active_operation_id ?? null,
  );

  const currentCursor = cursorHistory[pageIndex];
  const eventsV3PageQ = useEventsV3({
    cursor: currentCursor,
    limit: pageSize,
  });
  const eventPage = eventsV3PageQ.data?.items ?? [];
  const hasNextPage = Boolean(eventsV3PageQ.data?.next_cursor);
  const eventCount = eventPage.length;
  const page = pageIndex + 1;
  const showingFrom = eventCount === 0 ? 0 : pageIndex * pageSize + 1;
  const showingTo = pageIndex * pageSize + eventCount;
  const eventTotal = hasNextPage
    ? pageIndex * pageSize + eventCount + 1
    : pageIndex * pageSize + eventCount;

  const handlePageSizeChange = (size: number) => {
    setPageSize(size);
    setCursorHistory([null]);
    setPageIndex(0);
  };

  const handlePageChange = (nextPage: number) => {
    if (nextPage < page) {
      setPageIndex(Math.max(0, nextPage - 1));
      return;
    }
    const nextCursor = eventsV3PageQ.data?.next_cursor;
    if (!nextCursor) return;
    setCursorHistory((previous) => [...previous.slice(0, pageIndex + 1), nextCursor]);
    setPageIndex((previous) => previous + 1);
  };

  const demoReadiness = demoProjectionQ.data?.projection?.overall_complete === true
    ? "ready"
    : "not_implemented";

  return (
    <div className="space-y-6 p-4 sm:p-6">
      <h1 className="text-lg font-semibold">{t("dashboard.title")}</h1>

      {/* Legacy Event-v2 boundary marker (design r9 §13.4 / legacy_boundary_contract):
          the dashboard never reads Event-v2 data; the badge declares the
          retired per-flow source is read-only and out of the live chain. */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="outline">{t("dashboard.legacyEventV2Badge")}</Badge>
        <span>{t("dashboard.legacyScoreWarning")}</span>
        <Badge variant="secondary">{t("dashboard.legacyReadOnly")}</Badge>
      </div>

      {/* Three-state status cards (design r9 §13.2) */}
      <StatusCardsV3
        runtimeHealth={workflowSafety.allowed ? "healthy" : "degraded"}
        demoReadiness={demoReadiness}
        qualification="out_of_scope"
        lastObservedAt={runtimeStatusQ.data?.roles.telemetry_v3.observedAt ?? null}
      />

      {/* Runtime status panel */}
      {runtimeStatusQ.isLoading ? (
        <LoadingState label={t("runtimeStatus.title")} rows={3} />
      ) : (
        <RuntimeStatusPanel
          projection={runtimeStatusQ.data ?? unavailableRuntimeProjection()}
          compact
          refreshing={runtimeStatusQ.isFetching}
          onRefresh={() => void runtimeStatusQ.refetch()}
        />
      )}
      {runtimeStatusQ.isError ? (
        <ErrorState
          title={t("operations.runtimeUnknown")}
          error={runtimeStatusQ.error}
          onRetry={() => void runtimeStatusQ.refetch()}
        />
      ) : null}

      {/* Operations summary */}
      {operationsQ.data ? (
        <Card>
          <CardContent className="grid gap-4 p-4 sm:grid-cols-2 lg:grid-cols-5">
            <div><p className="text-xs text-muted-foreground">{t("operations.workflowQueue")}</p><p className="mt-1 font-mono text-sm">{operationsQ.data.workflows.queue.ready} ready · {operationsQ.data.workflows.queue.running} running</p></div>
            <div><p className="text-xs text-muted-foreground">{t("operations.workers")}</p><p className="mt-1 font-mono text-sm">{operationsQ.data.workflows.workers.active} active · {operationsQ.data.workflows.workers.stale} stale · {operationsQ.data.workflows.workers.inflight} inflight</p></div>
            <div><p className="text-xs text-muted-foreground">{t("operations.p4Unknown")}</p><p className="mt-1 font-mono text-sm">{operationsQ.data.p4.unknown_requests + operationsQ.data.p4.unknown_deployments + operationsQ.data.p4.rollback_unresolved}</p></div>
            <div><p className="flex items-center gap-1 text-xs text-muted-foreground"><Activity className="size-3.5" aria-hidden="true" />{t("operations.backgroundJobs")}</p><p className="mt-1 font-mono text-sm">{operationsQ.data.background_jobs?.failing ?? 0} failing · {operationsQ.data.background_jobs?.running ?? 0} running</p></div>
            <div><p className="text-xs text-muted-foreground">{t("operations.databaseWait")}</p><p className="mt-1 font-mono text-sm">{operationsQ.data.database.requests_waiting} · p95 {operationsQ.data.database.acquire_wait_p95_ms.toFixed(1)} ms</p></div>
          </CardContent>
        </Card>
      ) : null}

      {/* Traffic & detection monitoring: mode selector + chart (design r9 §13.2) */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-1 rounded-md border border-border p-0.5" role="tablist" aria-label="实时监控模式">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "live"}
              onClick={() => setMode("live")}
              className={`flex items-center gap-1 rounded px-3 py-1.5 text-sm transition-colors ${mode === "live" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`}
            >
              <Radio className="size-3.5" aria-hidden="true" />
              实时
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "history"}
              onClick={() => setMode("history")}
              className={`flex items-center gap-1 rounded px-3 py-1.5 text-sm transition-colors ${mode === "history" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`}
            >
              <History className="size-3.5" aria-hidden="true" />
              历史
            </button>
          </div>
          {mode === "live" ? (
            <div className="flex items-center gap-1 rounded-md border border-border p-0.5" role="group" aria-label="指标选择">
              {(["pps", "bps"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setMetric(m)}
                  data-active={metric === m}
                  className={`rounded px-3 py-1.5 text-sm transition-colors ${metric === m ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`}
                >
                  {m === "pps" ? "PPS" : "BPS"}
                </button>
              ))}
            </div>
          ) : null}
        </div>

        <div className="grid gap-4 lg:grid-cols-5">
          <div className="lg:col-span-3">
            {mode === "live" ? (
              <LiveSeriesChart
                samples={liveV3Q.data?.samples ?? []}
                freshness={liveV3Q.data?.freshness ?? null}
                metric={metric}
                isLoading={liveV3Q.isLoading}
                latestSampleAt={liveV3Q.data?.latest_sample_at ?? null}
              />
            ) : (
              <TrendsChartV3
                buckets={trendsV3Q.data?.buckets ?? []}
                bucket={trendsV3Q.data?.bucket ?? "5m"}
                isLoading={trendsV3Q.isLoading}
              />
            )}
          </div>
          <div className="lg:col-span-2">
            <SwitchStatus
              switches={switchesQ.data}
              isLoading={switchesQ.isLoading}
            />
          </div>
        </div>
      </div>

      {/* Event-v3 list (native v3 data, not legacy per-flow events) */}
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Badge variant="outline">事件</Badge>
          <span className="text-xs text-muted-foreground">
            {eventTotal > 0 ? `${showingFrom}-${showingTo} / ${eventTotal}` : "无事件"}
          </span>
        </div>
        {eventsV3PageQ.isLoading ? (
          <LoadingState label="事件" rows={5} />
        ) : eventsV3PageQ.isError ? (
          <ErrorState title="事件加载失败" error={eventsV3PageQ.error} onRetry={() => eventsV3PageQ.refetch()} />
        ) : eventPage.length > 0 ? (
          <div className="rounded-md border border-border">
            <table className="w-full text-sm">
              <thead className="border-b border-border bg-muted/30">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">时间</th>
                  <th className="px-3 py-2 text-left font-medium">目标</th>
                  <th className="px-3 py-2 text-left font-medium">判定</th>
                  <th className="px-3 py-2 text-left font-medium">类型</th>
                  <th className="px-3 py-2 text-left font-medium">完整度</th>
                </tr>
              </thead>
              <tbody>
                {eventPage.map((event) => (
                  <tr key={event.event_id} className="border-b border-border/50 hover:bg-muted/20">
                    <td className="px-3 py-2 font-mono text-xs">{event.created_at}</td>
                    <td className="px-3 py-2 font-mono text-xs">{event.target_uuid.slice(0, 8)}</td>
                    <td className="px-3 py-2">{event.decision}</td>
                    <td className="px-3 py-2">{event.event_kind}</td>
                    <td className="px-3 py-2 font-mono text-xs">{event.completeness.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Card className="border-border bg-card">
            <CardContent className="flex h-24 items-center justify-center text-sm text-muted-foreground">
              当前范围无事件
            </CardContent>
          </Card>
        )}
        <EventTablePagination
          page={page}
          pageSize={pageSize}
          total={eventTotal}
          onPageChange={handlePageChange}
          onPageSizeChange={handlePageSizeChange}
          isLoading={eventsV3PageQ.isLoading}
        />
      </div>

      {/* Active incidents (v3) */}
      {incidentsV3Q.data && incidentsV3Q.data.length > 0 ? (
        <div className="space-y-2">
          <h2 className="text-sm font-medium">最新事件</h2>
          <div className="rounded-md border border-border">
            <table className="w-full text-sm">
              <thead className="border-b border-border bg-muted/30">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Incident ID</th>
                  <th className="px-3 py-2 text-left font-medium">状态</th>
                  <th className="px-3 py-2 text-left font-medium">目标</th>
                  <th className="px-3 py-2 text-left font-medium">进入时间</th>
                </tr>
              </thead>
              <tbody>
                {incidentsV3Q.data.map((incident) => (
                  <tr key={incident.incident_id} className="border-b border-border/50 hover:bg-muted/20">
                    <td className="px-3 py-2 font-mono text-xs">{incident.incident_id.slice(0, 8)}</td>
                    <td className="px-3 py-2">{incident.status}</td>
                    <td className="px-3 py-2 font-mono text-xs">{incident.target_uuid?.slice(0, 8) ?? "—"}</td>
                    <td className="px-3 py-2 font-mono text-xs">{incident.opened_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}