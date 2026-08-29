"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, ShieldAlert } from "lucide-react";
import { toast } from "sonner";

import { ErrorState, LoadingState } from "@/components/async-state";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import api, { apiErrorMessage, parseApiError } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type { RiskPolicy } from "@/types/api";

function useRiskPolicy() {
  return useQuery<RiskPolicy>({
    queryKey: ["admin-risk"],
    queryFn: ({ signal }) => api.get<RiskPolicy>("/admin/risk-policy", { signal }).then((response) => response.data),
  });
}

type RiskPolicyForm = {
  min_directional_confidence: string;
  low_risk_ttl_seconds: string;
  active_rule_cap: string;
  max_cleanup_lag_seconds: string;
};

function formFromPolicy(policy: RiskPolicy): RiskPolicyForm {
  return {
    min_directional_confidence: String(policy.min_directional_confidence),
    low_risk_ttl_seconds: String(policy.low_risk_ttl_seconds),
    active_rule_cap: String(policy.active_rule_cap),
    max_cleanup_lag_seconds: String(policy.max_cleanup_lag_seconds),
  };
}

function isConfidence(value: string) {
  const parsed = Number(value.trim());
  return value.trim() !== "" && Number.isFinite(parsed) && parsed >= 0 && parsed <= 1;
}

function isPositiveInteger(value: string) {
  return /^\d+$/.test(value.trim()) && Number.isSafeInteger(Number(value)) && Number(value) >= 1;
}

export default function RiskPolicyPage() {
  const policyQuery = useRiskPolicy();
  const runtimeSafety = useRuntimeSafety("admin");
  const runtimeQuery = runtimeSafety.runtime;
  const queryClient = useQueryClient();
  const { t, locale } = useI18n();
  const [draft, setDraft] = useState<RiskPolicyForm | null>(null);
  const [changeReason, setChangeReason] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [conflictVersion, setConflictVersion] = useState<number | null>(null);
  const policy = policyQuery.data;
  const form = policy ? draft ?? formFromPolicy(policy) : null;
  const runtimeUnknown = !runtimeSafety.allowed;

  const changes = useMemo(() => {
    if (!policy || !form) return [];
    const next = {
      min_directional_confidence: Number(form.min_directional_confidence),
      low_risk_ttl_seconds: Number(form.low_risk_ttl_seconds),
      active_rule_cap: Number(form.active_rule_cap),
      max_cleanup_lag_seconds: Number(form.max_cleanup_lag_seconds),
    };
    return Object.entries(next)
      .filter(([key, value]) => value !== policy[key as keyof typeof next])
      .map(([key, value]) => ({ key, before: policy[key as keyof typeof next], after: value }));
  }, [form, policy]);

  const valid = Boolean(form
    && isConfidence(form.min_directional_confidence)
    && isPositiveInteger(form.low_risk_ttl_seconds)
    && isPositiveInteger(form.active_rule_cap)
    && isPositiveInteger(form.max_cleanup_lag_seconds));

  const updateMutation = useMutation({
    retry: false,
    mutationFn: async () => {
      if (!policy || !form) throw new Error("RISK_POLICY_NOT_LOADED");
      return api.patch<RiskPolicy>("/admin/risk-policy", {
        min_directional_confidence: Number(form.min_directional_confidence),
        low_risk_ttl_seconds: Number(form.low_risk_ttl_seconds),
        active_rule_cap: Number(form.active_rule_cap),
        max_cleanup_lag_seconds: Number(form.max_cleanup_lag_seconds),
        change_reason: changeReason.trim(),
      }, { headers: { "If-Match": String(policy.version) } });
    },
    onSuccess: async () => {
      setConfirmOpen(false);
      setDraft(null);
      setChangeReason("");
      setConflictVersion(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["admin-risk"] }),
        queryClient.invalidateQueries({ queryKey: ["risk-policy-summary"] }),
      ]);
      toast.success(t("admin.risk.policyUpdated"));
    },
    onError: async (error) => {
      setConfirmOpen(false);
      const info = parseApiError(error, locale);
      if (info.status === 412) {
        setConflictVersion(policy?.version ?? null);
        await policyQuery.refetch();
        return;
      }
      toast.error(t("common.failed"), { description: apiErrorMessage(info, locale) });
    },
  });

  if (policyQuery.isLoading) return <LoadingState rows={4} />;
  if (policyQuery.isError || !policy || !form) return <ErrorState error={policyQuery.error} onRetry={() => policyQuery.refetch()} />;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{t("admin.risk.title")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{t("admin.risk.subtitle")}</p>
      </div>
      {runtimeUnknown ? <ErrorState title={t("operations.runtimeUnknown")} error={runtimeQuery.error ?? new Error("runtime unavailable")} onRetry={() => runtimeQuery.refetch()} /> : null}
      {conflictVersion !== null ? <div className="rounded-md border border-warning/50 bg-warning/10 p-3 text-sm text-warning" role="alert">{t("admin.risk.versionConflict", { old: conflictVersion, latest: policy.version })}</div> : null}
      <Card className="max-w-2xl">
        <CardHeader><CardTitle className="text-base">{t("admin.risk.settings", { version: policy.version })}</CardTitle></CardHeader>
        <CardContent className="space-y-5">
          <div className="flex items-center justify-between rounded-md border bg-muted/30 p-3">
            <div><Label htmlFor="risk-auto-apply">{t("admin.risk.autoApply")}</Label><p className="mt-1 text-xs text-muted-foreground">{t("admin.risk.autoApplyReserved")}</p></div>
            <Switch id="risk-auto-apply" checked={Boolean(policy.auto_apply_enabled)} disabled aria-readonly="true" />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            {([
              ["min_directional_confidence", "admin.risk.minConfidence", "decimal"],
              ["low_risk_ttl_seconds", "admin.risk.lowRiskTtl", "numeric"],
              ["active_rule_cap", "admin.risk.activeRuleCap", "numeric"],
              ["max_cleanup_lag_seconds", "admin.risk.cleanupLag", "numeric"],
            ] as const).map(([field, label, inputMode]) => (
              <div key={field} className="space-y-1.5">
                <Label htmlFor={`risk-${field}`}>{t(label)}</Label>
                <Input id={`risk-${field}`} inputMode={inputMode} value={form[field]} onChange={(event) => setDraft({ ...form, [field]: event.target.value })} />
                <p className="text-xs text-muted-foreground">{t("admin.risk.currentValue")}: <span className="font-mono">{String(policy[field])}</span></p>
              </div>
            ))}
          </div>
          <div className="space-y-2"><Label htmlFor="risk-change-reason">{t("admin.risk.changeReason")}</Label><Textarea id="risk-change-reason" value={changeReason} onChange={(event) => setChangeReason(event.target.value)} required /></div>
          <div className="rounded-md border p-3">
            <p className="text-sm font-medium">{t("admin.risk.diff")}</p>
            {changes.length === 0 ? <p className="mt-1 text-sm text-muted-foreground">{t("admin.risk.noChanges")}</p> : <ul className="mt-2 space-y-1 font-mono text-xs">{changes.map((change) => <li key={change.key}>{change.key}: {String(change.before)} → {String(change.after)}</li>)}</ul>}
          </div>
          <Button className="w-full" disabled={runtimeUnknown || updateMutation.isPending || !valid || !changeReason.trim() || changes.length === 0} onClick={() => setConfirmOpen(true)}><ShieldAlert aria-hidden="true" />{t("admin.risk.savePolicy")}</Button>
        </CardContent>
      </Card>
      <Dialog open={confirmOpen} onOpenChange={(open) => !updateMutation.isPending && setConfirmOpen(open)}>
        <DialogContent>
          <DialogHeader><DialogTitle>{t("admin.risk.confirmTitle")}</DialogTitle><DialogDescription>{changeReason}</DialogDescription></DialogHeader>
          <ul className="space-y-1 rounded-md border p-3 font-mono text-xs">{changes.map((change) => <li key={change.key}>{change.key}: {String(change.before)} → {String(change.after)}</li>)}</ul>
          <DialogFooter><Button variant="outline" disabled={updateMutation.isPending} onClick={() => setConfirmOpen(false)}>{t("common.cancel")}</Button><Button disabled={runtimeUnknown || updateMutation.isPending} onClick={() => { if (runtimeSafety.guard()) updateMutation.mutate(); }}>{updateMutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}{t("common.save")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
