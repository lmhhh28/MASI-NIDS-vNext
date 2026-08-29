"use client";

import { useMemo, useState } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

// Event-v3 trends chart.
// Frozen baseline: Requirements r3 / Design r9 §13.2-13.4.
// Contract coverage: src/components/dashboard/trends-chart-v3.test.tsx
//
// Plots rate (PPS) and event-kind series from the minute-level Event-v3
// rollup. It never plots a retired Event-v2 score, never draws a fixed y=1
// threshold, and never treats a conformal p-value as a score.

export interface TrendsChartBucket {
  bucket_start: string;
  total: number;
  normal: number;
  anomaly: number;
  unknown: number;
  recovery: number;
  max_packets_per_second: number | null;
  max_bytes_per_second: number | null;
  min_completeness: number | null;
}

interface TrendsChartPoint extends TrendsChartBucket {
  label: string;
  rate: number | null;
}

export function TrendsChartV3({
  buckets,
  bucket,
  isLoading,
}: {
  buckets: TrendsChartBucket[];
  bucket: "1m" | "5m" | "15m" | "1h";
  isLoading: boolean;
}) {
  const [hovered, setHovered] = useState<number | null>(null);

  const series = useMemo(
    () =>
      buckets.map((b) => ({
        ...b,
        label: b.bucket_start,
        rate: b.max_packets_per_second ?? null,
      })),
    [buckets],
  );

  const peakRate = useMemo(() => {
    let peak = 0;
    for (const b of series) {
      const rate = b.rate ?? 0;
      if (rate > peak) peak = rate;
    }
    return peak;
  }, [series]);

  if (isLoading) {
    return (
      <div
        role="status"
        aria-label="加载中"
        className="flex h-48 items-center justify-center rounded-md border border-border bg-muted/40 text-sm text-muted-foreground"
      >
        加载中…
      </div>
    );
  }

  const hasObservations = series.some((b) => b.total > 0 || (b.rate ?? 0) > 0);
  const observed = series.filter((b) => b.rate != null);
  const describedById = "trends-chart-v3-summary";

  return (
    <figure
      className="rounded-md border border-border bg-card p-3"
      aria-describedby={describedById}
    >
      <div id={describedById} className="sr-only">
        {hasObservations
          ? `观测到 ${series.length} 个${bucket}桶，峰值 ${peakRate} pps（未求和）`
          : "当前范围无观测数据"}
      </div>
      {hasObservations ? (
        <div className="h-48">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={series}
              margin={{ top: 8, right: 12, bottom: 4, left: 4 }}
              onMouseMove={(state) => {
                const index =
                  state && typeof state.activeTooltipIndex === "number"
                    ? state.activeTooltipIndex
                    : null;
                setHovered(index);
              }}
              onMouseLeave={() => setHovered(null)}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                width={56}
                tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
              />
              <Tooltip
                content={({ active, payload }) => {
                  if (!active || !payload || payload.length === 0) return null;
                  const point = payload[0]?.payload as TrendsChartPoint | undefined;
                  if (!point) return null;
                  return (
                    <div className="rounded-md border border-border bg-popover px-3 py-2 text-xs shadow-sm">
                      <div className="font-medium">{point.label}</div>
                      <div>速率 {point.rate ?? "无"} pps</div>
                      <div>正常 {point.normal} / 异常 {point.anomaly} / 未知 {point.unknown}</div>
                      <div>恢复 {point.recovery}</div>
                    </div>
                  );
                }}
              />
              <Area
                type="monotone"
                dataKey="rate"
                name="rate"
                stroke="var(--primary)"
                fill="var(--primary)"
                fillOpacity={0.15}
                strokeWidth={1.5}
                connectNulls={false}
              />
              <Area
                type="monotone"
                dataKey="anomaly"
                name="anomaly"
                stroke="var(--destructive)"
                fill="var(--destructive)"
                fillOpacity={0.1}
                strokeWidth={1.5}
                connectNulls={false}
              />
              <Area
                type="monotone"
                dataKey="normal"
                name="normal"
                stroke="var(--success, var(--primary))"
                fill="transparent"
                strokeWidth={1.5}
                connectNulls={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
          当前范围无样本
        </div>
      )}
      <figcaption className="mt-2 flex items-center justify-between text-xs text-muted-foreground">
        <span>
          实时速率与事件判定 · {bucket}
        </span>
        <span aria-hidden="true">
          {observed.length > 0 ? `${observed.length} 个观测桶` : "无观测"}
        </span>
      </figcaption>
    </figure>
  );
}
