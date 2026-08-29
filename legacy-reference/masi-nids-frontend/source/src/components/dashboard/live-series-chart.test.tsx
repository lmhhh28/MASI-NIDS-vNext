import { readFileSync } from "node:fs";
import path from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LiveSeriesChart, type LiveMetric } from "@/components/dashboard/live-series-chart";
import type { DashboardLiveSampleV1, LiveFreshness } from "@/types/api";

const SRC = path.join(__dirname, "live-series-chart.tsx");
const SOURCE = readFileSync(SRC, "utf-8");

function sample(overrides: Partial<DashboardLiveSampleV1> = {}): DashboardLiveSampleV1 {
  return {
    sample_id: "s1",
    sample_content_sha256: "abc123",
    source_run_id: "run-1",
    target_uuid: "target-1",
    runtime_generation: 3,
    agent_session_version: 1,
    model_role: "champion",
    model_release_id: "demo-ae-v1",
    bucket_start_at: "2026-08-02T03:20:00Z",
    bucket_end_at: "2026-08-02T03:20:05Z",
    packets_per_second: 100,
    bytes_per_second: 5000,
    packet_count: 500,
    byte_count: 25000,
    protocol: 6,
    latest_decision: "anomaly",
    normal_count: 1,
    anomaly_count: 3,
    unknown_count: 1,
    min_completeness: 0.95,
    quality_degraded: false,
    quality_reason: null,
    ...overrides,
  };
}

const KNOWN: LiveFreshness = { status: "known", age_seconds: 1, stale_after_seconds: 15, reason_code: null };
const STALE: LiveFreshness = { status: "stale", age_seconds: 20, stale_after_seconds: 15, reason_code: "DASHBOARD_LIVE_STALE" };
const UNKNOWN: LiveFreshness = { status: "unknown", age_seconds: null, stale_after_seconds: 15, reason_code: "DASHBOARD_LIVE_UNKNOWN" };

describe("LiveSeriesChart", () => {
  it("renders a figure with accessible description when samples exist", () => {
    render(
      <LiveSeriesChart
        samples={[sample()]}
        freshness={KNOWN}
        metric="pps"
        isLoading={false}
        latestSampleAt="2026-08-02T03:20:05Z"
      />,
    );
    const figure = screen.getByRole("figure");
    expect(figure).toBeInTheDocument();
    expect(figure).toHaveAccessibleDescription();
  });

  it("shows '无实时数据' (not a zero line) when freshness is unknown", () => {
    render(
      <LiveSeriesChart
        samples={[]}
        freshness={UNKNOWN}
        metric="pps"
        isLoading={false}
        latestSampleAt={null}
      />,
    );
    expect(screen.getByText("无实时数据")).toBeInTheDocument();
  });

  it("shows stale age and last-good when freshness is stale", () => {
    render(
      <LiveSeriesChart
        samples={[sample()]}
        freshness={STALE}
        metric="pps"
        isLoading={false}
        latestSampleAt="2026-08-02T03:20:05Z"
      />,
    );
    expect(screen.getByText(/过期/)).toBeInTheDocument();
    expect(screen.getByText(/最后数据/)).toBeInTheDocument();
  });

  it("renders a role=status loading indicator when isLoading is true", () => {
    render(
      <LiveSeriesChart
        samples={[]}
        freshness={null}
        metric="pps"
        isLoading={true}
        latestSampleAt={null}
      />,
    );
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("separates loading and empty states (loading does not show empty)", () => {
    const { rerender } = render(
      <LiveSeriesChart samples={[]} freshness={null} metric="pps" isLoading={true} latestSampleAt={null} />,
    );
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByText("无实时数据")).not.toBeInTheDocument();

    rerender(
      <LiveSeriesChart samples={[]} freshness={UNKNOWN} metric="pps" isLoading={false} latestSampleAt={null} />,
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByText("无实时数据")).toBeInTheDocument();
  });

  it("source does not contain retired Event-v2 constructs", () => {
    expect(SOURCE).not.toContain("anomaly_score");
    expect(SOURCE).not.toContain("ReferenceLine");
    expect(SOURCE).not.toContain("y={1}");
    expect(SOURCE).not.toContain("compatibility");
  });

  it("source contains live projection fields", () => {
    expect(SOURCE).toContain("packets_per_second");
    expect(SOURCE).toContain("gap");
    expect(SOURCE).toContain("stale");
    expect(SOURCE).toContain("unknown");
  });

  it("source uses connectNulls={false} (gaps, not zero-fill)", () => {
    expect(SOURCE).toContain("connectNulls={false}");
  });

  it("source has a fixed height container (no layout jump on 5s update)", () => {
    expect(SOURCE).toMatch(/h-48/);
  });

  it("uses different tooltip content for PPS vs BPS metric", () => {
    const { rerender, container } = render(
      <LiveSeriesChart samples={[sample()]} freshness={KNOWN} metric="pps" isLoading={false} latestSampleAt="t" />,
    );
    expect(container.textContent).toMatch(/实时 PPS ·/);

    rerender(
      <LiveSeriesChart samples={[sample()]} freshness={KNOWN} metric="bps" isLoading={false} latestSampleAt="t" />,
    );
    expect(container.textContent).toMatch(/实时 BPS ·/);
  });
});