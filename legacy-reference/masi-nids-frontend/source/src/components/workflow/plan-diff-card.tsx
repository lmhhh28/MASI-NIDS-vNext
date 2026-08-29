"use client";

import { Network, Eye } from "lucide-react";

import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";

interface PlanShape {
  id?: string;
  table_name?: string;
  action_name?: string;
  match_fields?: Record<string, unknown>;
  action_params?: Record<string, unknown>;
  ttl_seconds?: number | null;
  priority?: number | null;
  schema_hash?: string | null;
  p4info_hash?: string | null;
  plan_hash?: string | null;
  directional_evidence_id?: string | null;
}

interface PlanDiffCardProps {
  plan: PlanShape | null | undefined;
  templateName?: string | null;
}

export function PlanDiffCard({ plan, templateName }: PlanDiffCardProps) {
  const { t } = useI18n();
  if (!plan || !plan.table_name) {
    if (templateName === "inspect_only") {
      return (
        <div className="rounded-xl border border-primary/30 bg-primary/5 p-4">
          <div className="flex items-center gap-2">
            <Eye className="h-4 w-4 text-primary" aria-hidden />
            <p className="text-xs font-medium">{t("workflow.plan.title")}</p>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            {t("workflow.plan.inspectOnly")}
          </p>
        </div>
      );
    }
    return (
      <div className="rounded-xl border border-border bg-card p-4 text-xs text-muted-foreground">
        {t("workflow.plan.empty")}
      </div>
    );
  }

  const matchEntries = Object.entries(plan.match_fields ?? {});
  return (
    <div className="min-w-0 rounded-xl border border-border bg-card p-4">
      <div className="mb-3 flex items-center gap-2">
        <Network className="h-4 w-4 text-primary" aria-hidden />
        <p className="text-xs font-medium">{t("workflow.plan.title")}</p>
      </div>
      <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-4">
        <PlanField label={t("workflow.plan.table")} value={plan.table_name} mono />
        <PlanField label={t("workflow.plan.action")} value={plan.action_name ?? "—"} mono />
        <PlanField
          label={t("workflow.plan.ttl")}
          value={plan.ttl_seconds != null ? String(plan.ttl_seconds) : "—"}
        />
        <PlanField
          label={t("workflow.plan.priority")}
          value={plan.priority != null ? String(plan.priority) : "—"}
        />
      </div>
      {matchEntries.length > 0 && (
        <div className="mt-3">
          <p className="text-xs uppercase tracking-wide text-muted-foreground">
            {t("workflow.plan.fields")}
          </p>
          <div className="mt-1.5 flex min-w-0 flex-wrap gap-1.5">
            {matchEntries.map(([key, value]) => (
              <Badge
                key={key}
                variant="outline"
                className="h-auto max-w-full justify-start whitespace-normal py-1 text-left text-xs font-mono leading-snug"
                title={`${key}=${String(value)}`}
              >
                <span className="min-w-0 break-all">
                  {key}={String(value)}
                </span>
              </Badge>
            ))}
          </div>
        </div>
      )}
      <div className="mt-3 grid min-w-0 gap-2 md:grid-cols-3">
        <PlanField label="schema" value={plan.schema_hash ?? "—"} mono />
        <PlanField label="p4info" value={plan.p4info_hash ?? "—"} mono />
        <PlanField label="plan hash" value={plan.plan_hash ?? "—"} mono />
      </div>
      {plan.directional_evidence_id && (
        <div className="mt-2">
          <PlanField label={t("workflow.evidence.directional")} value={plan.directional_evidence_id} mono />
        </div>
      )}
    </div>
  );
}

function PlanField({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className={`mt-0.5 break-all text-xs ${mono ? "font-mono" : ""}`} title={value}>
        {value}
      </p>
    </div>
  );
}
