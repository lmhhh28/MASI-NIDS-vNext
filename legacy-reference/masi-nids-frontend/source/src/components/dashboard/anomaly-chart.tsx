"use client";

import { useMemo } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  ReferenceLine,
  Tooltip as ReTooltip,
  ResponsiveContainer,
  type TooltipContentProps,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n, type I18nContextValue } from "@/lib/i18n";
import type { NidsEvent } from "@/types/api";

interface AnomalyChartProps {
  events: NidsEvent[] | undefined;
  isLoading: boolean;
}

interface ChartPoint {
  windowLabel: string;
  windowRange: string;
  score: number;
  decision: string;
  nearestClass: string | null;
  flowId: string | null;
  sourceEventId: string;
  ingestedAt: string;
}

function isFiniteNumber(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function formatWindowBound(value: number | null | undefined) {
  return isFiniteNumber(value) ? `${value.toFixed(1)}s` : "-";
}

function formatWindowRange(
  start: number | null | undefined,
  end: number | null | undefined,
) {
  if (isFiniteNumber(start) && isFiniteNumber(end)) {
    return `${formatWindowBound(start)}-${formatWindowBound(end)}`;
  }
  if (isFiniteNumber(end)) return formatWindowBound(end);
  if (isFiniteNumber(start)) return formatWindowBound(start);
  return "-";
}

function decisionLabel(decision: string, t: I18nContextValue["t"]) {
  if (decision === "anomaly") return t("events.decisionAnomaly");
  if (decision === "normal") return t("events.decisionNormal");
  if (decision === "unknown") return t("events.decisionUnknown");
  return decision || "-";
}

function TooltipRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 items-start justify-between gap-4">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 truncate text-right font-mono tabular-nums">
        {value}
      </span>
    </div>
  );
}

function AnomalyTooltip({
  active,
  payload,
  t,
  formatTime,
}: TooltipContentProps & {
  t: I18nContextValue["t"];
  formatTime: I18nContextValue["formatTime"];
}) {
  if (!active || !payload?.length) return null;
  const point = payload[0]?.payload as ChartPoint | undefined;
  if (!point) return null;

  const ingestedAt = point.ingestedAt
    ? formatTime(point.ingestedAt, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "-";

  return (
    <div className="w-72 rounded-md border border-border bg-popover p-3 text-xs text-popover-foreground shadow-lg">
      <div className="mb-2 min-w-0">
        <p className="truncate font-medium text-foreground">
          {t("chart.windowRange")}: {point.windowRange}
        </p>
      </div>
      <div className="space-y-1.5">
        <TooltipRow
          label={t("events.score")}
          value={point.score.toFixed(4)}
        />
        <TooltipRow
          label={t("events.decision")}
          value={decisionLabel(point.decision, t)}
        />
        <TooltipRow
          label={t("events.class")}
          value={point.nearestClass ?? "-"}
        />
        <TooltipRow label={t("events.flow")} value={point.flowId ?? "-"} />
        <TooltipRow label={t("chart.eventId")} value={point.sourceEventId} />
        <TooltipRow label={t("chart.ingestedAt")} value={ingestedAt} />
      </div>
    </div>
  );
}

export function AnomalyChart({ events, isLoading }: AnomalyChartProps) {
  const { t, formatTime } = useI18n();
  const chartData = useMemo<ChartPoint[]>(() => {
    if (!events || events.length === 0) return [];
    return events
      .filter((e) => e.legacy_score != null)
      .slice(0, 60)
      .reverse()
      .map((e) => ({
        windowLabel: formatWindowBound(e.timestamp_end ?? e.timestamp_start),
        windowRange: formatWindowRange(e.timestamp_start, e.timestamp_end),
        score: e.legacy_score ?? 0,
        decision: e.decision ?? "unknown",
        nearestClass: e.nearest_class,
        flowId: e.flow_id ?? `${e.src_ip ?? "?"}->${e.dst_ip ?? "?"}`,
        sourceEventId: e.source_event_id,
        ingestedAt: e.ingested_at,
      }));
  }, [events]);
  const aboveThreshold = chartData.filter((point) => point.score > 1).length;
  const maximumScore = chartData.reduce((maximum, point) => Math.max(maximum, point.score), 0);
  const chartSummary = t("chart.textSummary", {
    count: chartData.length,
    above: aboveThreshold,
    maximum: maximumScore.toFixed(2),
  });

  if (isLoading) {
    return (
      <Card className="border-border bg-card">
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">
            {t("chart.anomalyTrend")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-56 w-full rounded-lg" />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-border bg-card">
      <CardHeader>
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {t("chart.anomalyTrend")}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {chartData.length === 0 ? (
          <div className="flex h-56 items-center justify-center text-sm text-muted-foreground">
            {t("chart.noEventData")}
          </div>
        ) : (
          <div className="space-y-3">
            <p id="anomaly-chart-summary" className="text-sm text-muted-foreground">
              {chartSummary}
            </p>
            <div
              className="h-56 w-full focus-visible:rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              role="img"
              tabIndex={0}
              aria-describedby="anomaly-chart-summary"
              aria-label={t("chart.accessibleLabel")}
            >
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart
                  accessibilityLayer
                  data={chartData}
                  margin={{ top: 12, right: 12, left: 0, bottom: 4 }}
                >
              <defs>
                <linearGradient id="scoreGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop
                    offset="5%"
                    stopColor="var(--chart-1)"
                    stopOpacity={0.4}
                  />
                  <stop
                    offset="95%"
                    stopColor="var(--chart-1)"
                    stopOpacity={0}
                  />
                </linearGradient>
              </defs>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="var(--border)"
                vertical={false}
              />
              <XAxis
                dataKey="windowLabel"
                tick={{ fill: "var(--muted-foreground)", fontSize: 11 }}
                tickLine={false}
                axisLine={false}
              />
                  <YAxis
                tick={{ fill: "var(--muted-foreground)", fontSize: 11 }}
                tickLine={false}
                axisLine={false}
                domain={[0, "auto"]}
                label={{
                  value: t("chart.scoreRatio"),
                  angle: -90,
                  position: "insideLeft",
                  fill: "var(--muted-foreground)",
                  fontSize: 11,
                }}
              />
                  <ReferenceLine
                    y={1}
                    stroke="var(--warning)"
                    strokeDasharray="5 4"
                    label={{
                      value: t("chart.acceptanceThreshold"),
                      position: "insideTopRight",
                      fill: "var(--warning)",
                      fontSize: 11,
                    }}
                  />
              <ReTooltip
                content={(props) => (
                  <AnomalyTooltip
                    {...props}
                    t={t}
                    formatTime={formatTime}
                  />
                )}
              />
                  <Area
                type="monotone"
                dataKey="score"
                stroke="var(--chart-1)"
                strokeWidth={2}
                fill="url(#scoreGradient)"
                dot={false}
                activeDot={{
                  r: 4,
                  fill: "var(--chart-1)",
                  stroke: "var(--background)",
                  strokeWidth: 2,
                }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <details className="rounded-lg border border-border bg-muted/20 p-3">
              <summary className="cursor-pointer text-sm font-medium">
                {t("chart.dataTable")}
              </summary>
              <div className="mt-3 max-h-64 overflow-auto">
                <table className="w-full text-left text-xs">
                  <thead className="sticky top-0 bg-card">
                    <tr>
                      <th className="p-2">{t("chart.windowRange")}</th>
                      <th className="p-2">{t("events.score")}</th>
                      <th className="p-2">{t("events.decision")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {chartData.map((point) => (
                      <tr key={`${point.sourceEventId}-${point.ingestedAt}`} className="border-t border-border">
                        <td className="p-2 font-mono">{point.windowRange}</td>
                        <td className="p-2 font-mono tabular-nums">{point.score.toFixed(4)}</td>
                        <td className="p-2">{decisionLabel(point.decision, t)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
