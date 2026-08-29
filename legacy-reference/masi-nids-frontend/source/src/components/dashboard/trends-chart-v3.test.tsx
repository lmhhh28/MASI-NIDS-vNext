import { readFileSync } from "node:fs";
import path from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TrendsChartV3 } from "@/components/dashboard/trends-chart-v3";

// Regression coverage for the design r9 Event-v3 trends chart. The implementation
// begins as a comment-only stub, so the expected pre-failure is its missing component export.
//
// Design r9 §13.4 retires the Event-v2 AnomalyChart rather than adapting it:
// the old chart plots `anomaly_score` and draws a fixed `ReferenceLine y={1}`
// threshold.  Event v3 has no such score -- `compatibility` is a conformal
// p-value -- so the replacement plots rates and event kinds instead, and must
// not smuggle the retired semantics back in through a field adapter.
// Frozen baseline: Requirements r3 / Design r9 §13.2-13.4.

const COMPONENT_SOURCE = path.join(__dirname, "trends-chart-v3.tsx");

type Bucket = {
  bucket_start: string;
  total: number;
  normal: number;
  anomaly: number;
  unknown: number;
  recovery: number;
  max_packets_per_second: number;
  max_bytes_per_second: number;
  min_completeness: number;
};

function bucket(overrides: Partial<Bucket> = {}): Bucket {
  return {
    bucket_start: "2032-03-01T12:00:00+00:00",
    total: 0,
    normal: 0,
    anomaly: 0,
    unknown: 0,
    recovery: 0,
    max_packets_per_second: 0,
    max_bytes_per_second: 0,
    min_completeness: 0,
    ...overrides,
  };
}

const BUCKETS: Bucket[] = [
  bucket({ bucket_start: "2032-03-01T12:00:00+00:00", total: 6, normal: 6, max_packets_per_second: 120 }),
  bucket({ bucket_start: "2032-03-01T12:05:00+00:00", total: 9, anomaly: 9, max_packets_per_second: 4800 }),
  bucket({ bucket_start: "2032-03-01T12:10:00+00:00", total: 3, recovery: 3, max_packets_per_second: 200 }),
];

describe("TrendsChartV3", () => {
  it("renders an accessible summary of the supplied buckets", () => {
    render(<TrendsChartV3 buckets={BUCKETS} bucket="5m" isLoading={false} />);
    const figure = screen.getByRole("figure");
    expect(figure).toBeInTheDocument();
    expect(figure).toHaveAccessibleDescription();
  });

  it("reports the observed peak rate rather than a summed rate", () => {
    render(<TrendsChartV3 buckets={BUCKETS} bucket="5m" isLoading={false} />);
    const description = screen.getByRole("figure").getAttribute("aria-describedby");
    expect(description).toBeTruthy();
    const summary = document.getElementById(String(description));
    expect(summary?.textContent ?? "").toContain("4800");
    expect(summary?.textContent ?? "").not.toContain("5120");
  });

  it("renders an empty state for a fully zero-filled range without crashing", () => {
    render(<TrendsChartV3 buckets={[bucket(), bucket()]} bucket="1m" isLoading={false} />);
    expect(screen.getByRole("figure")).toBeInTheDocument();
  });

  it("renders a loading state instead of an empty chart", () => {
    render(<TrendsChartV3 buckets={[]} bucket="1m" isLoading />);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("does not expose a retired Event-v2 score anywhere in its output", () => {
    const { container } = render(<TrendsChartV3 buckets={BUCKETS} bucket="5m" isLoading={false} />);
    const rendered = container.innerHTML;
    for (const retired of ["anomaly_score", "anomaly score", "Anomaly Score"]) {
      expect(rendered).not.toContain(retired);
    }
  });

  it("does not reintroduce the fixed y=1 threshold or the retired score field", () => {
    // A rendered-output assertion cannot see a ReferenceLine that recharts skips
    // in jsdom, so the retired constructs are asserted against the source.
    const source = readFileSync(COMPONENT_SOURCE, "utf-8");
    expect(source).not.toContain("anomaly_score");
    expect(source).not.toContain("ReferenceLine");
    expect(source).not.toMatch(/y=\{1\}/);
    expect(source).not.toContain("compatibility");
  });

  it("plots rate and event-kind series, not a single score series", () => {
    const source = readFileSync(COMPONENT_SOURCE, "utf-8");
    expect(source).toContain("max_packets_per_second");
    expect(source).toContain("anomaly");
    expect(source).toContain("normal");
  });
});
