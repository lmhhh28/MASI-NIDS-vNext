"use client";

import { useState, useMemo, useRef, useEffect, useCallback } from "react";
import {
  CheckCircle,
  XCircle,
  Circle,
  AlertTriangle,
  Hourglass,
  Loader2,
  GitBranch,
  Zap,
  Wrench,
  Shield,
  Ban,
} from "lucide-react";

import { useI18n } from "@/lib/i18n";
import type { TranslationKey } from "@/lib/messages";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import type { TraceTopology, TraceEvent } from "@/types/api";
import { useAnalysisTraceV1 } from "@/hooks/use-analysis-trace-v1";

// Legacy sequence remains readable for pre-cutover audit rows.
export const LEGACY_WORKFLOW_NODE_SEQUENCE = [
  "alert_triage",
  "alert_triage_review",
  "intent_parser",
  "intent_review",
  "policy_planner",
  "policy_plan_review",
  "semantic_guard",
  "semantic_guard_review",
  "field_validator",
  "field_validation_review",
  "risk_assessor",
  "risk_review",
  "review_packet_builder",
  "review_packet_review",
  "human_review",
  "p4_apply",
  "p4_apply_review",
  "report_writer",
  "report_review",
] as const;

export const WORKFLOW_V2_NODE_SEQUENCE = [
  "triage",
  "intent",
  "triage_validate",
  "intent_validate",
  "plan",
  "semantic_guard",
  "field_validate",
  "risk",
  "safety_join",
  "full_report",
] as const;

type AgentStep = {
  id: string;
  workflow_id: string;
  node_name: string;
  stage: string;
  status: string;
  input_json: string;
  output_json: string;
  evidence_json: string;
  output?: Record<string, unknown>;
  created_at: string;
};

interface PipelineGraphProps {
  steps: AgentStep[];
  agentAnalysisContext?: {
    workflowId: string;
    revisionId: string;
  } | null;
}

type Tone = "passed" | "failed" | "skipped" | "running" | "waiting" | "draft" | "blocked";

function classifyStatus(status: string): Tone {
  const s = status.toLowerCase();
  if (s.includes("fail") || s === "rejected" || s === "review_failed") return "failed";
  if (s.includes("block")) return "blocked";
  if (s === "skipped" || s === "not_attempted") return "skipped";
  if (s === "waiting" || s === "needs_human_review") return "waiting";
  if (
    s === "passed" ||
    s === "reviewed" ||
    s === "current" ||
    s === "ok" ||
    s === "succeeded" ||
    s === "reused"
  ) return "passed";
  if (s === "running" || s === "in_progress" || s === "queued" || s === "admitted") return "running";
  if (s === "cancelled" || s === "superseded") return "skipped";
  return "draft";
}

function toneClasses(tone: Tone) {
  switch (tone) {
    case "passed":
      return "border-primary/40 bg-primary/10 text-primary";
    case "failed":
      return "border-destructive/40 bg-destructive/10 text-destructive";
    case "blocked":
      return "border-destructive/40 bg-destructive/5 text-destructive";
    case "waiting":
      return "border-warning/40 bg-warning/10 text-warning";
    case "running":
      return "border-chart-5/40 bg-chart-5/10 text-chart-5";
    case "skipped":
      return "border-border bg-secondary/40 text-muted-foreground";
    default:
      return "border-border bg-card text-muted-foreground";
  }
}

function ToneIcon({ tone }: { tone: Tone }) {
  switch (tone) {
    case "passed":
      return <CheckCircle className="h-3.5 w-3.5" aria-hidden />;
    case "failed":
    case "blocked":
      return <XCircle className="h-3.5 w-3.5" aria-hidden />;
    case "waiting":
      return <AlertTriangle className="h-3.5 w-3.5" aria-hidden />;
    case "running":
      return <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden />;
    case "skipped":
      return <Circle className="h-3.5 w-3.5" aria-hidden />;
    default:
      return <Hourglass className="h-3.5 w-3.5" aria-hidden />;
  }
}

function isReviewNode(name: string): boolean {
  return name.endsWith("_review") || name.includes("validate") || name.endsWith("_guard") || name.endsWith("_join");
}

function compactJson(value: unknown, fallback: string): string {
  if (value == null) return fallback;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function tryParse(json: string): unknown {
  try {
    return JSON.parse(json);
  } catch {
    return json;
  }
}

type LlmMetadata = {
  used?: boolean;
  fallback?: boolean;
  error?: string | null;
  disabled_reason?: string | null;
  llm_config_id?: string | null;
  prompt_version_id?: string | null;
  prompt_version?: number | string | null;
};

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function stepOutput(step: AgentStep): Record<string, unknown> | null {
  return recordValue(step.output) ?? recordValue(tryParse(step.output_json));
}

function stepLlmMetadata(step: AgentStep | null): LlmMetadata | null {
  if (!step) return null;
  const output = stepOutput(step);
  const payload = recordValue(output?.payload) ?? output;
  return recordValue(payload?.llm_metadata) as LlmMetadata | null;
}

type Translate = (key: TranslationKey, params?: Record<string, string | number | null | undefined>) => string;

function llmStatusLabel(metadata: LlmMetadata | null, t: Translate): string | null {
  if (!metadata) return null;
  if (metadata.fallback) return t("workflow.pipeline.llmFallback");
  if (metadata.used) return t("workflow.pipeline.llmUsed");
  return t("workflow.pipeline.llmDisabled");
}

function llmBadgeVariant(metadata: LlmMetadata | null): "default" | "secondary" | "destructive" | "outline" {
  if (!metadata) return "outline";
  if (metadata.fallback || metadata.error) return "destructive";
  if (metadata.used) return "default";
  return "secondary";
}

export function PipelineGraph({ steps, agentAnalysisContext }: PipelineGraphProps) {
  const { t } = useI18n();
  const [selected, setSelected] = useState<AgentStep | null>(null);
  const [innerSelectedNode, setInnerSelectedNode] = useState<string | null>(null);

  // Analysis trace hook: only active when the selected node is agent_analysis
  // and the trace context (workflow + revision IDs) is provided.
  const isAnalysisNode = selected?.node_name === "agent_analysis" && agentAnalysisContext !== null;
  const traceNodeRunId = selected?.id ?? null;
  const traceResult = useAnalysisTraceV1(
    isAnalysisNode && traceNodeRunId
      ? {
          workflowId: agentAnalysisContext!.workflowId,
          revisionId: agentAnalysisContext!.revisionId,
          nodeRunId: traceNodeRunId,
          sheetVisible: isAnalysisNode,
        }
      : null,
  );

  // Map node_name -> latest step.
  const latestByNode: Record<string, AgentStep> = {};
  for (const step of steps) {
    const prev = latestByNode[step.node_name];
    if (!prev || prev.created_at < step.created_at) {
      latestByNode[step.node_name] = step;
    }
  }

  if (steps.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-card p-8 text-center text-sm text-muted-foreground">
        <GitBranch className="mx-auto mb-2 h-6 w-6 opacity-40" aria-hidden />
        {t("workflow.pipeline.empty")}
      </div>
    );
  }

  const baseSequence = steps.some((step) => WORKFLOW_V2_NODE_SEQUENCE.includes(step.node_name as never))
    ? [...WORKFLOW_V2_NODE_SEQUENCE]
    : [...LEGACY_WORKFLOW_NODE_SEQUENCE];
  const extraNodes = Object.keys(latestByNode).filter((name) => !baseSequence.includes(name as never)).sort();
  const nodeSequence = [...baseSequence, ...extraNodes];

  return (
    <>
      <div className="min-w-0 rounded-xl border border-border bg-card p-3">
        <ol className="grid min-w-0 grid-cols-1 gap-1.5 md:grid-cols-2 lg:grid-cols-3">
          {nodeSequence.map((name, idx) => {
            const step = latestByNode[name];
            const tone: Tone = step ? classifyStatus(step.status) : "draft";
            const isReview = isReviewNode(name);
            const stageLabel = isReview ? t("workflow.pipeline.review") : t("workflow.pipeline.producer");
            const statusLabel = step ? step.status : t("workflow.pipeline.statusDraft");
            const llmMetadata = stepLlmMetadata(step ?? null);
            const llmLabel = llmStatusLabel(llmMetadata, t);
            const interactionClasses = step
              ? "cursor-pointer hover:bg-secondary/30 focus-visible:ring-3 focus-visible:ring-ring/40"
              : "cursor-default";
            return (
              <li key={name}>
                <button
                  type="button"
                  className={`flex w-full min-w-0 items-start gap-2 rounded-lg border p-2.5 text-left text-xs transition-colors duration-200 ${interactionClasses} ${toneClasses(tone)}`}
                  onClick={() => step && setSelected(step)}
                  aria-label={`${name} (${statusLabel})`}
                  disabled={!step}
                >
                  <span className="mt-0.5 shrink-0">
                    <ToneIcon tone={tone} />
                  </span>
                  <span className="flex-1 min-w-0">
                    <span className="block text-xs uppercase tracking-wide opacity-70">
                      {idx + 1}. {stageLabel}
                    </span>
                    <span className="block break-all font-mono text-xs leading-tight">
                      {name}
                    </span>
                    <span className="mt-0.5 block break-all text-xs opacity-80">{statusLabel}</span>
                    {llmLabel && (
                      <Badge variant={llmBadgeVariant(llmMetadata)} className="mt-1 h-4 max-w-full text-xs">
                        {llmLabel}
                      </Badge>
                    )}
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      </div>

      <Sheet open={!!selected} onOpenChange={(open) => !open && setSelected(null)}>
        <SheetContent side="right" className={`w-full overflow-y-auto ${isAnalysisNode ? "sm:max-w-3xl" : "sm:max-w-md"}`}>
          <SheetHeader>
            <SheetTitle className="pr-8 break-all font-mono text-sm">
              {selected ? t("workflow.pipeline.detailTitle", { name: selected.node_name }) : ""}
            </SheetTitle>
            <SheetDescription className="break-all">{selected?.created_at ?? ""}</SheetDescription>
          </SheetHeader>
          {selected && (
            <div className="mt-4 space-y-4">
              {/* Inner analysis graph + terminal log for agent_analysis nodes */}
              {isAnalysisNode && traceResult && (
                <div className="space-y-3">
                  <InnerAnalysisGraph
                    topology={traceResult.data?.topology ?? null}
                    events={traceResult.data?.events ?? []}
                    selectedNodeId={innerSelectedNode}
                    onNodeSelect={setInnerSelectedNode}
                    graphTopologySha256={traceResult.data?.graph_topology_sha256 ?? null}
                  />
                  <AnalysisTraceTerminal
                    events={traceResult.data?.events ?? []}
                    selectedNodeId={innerSelectedNode}
                    onClearNodeFilter={() => setInnerSelectedNode(null)}
                    traceState={traceResult.data?.trace_state ?? "not_started"}
                  />
                  {traceResult.isError && (
                    <p className="text-xs text-muted-foreground">读取失败</p>
                  )}
                </div>
              )}
              {stepLlmMetadata(selected) && (
                <div className="space-y-2 rounded-lg border border-border bg-secondary/20 p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs uppercase tracking-wide text-muted-foreground">
                      {t("workflow.pipeline.llm")}
                    </span>
                    <Badge variant={llmBadgeVariant(stepLlmMetadata(selected))} className="h-4 text-xs">
                      {llmStatusLabel(stepLlmMetadata(selected), t)}
                    </Badge>
                  </div>
                  <dl className="grid min-w-0 gap-1 text-xs md:grid-cols-2">
                    <MetadataField label={t("workflow.pipeline.llmConfig")} value={stepLlmMetadata(selected)?.llm_config_id} />
                    <MetadataField label={t("workflow.pipeline.promptVersion")} value={stepLlmMetadata(selected)?.prompt_version ?? stepLlmMetadata(selected)?.prompt_version_id} />
                    <MetadataField label={t("workflow.pipeline.disabledReason")} value={stepLlmMetadata(selected)?.disabled_reason} />
                    <MetadataField label={t("workflow.pipeline.error")} value={stepLlmMetadata(selected)?.error} />
                  </dl>
                </div>
              )}
              <div className="space-y-1.5">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  {t("workflow.pipeline.input")}
                </p>
                <pre className="max-h-48 max-w-full overflow-auto rounded-lg border border-border bg-secondary/20 p-2 text-xs font-mono whitespace-pre-wrap break-all">
                  {compactJson(tryParse(selected.input_json), t("common.none"))}
                </pre>
              </div>
              <div className="space-y-1.5">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  {t("workflow.pipeline.output")}
                </p>
                <pre className="max-h-64 max-w-full overflow-auto rounded-lg border border-border bg-secondary/20 p-2 text-xs font-mono whitespace-pre-wrap break-all">
                  {compactJson(tryParse(selected.output_json), t("common.none"))}
                </pre>
              </div>
            </div>
          )}
        </SheetContent>
      </Sheet>
    </>
  );
}

function MetadataField({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="break-all font-mono text-xs">{value == null || value === "" ? "—" : String(value)}</dd>
    </div>
  );
}

// ── Inner Analysis Graph (Phase 6 / Design r9 §9.8) ─────────────────────

type InnerNodeStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "partial"
  | "fallback"
  | "failed"
  | "timed_out"
  | "cancelled"
  | "skipped"
  | "outcome_unknown";

function classifyNodeStatus(events: TraceEvent[], nodeId: string): InnerNodeStatus {
  const nodeEvents = events.filter((e) => e.node_id === nodeId);
  if (nodeEvents.length === 0) return "queued";
  const last = nodeEvents[nodeEvents.length - 1];
  const phase = last.phase;
  if (phase === "completed") return "succeeded";
  if (phase === "started") return "running";
  if (phase === "skipped") return "skipped";
  if (phase === "failed") return "failed";
  if (phase === "timed_out") return "timed_out";
  if (phase === "cancelled") return "cancelled";
  if (phase === "fallback") return "fallback";
  if (phase === "partial") return "partial";
  if (phase === "late_result_dropped") return "cancelled";
  if (phase === "outcome_unknown") return "outcome_unknown";
  return "queued";
}

function innerToneClasses(status: InnerNodeStatus): string {
  switch (status) {
    case "succeeded":
      return "border-primary/40 bg-primary/10 text-primary";
    case "failed":
    case "timed_out":
      return "border-destructive/40 bg-destructive/10 text-destructive";
    case "running":
      return "border-chart-5/40 bg-chart-5/10 text-chart-5";
    case "skipped":
    case "cancelled":
      return "border-border bg-secondary/40 text-muted-foreground";
    case "partial":
    case "fallback":
      return "border-warning/40 bg-warning/10 text-warning";
    case "outcome_unknown":
      return "border-amber-500/40 bg-amber-500/5 text-amber-600 dark:text-amber-400";
    default:
      return "border-border bg-card text-muted-foreground";
  }
}

function innerToneIcon(status: InnerNodeStatus) {
  switch (status) {
    case "succeeded":
      return <CheckCircle className="h-3.5 w-3.5" aria-hidden />;
    case "failed":
    case "timed_out":
      return <XCircle className="h-3.5 w-3.5" aria-hidden />;
    case "running":
      return <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden />;
    case "skipped":
    case "cancelled":
      return <Circle className="h-3.5 w-3.5" aria-hidden />;
    case "partial":
    case "fallback":
      return <AlertTriangle className="h-3.5 w-3.5" aria-hidden />;
    case "outcome_unknown":
      return <Hourglass className="h-3.5 w-3.5" aria-hidden />;
    default:
      return <Hourglass className="h-3.5 w-3.5" aria-hidden />;
  }
}

function nodeKindIcon(kind: string) {
  switch (kind) {
    case "llm":
      return <Zap className="h-3 w-3" aria-hidden />;
    case "mcp_round":
      return <Wrench className="h-3 w-3" aria-hidden />;
    case "validator":
    case "guard":
      return <Shield className="h-3 w-3" aria-hidden />;
    case "fallback":
      return <Ban className="h-3 w-3" aria-hidden />;
    default:
      return null;
  }
}

function statusText(status: InnerNodeStatus): string {
  const map: Record<InnerNodeStatus, string> = {
    queued: "等待",
    running: "执行中",
    succeeded: "完成",
    partial: "部分",
    fallback: "降级",
    failed: "失败",
    timed_out: "超时",
    cancelled: "取消",
    skipped: "跳过",
    outcome_unknown: "未确认",
  };
  return map[status];
}

function directNeighbors(topology: TraceTopology, nodeId: string, direction: "up" | "down"): Set<string> {
  const result = new Set<string>();
  for (const edge of topology.edges) {
    if (direction === "up" && edge.to_node_id === nodeId) {
      result.add(edge.from_node_id);
    } else if (direction === "down" && edge.from_node_id === nodeId) {
      result.add(edge.to_node_id);
    }
  }
  return result;
}

export interface InnerAnalysisGraphProps {
  topology: TraceTopology | null;
  events: TraceEvent[];
  selectedNodeId: string | null;
  onNodeSelect: (nodeId: string | null) => void;
  graphTopologySha256: string | null;
}

export function InnerAnalysisGraph({
  topology,
  events,
  selectedNodeId,
  onNodeSelect,
  graphTopologySha256,
}: InnerAnalysisGraphProps) {
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);

  const nodes = useMemo(() => topology?.nodes ?? [], [topology]);
  const highlightedSet = useMemo(() => {
    if (!topology) return new Set<string>();
    const target = selectedNodeId ?? hoveredNode;
    if (!target) return new Set<string>();
    const result = new Set<string>([target]);
    result.union(directNeighbors(topology, target, "up"));
    result.union(directNeighbors(topology, target, "down"));
    return result;
  }, [topology, selectedNodeId, hoveredNode]);

  if (!topology || nodes.length === 0) {
    return (
      <div className="rounded-md border border-border bg-card p-4 text-center text-sm text-muted-foreground">
        无内层调用图
      </div>
    );
  }

  return (
    <div className="rounded-md border border-border bg-card p-3">
      {graphTopologySha256 && (
        <div className="mb-2 text-xs text-muted-foreground" aria-hidden="true">
          拓扑哈希 {graphTopologySha256.slice(0, 12)}…
        </div>
      )}
      <div className="grid min-w-0 grid-cols-1 gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
        {nodes.map((node) => {
          const status = classifyNodeStatus(events, node.node_id);
          const isHighlighted = highlightedSet.has(node.node_id);
          const isSelected = selectedNodeId === node.node_id;
          return (
            <button
              key={node.node_id}
              type="button"
              className={`flex min-w-0 items-start gap-1.5 rounded-lg border p-2 text-left text-xs transition-all duration-150 motion-reduce:transition-none ${
                innerToneClasses(status)
              } ${isSelected ? "ring-2 ring-ring/50" : ""} ${
                isHighlighted && !isSelected ? "ring-1 ring-ring/20" : ""
              } ${status === "queued" ? "opacity-60" : ""}`}
              onClick={() => onNodeSelect(isSelected ? null : node.node_id)}
              onMouseEnter={() => setHoveredNode(node.node_id)}
              onMouseLeave={() => setHoveredNode(null)}
              onFocus={() => setHoveredNode(node.node_id)}
              onBlur={() => setHoveredNode(null)}
              aria-label={`${node.display_key} (${statusText(status)})`}
            >
              <span className="mt-0.5 shrink-0">{innerToneIcon(status)}</span>
              <span className="flex-1 min-w-0">
                <span className="flex items-center gap-1">
                  {nodeKindIcon(node.kind)}
                  <span className="break-all text-xs leading-tight">{node.display_key}</span>
                </span>
                <span className="mt-0.5 block text-xs opacity-80">{statusText(status)}</span>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── Analysis Trace Terminal (Phase 6 / Design r9 §7) ─────────────────────

export interface AnalysisTraceTerminalProps {
  events: TraceEvent[];
  selectedNodeId: string | null;
  onClearNodeFilter: () => void;
  traceState: string;
}

export function AnalysisTraceTerminal({
  events,
  selectedNodeId,
  onClearNodeFilter,
  traceState,
}: AnalysisTraceTerminalProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [autoFollow, setAutoFollow] = useState(true);
  const [newCount, setNewCount] = useState(0);
  const [search, setSearch] = useState("");

  const filteredEvents = useMemo(() => {
    let result = events;
    if (selectedNodeId) {
      result = result.filter((e) => e.node_id === selectedNodeId);
    }
    if (search.trim()) {
      const q = search.toLowerCase();
      result = result.filter(
        (e) =>
          e.message_code.toLowerCase().includes(q) ||
          e.event_kind.toLowerCase().includes(q) ||
          String(e.elapsed_ms ?? "").includes(q),
      );
    }
    return result;
  }, [events, selectedNodeId, search]);

  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, []);

  useEffect(() => {
    if (autoFollow) {
      scrollToBottom();
    } else {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setNewCount((c) => c + 1);
    }
  }, [filteredEvents.length, autoFollow, scrollToBottom]);

  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 4;
    setAutoFollow(atBottom);
    if (atBottom) setNewCount(0);
  }, []);

  const traceStateLabel: Record<string, string> = {
    not_started: "等待执行",
    active: "分析中",
    terminal: "已完成",
    expired: "记录已过期",
    unavailable: "不可用",
  };

  return (
    <div className="rounded-md border border-border bg-muted/20">
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-1.5">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className="font-mono uppercase">调用日志</span>
          <Badge variant="outline" className="h-4 text-xs">{traceStateLabel[traceState] ?? traceState}</Badge>
          {selectedNodeId && (
            <button
              type="button"
              onClick={onClearNodeFilter}
              className="text-xs text-primary hover:underline"
            >
              全部
            </button>
          )}
        </div>
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索…"
          className="h-6 w-32 rounded border border-border bg-card px-2 text-xs focus:outline-none focus:ring-1 focus:ring-ring/30"
          aria-label="搜索日志"
        />
      </div>
      <div
        ref={containerRef}
        role="log"
        aria-label="Agent 分析调用日志"
        aria-live="polite"
        tabIndex={0}
        onScroll={handleScroll}
        className="max-h-72 min-h-48 overflow-y-auto px-2 py-1.5 font-mono text-xs leading-relaxed focus:outline-none focus-visible:ring-1 focus-visible:ring-ring/30"
      >
        {filteredEvents.length === 0 ? (
          <div className="flex h-24 items-center justify-center text-muted-foreground">无记录</div>
        ) : (
          filteredEvents.map((event) => (
            <TraceLogLine key={event.event_id} event={event} />
          ))
        )}
      </div>
      {!autoFollow && newCount > 0 && (
        <button
          type="button"
          onClick={() => {
            setAutoFollow(true);
            scrollToBottom();
          }}
          className="absolute bottom-2 right-2 rounded-md border border-border bg-card px-2 py-1 text-xs shadow-sm hover:bg-secondary/30"
        >
          有 {newCount} 条新记录
        </button>
      )}
    </div>
  );
}

function TraceLogLine({ event }: { event: TraceEvent }) {
  const time = event.occurred_at.slice(11, 23);
  const kindLabel: Record<string, string> = {
    graph: "[图]",
    node: "[节点]",
    deterministic: "[确定性]",
    llm: "[LLM]",
    mcp: "[MCP]",
    validator: "[校验]",
    fallback: "[降级]",
  };
  const label = kindLabel[event.event_kind] ?? `[${event.event_kind}]`;
  const phaseLabel: Record<string, string> = {
    started: "开始",
    completed: "完成",
    skipped: "跳过",
    failed: "失败",
    timed_out: "超时",
    cancelled: "取消",
    partial: "部分",
    fallback: "降级",
    late_result_dropped: "丢弃",
    outcome_unknown: "未确认",
  };
  const phase = phaseLabel[event.phase] ?? event.phase;
  const elapsed = event.elapsed_ms != null ? `${event.elapsed_ms}ms` : "";
  const hash = event.response_sha256 ? `hash=${event.response_sha256.slice(0, 8)}…` : "";

  return (
    <div className="whitespace-nowrap text-muted-foreground hover:text-foreground">
      <span className="text-muted-foreground/70">{time}</span>{" "}
      <span className="text-primary/80">{label}</span>{" "}
      <span>{event.message_code}</span>{" "}
      {elapsed && <span className="text-muted-foreground/80">{elapsed}</span>}{" "}
      <span className="text-foreground/80">{phase}</span>{" "}
      {hash && <span className="text-muted-foreground/60">{hash}</span>}
    </div>
  );
}
