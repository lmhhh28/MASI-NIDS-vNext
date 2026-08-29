"use client";

import { CheckCircle, XCircle, Circle, AlertTriangle, Clock } from "lucide-react";

import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";

interface AgentStep {
  id: string;
  workflow_id: string;
  node_name: string;
  stage: string;
  status: string;
  output_json?: string;
  output?: Record<string, unknown>;
  created_at: string;
}

interface StepTimelineProps {
  steps: AgentStep[];
}

function statusIcon(status: string) {
  const s = status.toLowerCase();
  if (s.includes("fail") || s.includes("rejected")) {
    return <XCircle className="h-3.5 w-3.5 text-destructive" aria-hidden />;
  }
  if (s.includes("block")) {
    return <XCircle className="h-3.5 w-3.5 text-destructive" aria-hidden />;
  }
  if (s === "passed" || s === "reviewed" || s === "current") {
    return <CheckCircle className="h-3.5 w-3.5 text-primary" aria-hidden />;
  }
  if (s === "skipped" || s === "not_attempted") {
    return <Circle className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />;
  }
  if (s === "waiting" || s === "needs_human_review") {
    return <AlertTriangle className="h-3.5 w-3.5 text-warning" aria-hidden />;
  }
  return <Clock className="h-3.5 w-3.5 text-chart-5" aria-hidden />;
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function tryParse(json: string | undefined): unknown {
  if (!json) return null;
  try {
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function llmMetadata(step: AgentStep): Record<string, unknown> | null {
  const output = recordValue(step.output) ?? recordValue(tryParse(step.output_json));
  const payload = recordValue(output?.payload);
  return recordValue(payload?.llm_metadata);
}

export function StepTimeline({ steps }: StepTimelineProps) {
  const { t, formatDateTime } = useI18n();
  const sorted = [...steps].sort((a, b) => a.created_at.localeCompare(b.created_at));

  if (sorted.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-card p-8 text-center text-sm text-muted-foreground">
        {t("workflow.timeline.empty")}
      </div>
    );
  }

  return (
    <ol className="relative ml-3 min-w-0 space-y-3 border-l border-border">
      {sorted.map((step) => (
        <TimelineItem key={step.id} step={step} />
      ))}
    </ol>
  );

  function TimelineItem({ step }: { step: AgentStep }) {
    const metadata = llmMetadata(step);
    const fallback = Boolean(metadata?.fallback);
    const used = Boolean(metadata?.used);
    const label = fallback
      ? t("workflow.pipeline.llmFallback")
      : used
        ? t("workflow.pipeline.llmUsed")
        : metadata
          ? t("workflow.pipeline.llmDisabled")
          : null;
    return (
      <li className="ml-4 min-w-0">
        <span className="absolute -left-1.5 mt-1 flex h-3 w-3 items-center justify-center rounded-full bg-card ring-2 ring-border" aria-hidden>
          {statusIcon(step.status)}
        </span>
        <div className="min-w-0 rounded-lg border border-border bg-card p-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="break-all font-mono text-xs font-medium">{step.node_name}</span>
            <span className="text-xs uppercase tracking-wide text-muted-foreground">
              {step.stage}
            </span>
            {label && (
              <Badge variant={fallback ? "destructive" : used ? "default" : "secondary"} className="h-4 text-xs">
                {label}
              </Badge>
            )}
            <span className="ml-auto break-all text-xs font-mono text-muted-foreground">
              {formatDateTime(step.created_at, {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
                fractionalSecondDigits: 3,
              })}
            </span>
          </div>
          <p className="mt-1 break-all text-xs text-muted-foreground">{step.status}</p>
        </div>
      </li>
    );
  }
}
