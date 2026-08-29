"use client";

import { ShieldCheck, Database, Network, Table as TableIcon, BookOpen, FileText } from "lucide-react";

import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { DirectionalEvidenceReviewActions } from "@/components/evidence/directional-evidence-review-actions";

interface ReviewPacketShape {
  artifact_ids?: string[];
  directional_evidence_hashes?: string[];
  p4info_hash?: string | null;
  table_schema_hash?: string | null;
  risk_policy_version?: number | string | null;
  template_name?: string | null;
}

interface ArtifactShape {
  id?: string;
  artifact_type?: string;
  payload?: Record<string, unknown>;
  evidence_refs?: Array<{
    evidence_type?: string;
    object_id?: string;
    evidence_hash?: string;
    status?: string;
  }>;
}

interface EvidenceChainProps {
  reviewPacket: ReviewPacketShape | null | undefined;
  artifacts: ArtifactShape[] | undefined;
}

interface EvidenceItem {
  type: "event" | "directional" | "p4info" | "schema" | "policy" | "template" | "artifact";
  label: string;
  hash: string | null;
  status?: string | null;
  objectId?: string | null;
}

function truncateHash(hash: string | null | undefined): string {
  if (!hash) return "—";
  return hash.length > 12 ? `${hash.slice(0, 6)}…${hash.slice(-4)}` : hash;
}

function evidenceIcon(type: EvidenceItem["type"]) {
  switch (type) {
    case "event":
      return <FileText className="h-3.5 w-3.5" aria-hidden />;
    case "directional":
      return <Network className="h-3.5 w-3.5" aria-hidden />;
    case "p4info":
      return <Database className="h-3.5 w-3.5" aria-hidden />;
    case "schema":
      return <TableIcon className="h-3.5 w-3.5" aria-hidden />;
    case "policy":
      return <ShieldCheck className="h-3.5 w-3.5" aria-hidden />;
    case "template":
      return <BookOpen className="h-3.5 w-3.5" aria-hidden />;
    default:
      return <FileText className="h-3.5 w-3.5" aria-hidden />;
  }
}

export function EvidenceChain({ reviewPacket, artifacts }: EvidenceChainProps) {
  const { t } = useI18n();
  const items: EvidenceItem[] = [];

  if (reviewPacket?.directional_evidence_hashes?.length) {
    for (const hash of reviewPacket.directional_evidence_hashes) {
      items.push({
        type: "directional",
        label: t("workflow.evidence.directional"),
        hash,
      });
    }
  }
  if (reviewPacket?.p4info_hash) {
    items.push({
      type: "p4info",
      label: t("workflow.evidence.p4info"),
      hash: reviewPacket.p4info_hash,
    });
  }
  if (reviewPacket?.table_schema_hash) {
    items.push({
      type: "schema",
      label: t("workflow.evidence.schema"),
      hash: reviewPacket.table_schema_hash,
    });
  }
  if (reviewPacket?.risk_policy_version != null) {
    items.push({
      type: "policy",
      label: t("workflow.evidence.riskPolicy"),
      hash: `v${reviewPacket.risk_policy_version}`,
    });
  }
  if (reviewPacket?.template_name) {
    items.push({
      type: "template",
      label: t("workflow.evidence.template"),
      hash: reviewPacket.template_name,
    });
  }

  for (const artifact of artifacts ?? []) {
    for (const ref of artifact.evidence_refs ?? []) {
      const type = ref.evidence_type;
      if (!type) continue;
      if (type === "directional_evidence") {
        items.push({
          type: "directional",
          label: t("workflow.evidence.directional"),
          hash: ref.evidence_hash ?? null,
          status: ref.status,
          objectId: ref.object_id,
        });
      } else if (type === "nids_event") {
        items.push({
          type: "event",
          label: t("workflow.evidence.event"),
          hash: ref.evidence_hash ?? null,
          status: ref.status,
          objectId: ref.object_id,
        });
      }
    }
  }

  // Deduplicate by (type, hash, objectId).
  const seen = new Set<string>();
  const dedup: EvidenceItem[] = [];
  for (const it of items) {
    const key = `${it.type}|${it.hash ?? ""}|${it.objectId ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    dedup.push(it);
  }

  if (dedup.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-card p-8 text-center text-sm text-muted-foreground">
        {t("workflow.evidence.empty")}
      </div>
    );
  }

  return (
    <div className="min-w-0 rounded-xl border border-border bg-card p-4">
      <p className="mb-3 text-xs font-medium text-muted-foreground">
        {t("workflow.evidence.title")}
      </p>
      <div className="flex min-w-0 flex-wrap gap-2">
        {dedup.map((item, idx) => (
          <div
            key={`${item.type}-${idx}`}
            className="flex min-w-0 max-w-full items-center gap-2 rounded-lg border border-border bg-secondary/20 px-2.5 py-1.5"
            title={item.hash ?? undefined}
          >
            <span className="text-muted-foreground">{evidenceIcon(item.type)}</span>
            <div className="flex min-w-0 flex-col leading-tight">
              <span className="text-xs uppercase tracking-wide text-muted-foreground">
                {item.label}
              </span>
              <span className="break-all font-mono text-xs">{truncateHash(item.hash)}</span>
            </div>
            {item.status && (
              <Badge
                variant={item.status === "reviewed" || item.status === "resolved" ? "default" : "secondary"}
                className="text-xs"
              >
                {item.status}
              </Badge>
            )}
            {item.type === "directional" && item.objectId && (
              <DirectionalEvidenceReviewActions
                evidenceId={item.objectId}
                status={item.status ?? null}
              />
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
