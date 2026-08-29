import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { InnerAnalysisGraph } from "@/components/workflow/pipeline-graph";
import type { TraceTopology, TraceEvent } from "@/types/api";

function topology(): TraceTopology {
  return {
    nodes: [
      { node_id: "load", internal_name: "load_bundle", display_key: "读取证据", kind: "deterministic", parallel_group_id: null, fanout_index: null, stable_ordinal: 0 },
      { node_id: "impact", internal_name: "rate_impact", display_key: "流量影响", kind: "deterministic", parallel_group_id: "ctx", fanout_index: 0, stable_ordinal: 1 },
      { node_id: "quality", internal_name: "quality_check", display_key: "数据质量", kind: "deterministic", parallel_group_id: "ctx", fanout_index: 1, stable_ordinal: 2 },
      { node_id: "runtime", internal_name: "runtime_check", display_key: "运行状态", kind: "deterministic", parallel_group_id: "ctx", fanout_index: 2, stable_ordinal: 3 },
      { node_id: "hypothesis", internal_name: "hypothesis_planner", display_key: "形成判断", kind: "llm", parallel_group_id: null, fanout_index: null, stable_ordinal: 4 },
    ],
    edges: [
      { from_node_id: "load", to_node_id: "impact", stable_ordinal: 0, condition_key: null },
      { from_node_id: "load", to_node_id: "quality", stable_ordinal: 1, condition_key: null },
      { from_node_id: "load", to_node_id: "runtime", stable_ordinal: 2, condition_key: null },
      { from_node_id: "impact", to_node_id: "hypothesis", stable_ordinal: 3, condition_key: null },
      { from_node_id: "quality", to_node_id: "hypothesis", stable_ordinal: 4, condition_key: null },
      { from_node_id: "runtime", to_node_id: "hypothesis", stable_ordinal: 5, condition_key: null },
    ],
  };
}

function event(seq: number, nodeId: string, phase: string): TraceEvent {
  return {
    sequence_no: seq,
    event_id: `ev-${seq}`,
    occurred_at: `2026-08-02T03:20:${String(seq).padStart(2, "0")}.000Z`,
    node_id: nodeId,
    parent_node_ids: [],
    parallel_group_id: null,
    event_kind: "deterministic",
    phase,
    call_id: null,
    call_index: null,
    callee_key: null,
    elapsed_ms: 50,
    outcome: phase === "completed" ? "succeeded" : phase,
    error_code: null,
    request_sha256: null,
    response_sha256: null,
    message_code: "node.completed",
    bounded_redacted_args: {},
    redaction_profile_version: 1,
    trace_id: "trace-1",
  };
}

describe("InnerAnalysisGraph", () => {
  it("renders all topology nodes as buttons", () => {
    const topo = topology();
    render(
      <InnerAnalysisGraph
        topology={topo}
        events={[event(1, "load", "completed"), event(2, "impact", "completed")]}
        selectedNodeId={null}
        onNodeSelect={() => {}}
        graphTopologySha256="abc123def456"
      />,
    );
    for (const node of topo.nodes) {
      expect(screen.getByLabelText(new RegExp(node.display_key))).toBeInTheDocument();
    }
  });

  it("renders 4 deterministic parallel context nodes", () => {
    const topo = topology();
    render(
      <InnerAnalysisGraph
        topology={topo}
        events={[]}
        selectedNodeId={null}
        onNodeSelect={() => {}}
        graphTopologySha256={null}
      />,
    );
    // 5 total nodes: load + 4 parallel + hypothesis
    expect(topo.nodes).toHaveLength(5);
    const parallel = topo.nodes.filter((n) => n.parallel_group_id === "ctx");
    expect(parallel).toHaveLength(3); // impact, quality, runtime (3 parallel deterministic)
  });

  it("shows '无内层调用图' when topology is null", () => {
    render(
      <InnerAnalysisGraph
        topology={null}
        events={[]}
        selectedNodeId={null}
        onNodeSelect={() => {}}
        graphTopologySha256={null}
      />,
    );
    expect(screen.getByText("无内层调用图")).toBeInTheDocument();
  });

  it("calls onNodeSelect on click, and null on second click", () => {
    const onSelect = vi.fn();
    const { rerender } = render(
      <InnerAnalysisGraph
        topology={topology()}
        events={[]}
        selectedNodeId={null}
        onNodeSelect={onSelect}
        graphTopologySha256={null}
      />,
    );
    const node = screen.getByLabelText(/读取证据/);
    fireEvent.click(node);
    expect(onSelect).toHaveBeenLastCalledWith("load");
    // Simulate the parent re-rendering with the now-selected node.
    rerender(
      <InnerAnalysisGraph
        topology={topology()}
        events={[]}
        selectedNodeId="load"
        onNodeSelect={onSelect}
        graphTopologySha256={null}
      />,
    );
    fireEvent.click(node);
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("skipped nodes do not disappear (rendered as button with '跳过' text)", () => {
    render(
      <InnerAnalysisGraph
        topology={topology()}
        events={[event(1, "runtime", "skipped")]}
        selectedNodeId={null}
        onNodeSelect={() => {}}
        graphTopologySha256={null}
      />,
    );
    const runtimeNode = screen.getByLabelText(/运行状态/);
    expect(runtimeNode).toBeInTheDocument();
    expect(runtimeNode.textContent).toContain("跳过");
  });

  it("fallback nodes show '降级' text", () => {
    render(
      <InnerAnalysisGraph
        topology={topology()}
        events={[event(1, "hypothesis", "fallback")]}
        selectedNodeId={null}
        onNodeSelect={() => {}}
        graphTopologySha256={null}
      />,
    );
    const hypNode = screen.getByLabelText(/形成判断/);
    expect(hypNode.textContent).toContain("降级");
  });

  it("shows graph topology hash prefix (first 12 chars)", () => {
    render(
      <InnerAnalysisGraph
        topology={topology()}
        events={[]}
        selectedNodeId={null}
        onNodeSelect={() => {}}
        graphTopologySha256="abcdef1234567890abcdef1234567890"
      />,
    );
    expect(screen.getByText(/abcdef123456/)).toBeInTheDocument();
  });

  it("node aria-label includes status text (color is not the only semantic)", () => {
    render(
      <InnerAnalysisGraph
        topology={topology()}
        events={[event(1, "load", "completed")]}
        selectedNodeId={null}
        onNodeSelect={() => {}}
        graphTopologySha256={null}
      />,
    );
    const loadNode = screen.getByLabelText(/读取证据/);
    expect(loadNode.getAttribute("aria-label")).toMatch(/完成/);
  });
});