"use client";

import { ShieldAlert, ShieldCheck, ShieldQuestion, AlertTriangle } from "lucide-react";

import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";

interface RiskShape {
  risk_level?: string;
  requires_human?: boolean;
  auto_apply_eligible?: boolean;
  hard_stop_codes?: string[];
  reasons?: string[];
  policy_version?: number | null;
  ttl_seconds?: number | null;
  active_rule_count?: number;
  active_rule_cap?: number | null;
}

interface RiskCardProps {
  risk: RiskShape | null | undefined;
}

function riskTone(level?: string) {
  switch ((level ?? "").toLowerCase()) {
    case "low":
      return { className: "border-primary/30 bg-primary/10 text-primary", icon: <ShieldCheck className="h-4 w-4" aria-hidden /> };
    case "medium":
      return { className: "border-warning/30 bg-warning/10 text-warning", icon: <ShieldAlert className="h-4 w-4" aria-hidden /> };
    case "high":
    case "prohibited":
      return { className: "border-destructive/30 bg-destructive/10 text-destructive", icon: <AlertTriangle className="h-4 w-4" aria-hidden /> };
    default:
      return { className: "border-border bg-secondary/30 text-muted-foreground", icon: <ShieldQuestion className="h-4 w-4" aria-hidden /> };
  }
}

export function RiskCard({ risk }: RiskCardProps) {
  const { t } = useI18n();
  if (!risk) {
    return <div className="rounded-xl border border-border bg-card p-4 text-xs text-muted-foreground">—</div>;
  }
  const tone = riskTone(risk.risk_level);
  const hardStops = risk.hard_stop_codes ?? [];
  const reasons = risk.reasons ?? [];

  return (
    <div className={`min-w-0 rounded-xl border p-4 ${tone.className}`}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        {tone.icon}
        <p className="text-xs font-medium">{t("workflow.risk.title")}</p>
        <Badge variant="outline" className="ml-auto max-w-full text-xs uppercase font-mono">
          {risk.risk_level ?? "—"}
        </Badge>
      </div>
      <div className="mt-3 grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2 md:grid-cols-4">
        <RiskField label={t("workflow.risk.requiresHuman")} value={String(!!risk.requires_human)} />
        <RiskField label={t("workflow.risk.autoEligible")} value={String(!!risk.auto_apply_eligible)} />
        <RiskField
          label={t("workflow.plan.ttl")}
          value={risk.ttl_seconds != null ? String(risk.ttl_seconds) : "—"}
        />
        <RiskField
          label={t("workflow.risk.policyVersion")}
          value={risk.policy_version != null ? `v${risk.policy_version}` : "—"}
        />
      </div>
      {hardStops.length > 0 && (
        <div className="mt-3">
          <p className="text-xs uppercase tracking-wide opacity-70">
            {t("workflow.risk.hardStops")}
          </p>
          <div className="mt-1 flex min-w-0 flex-wrap gap-1.5">
            {hardStops.map((code) => (
              <Badge
                key={code}
                variant="destructive"
                className="h-auto max-w-full whitespace-normal py-1 text-xs font-mono leading-snug"
                title={code}
              >
                <span className="break-all">{code}</span>
              </Badge>
            ))}
          </div>
        </div>
      )}
      {reasons.length > 0 && (
        <ul className="mt-2 list-disc space-y-0.5 pl-5 text-xs opacity-90">
          {reasons.map((reason, idx) => (
            <li key={idx} className="break-words">{reason}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RiskField({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <p className="text-xs uppercase tracking-wide opacity-70">{label}</p>
      <p className="mt-0.5 break-all text-xs font-mono">{value}</p>
    </div>
  );
}
