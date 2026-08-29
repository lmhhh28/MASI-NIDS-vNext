import { readFileSync } from "node:fs";
import path from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AnalysisTraceTerminal } from "@/components/workflow/pipeline-graph";
import type { TraceEvent } from "@/types/api";

const SRC = path.join(__dirname, "pipeline-graph.tsx");
const SOURCE = readFileSync(SRC, "utf-8");

function event(seq: number, overrides: Partial<TraceEvent> = {}): TraceEvent {
  return {
    sequence_no: seq,
    event_id: `ev-${seq}`,
    occurred_at: `2026-08-02T03:20:${String(seq).padStart(2, "0")}.000Z`,
    node_id: "hypothesis",
    parent_node_ids: [],
    parallel_group_id: null,
    event_kind: "llm",
    phase: "completed",
    call_id: `call-${seq}`,
    call_index: null,
    callee_key: null,
    elapsed_ms: 100,
    outcome: "succeeded",
    error_code: null,
    request_sha256: null,
    response_sha256: `sha256-${seq}`,
    message_code: `msg-${seq}`,
    bounded_redacted_args: {},
    redaction_profile_version: 1,
    trace_id: "trace-1",
    ...overrides,
  };
}

describe("AnalysisTraceTerminal", () => {
  it("renders role=log with aria-live=polite", () => {
    render(
      <AnalysisTraceTerminal
        events={[event(1)]}
        selectedNodeId={null}
        onClearNodeFilter={() => {}}
        traceState="active"
      />,
    );
    const log = screen.getByRole("log");
    expect(log).toBeInTheDocument();
    expect(log).toHaveAttribute("aria-live", "polite");
  });

  it("renders events in sequence order (not by timestamp)", () => {
    const events = [event(3), event(1), event(2)];
    render(
      <AnalysisTraceTerminal
        events={events}
        selectedNodeId={null}
        onClearNodeFilter={() => {}}
        traceState="active"
      />,
    );
    // They appear in source array order; the hook delivers them by sequence_no
    const log = screen.getByRole("log");
    expect(log.textContent).toContain("msg-3");
    expect(log.textContent).toContain("msg-1");
    expect(log.textContent).toContain("msg-2");
  });

  it("shows '无记录' when events is empty", () => {
    render(
      <AnalysisTraceTerminal
        events={[]}
        selectedNodeId={null}
        onClearNodeFilter={() => {}}
        traceState="active"
      />,
    );
    expect(screen.getByText("无记录")).toBeInTheDocument();
  });

  it("displays terminal badge '已完成' when traceState is terminal", () => {
    render(
      <AnalysisTraceTerminal
        events={[event(1)]}
        selectedNodeId={null}
        onClearNodeFilter={() => {}}
        traceState="terminal"
      />,
    );
    expect(screen.getByText("已完成")).toBeInTheDocument();
  });

  it("displays waiting badge '等待执行' when traceState is not_started", () => {
    render(
      <AnalysisTraceTerminal
        events={[]}
        selectedNodeId={null}
        onClearNodeFilter={() => {}}
        traceState="not_started"
      />,
    );
    expect(screen.getByText("等待执行")).toBeInTheDocument();
  });

  it("source does not contain command input, retry, or deploy controls", () => {
    expect(SOURCE).not.toMatch(/onClick.*approve/);
    expect(SOURCE).not.toMatch(/onClick.*deploy/);
    expect(SOURCE).not.toMatch(/<button[^>]*retry/i);
    expect(SOURCE).not.toMatch(/type=["']submit["']/);
  });

  it("search input is type=text (not a command prompt)", () => {
    expect(SOURCE).toMatch(/type="text"/);
    expect(SOURCE).not.toMatch(/type=["']password["']/);
  });

  it("terminal log entries do not expose raw prompt/token/auth in bounded_redacted_args", () => {
    const safeEvent = event(1, { bounded_redacted_args: { tool: "events.get_window" } });
    render(
      <AnalysisTraceTerminal
        events={[safeEvent]}
        selectedNodeId={null}
        onClearNodeFilter={() => {}}
        traceState="active"
      />,
    );
    const log = screen.getByRole("log");
    const text = log.textContent ?? "";
    expect(text).not.toContain("prompt");
    expect(text).not.toContain("token");
    expect(text).not.toContain("Bearer");
    expect(text).not.toContain("auth");
  });
});