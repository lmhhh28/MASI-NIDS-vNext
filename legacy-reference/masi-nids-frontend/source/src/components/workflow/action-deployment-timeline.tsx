"use client";

import { CheckCircle, Circle, Clock, XCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { actionTimeline } from "@/lib/workflow-action";
import type { WorkflowSummary } from "@/types/api";

export function ActionDeploymentTimeline({ summary }: { summary: WorkflowSummary }) {
  const items = actionTimeline(summary);
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium">Action deployment</span>
        {summary.current_deployment_intent_id ? (
          <span className="break-all font-mono text-xs text-muted-foreground">{summary.current_deployment_intent_id}</span>
        ) : null}
      </div>
      <ol className="grid gap-3 md:grid-cols-3">
        {items.map((item) => (
          <li key={item.id} className="min-w-0 rounded-md border border-border/70 p-3">
            <div className="flex items-center gap-2">
              {iconFor(item.status)}
              <span className="min-w-0 truncate font-mono text-xs">{item.label}</span>
            </div>
            <Badge variant={badgeFor(item.status)} className="mt-2 text-xs">
              {item.status}
            </Badge>
          </li>
        ))}
      </ol>
    </div>
  );
}

function iconFor(status: string) {
  if (status === "applied" || status === "succeeded" || status === "prepared") {
    return <CheckCircle className="h-4 w-4 text-primary" aria-hidden="true" />;
  }
  if (status === "failed" || status === "p4_write_failed" || status === "rejected") {
    return <XCircle className="h-4 w-4 text-destructive" aria-hidden="true" />;
  }
  if (status === "not_created" || status === "not_submitted") {
    return <Circle className="h-4 w-4 text-muted-foreground" aria-hidden="true" />;
  }
  return <Clock className="h-4 w-4 text-warning" aria-hidden="true" />;
}

function badgeFor(status: string) {
  if (status === "applied" || status === "succeeded" || status === "prepared") return "success";
  if (status === "failed" || status === "p4_write_failed" || status === "rejected") return "destructive";
  return "secondary";
}
