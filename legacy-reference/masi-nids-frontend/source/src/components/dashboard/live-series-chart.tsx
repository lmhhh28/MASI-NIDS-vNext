"use client";

import { useMemo } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { DashboardLiveSampleV1, LiveFreshness } from "@/types/api";

// DashboardLiveSampleV1 live series chart.
// Frozen baseline: Requirements r3 / Design r9 §13.1.1, §13.2, §5.
// Contract coverage: src/components/dashboard/live-series-chart.test.tsx
//
// Renders real 5-second samples in a fixed-height 180-point chart. Missing
// samples are shown as gaps (broken lines / null), never filled with zero.
// Stale state preserves the last-good value with an age/reason indicator.
// No retired Event-v2 score metric, no fixed y=1 threshold, no v2 score mapping.

export type LiveMetric = "pps" | "bps";

export interface LiveSeriesChartProps {
  samples: DashboardLiveSampleV1[];
  freshness: LiveFreshness | null;
  metric: LiveMetric;
  isLoading: boolean;
  latestSampleAt: string | null;
}

interface LiveChartPoint {
  label: string;
  value: number | null;
  decision: string;
  quality: string;
  generation: number;
  sample_id: string;
}

function freshnessLabel(freshness: LiveFreshness | null): string {
  if (!freshness) return "未知";
  const status = freshness.status;
  if (status === "known") return "正常";
  if (status === "stale") {
    const age = freshness.age_seconds != null ? `${Math.floor(freshness.age_seconds)}秒` : "";
    const reason = freshness.reason_code ? ` · ${freshness.reason_code}` : "";
    return `过期 ${age}${reason}`;
  }
  return "未知";
}

function freshnessClass(freshness: LiveFreshness | null): string {
  if (!freshness) return "text-muted-foreground";
  if (freshness.status === "known") return "text-muted-foreground";
  return "text-amber-600 dark:text-amber-400";
}

export function LiveSeriesChart({
  samples,
  freshness,
  metric,
  isLoading,
  latestSampleAt,
}: LiveSeriesChartProps) {
  const series = useMemo<LiveChartPoint[]>(() => {
    return samples.map((s) => ({
      label: s.bucket_end_at,
      value: metric === "pps" ? s.packets_per_second : s.bytes_per_second,
      decision: s.latest_decision,
      quality: s.quality_degraded ? `降级(${s.quality_reason ?? ""})` : "正常",
      generation: s.runtime_generation,
      sample_id: s.sample_id,
    }));
  }, [samples, metric]);

  const peak = useMemo(() => {
    let p = 0;
    for (const s of series) {
      const v = s.value ?? 0;
      if (v > p) p = v;
    }
    return p;
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

  const hasSamples = series.length > 0;
  const describedById = "live-chart-v3-summary";
  const metricLabel = metric === "pps" ? "PPS" : "BPS";

  return (
    <figure
      className="rounded-md border border-border bg-card p-3"
      aria-describedby={describedById}
    >
      <div id={describedById} className="sr-only">
        {hasSamples
          ? `${series.length} 个 5 秒实时点，峰值 ${peak} ${metricLabel}（未求和）`
          : "当前范围无实时样本"}
      </div>

      {/* Freshness indicator */}
      <div className="mb-2 flex items-center justify-between text-xs">
        <span className={freshnessClass(freshness)}>
          {freshnessLabel(freshness)}
          {latestSampleAt && freshness?.status !== "known" ? ` · 最后数据 ${latestSampleAt}` : ""}
        </span>
        <span className="text-muted-foreground" aria-hidden="true">
          {hasSamples ? `${series.length} 点` : "无观测"}
        </span>
      </div>

      {hasSamples ? (
        <div className="h-48">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={series}
              margin={{ top: 8, right: 12, bottom: 4, left: 4 }}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
                minTickGap={24}
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
                  const point = payload[0]?.payload as LiveChartPoint | undefined;
                  if (!point) return null;
                  return (
                    <div className="rounded-md border border-border bg-popover px-3 py-2 text-xs shadow-sm">
                      <div className="font-medium">{point.label}</div>
                      <div>{metricLabel} {point.value ?? "缺测"}</div>
                      <div>判定 {point.decision}</div>
                      <div>质量 {point.quality}</div>
                      <div>代际 {point.generation}</div>
                    </div>
                  );
                }}
              />
              <Area
                type="monotone"
                dataKey="value"
                name={metricLabel}
                stroke="var(--primary)"
                fill="var(--primary)"
                fillOpacity={0.15}
                strokeWidth={1.5}
                connectNulls={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
          {freshness?.status === "unknown" ? "无实时数据" : "当前范围无样本"}
        </div>
      )}

      <figcaption className="mt-2 flex items-center justify-between text-xs text-muted-foreground">
        <span>
          实时 {metricLabel} · 5 秒点
        </span>
        <span aria-hidden="true">
          15 分钟 / 最多 180 点
        </span>
      </figcaption>
    </figure>
  );
}