"use client";

import Link from "next/link";
import { Ban, Loader2, ShieldAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useFlowEvidenceBlockAdmission } from "@/hooks/use-flow-evidence-v3";
import { useI18n } from "@/lib/i18n";
import type { FlowEvidenceV3Summary } from "@/types/api";

interface FlowEvidenceActionPanelProps {
  items: FlowEvidenceV3Summary[];
}

export function FlowEvidenceActionPanel({ items }: FlowEvidenceActionPanelProps) {
  if (items.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldAlert className="h-4 w-4" aria-hidden="true" />
            FlowEvidence action
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">No active FlowEvidence candidates.</CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <ShieldAlert className="h-4 w-4" aria-hidden="true" />
          FlowEvidence action
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {items.map((item) => (
          <FlowEvidenceActionRow key={item.flow_evidence_id} item={item} />
        ))}
      </CardContent>
    </Card>
  );
}

function FlowEvidenceActionRow({ item }: { item: FlowEvidenceV3Summary }) {
  const { locale, formatNumber } = useI18n();
  const block = useFlowEvidenceBlockAdmission(item.flow_evidence_id);
  const workflowId = block.data?.id;
  return (
    <div className="grid gap-3 rounded-lg border border-border p-3 md:grid-cols-[1fr_auto] md:items-center">
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={item.status === "active" ? "success" : "secondary"}>{item.status}</Badge>
          <span className="break-all font-mono text-xs">{item.flow_evidence_id}</span>
        </div>
        <div className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
          <Fact label="flow" value={`${item.observed_src_ip}:${item.observed_src_port} -> ${item.observed_dst_ip}:${item.observed_dst_port}`} />
          <Fact label="protocol" value={item.observed_protocol} />
          <Fact label="samples" value={formatNumber(item.sample_count)} />
          <Fact label="confidence" value={formatNumber(item.confidence, { style: "percent", maximumFractionDigits: 1 })} />
        </div>
        {block.isError ? <p className="text-xs text-destructive">{block.error.message}</p> : null}
      </div>
      <div className="flex flex-wrap items-center gap-2 md:justify-end">
        {workflowId ? (
          <Link href={`/workflows/${workflowId}`} prefetch={false} className={buttonVariants({ variant: "outline", size: "sm" })}>
            Open workflow
          </Link>
        ) : (
          <Button
            type="button"
            size="sm"
            disabled={item.status !== "active" || block.isPending}
            onClick={() => block.mutate({ locale })}
          >
            {block.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Ban aria-hidden="true" />}
            Block flow
          </Button>
        )}
      </div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="min-w-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-all font-mono">{value == null || value === "" ? "-" : String(value)}</dd>
    </div>
  );
}
