"use client";

import dynamic from "next/dynamic";
import { useQuery } from "@tanstack/react-query";
import { Plus, BrainCircuit } from "lucide-react";
import { useState } from "react";

import api, { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { LLMConfig } from "@/types/api";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { ErrorState } from "@/components/async-state";

const CreateLLMConfigDialog = dynamic(() =>
  import("@/components/admin/create-llm-config-dialog").then(
    (module) => module.CreateLLMConfigDialog,
  ),
);
const LLMConfigActions = dynamic(() =>
  import("@/components/admin/llm-config-actions").then(
    (module) => module.LLMConfigActions,
  ),
);

function useConfigs() {
  return useQuery<LLMConfig[]>({ queryKey: ["admin-llm"], queryFn: ({ signal }) => api.get("/admin/llm-config", { signal }).then(r => r.data) });
}

export default function LLMPage() {
  const configsQ = useConfigs();
  const runtimeSafety = useRuntimeSafety("admin");
  const [open, setOpen] = useState(false);
  const { t, locale } = useI18n();
  const runtimeUnknown = !runtimeSafety.allowed;
  const runtimeQ = runtimeSafety.runtime;

  if (configsQ.isError) {
    return (
      <div className="rounded-xl border border-destructive/30 bg-card p-6 text-sm text-destructive">
        {apiErrorMessage(configsQ.error, locale)}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">{t("admin.llm.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("admin.llm.subtitle")}</p>
        </div>
        <Button size="sm" disabled={runtimeUnknown} onClick={() => setOpen(true)}>
          <Plus aria-hidden="true" />
          {t("admin.llm.addConfig")}
        </Button>
      </div>
      {open ? <CreateLLMConfigDialog onClose={() => setOpen(false)} /> : null}
      {runtimeUnknown ? <ErrorState title={t("operations.runtimeUnknown")} error={runtimeQ.error ?? new Error("runtime unavailable")} onRetry={() => runtimeQ.refetch()} /> : null}
      {configsQ.isLoading ? (
        <div className="space-y-3">{[1, 2].map(i => <Skeleton key={i} className="h-24 rounded-xl" />)}</div>
      ) : !configsQ.data || configsQ.data.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-12 text-center">
          <BrainCircuit className="h-10 w-10 text-muted-foreground/40 mx-auto mb-3" />
          <p className="text-sm text-muted-foreground">{t("admin.llm.noConfigs")}</p>
        </div>
      ) : (
        <div className="space-y-3">
          {configsQ.data.map(c => (
            <Card key={c.id} className="border-border bg-card">
              <CardContent className="flex flex-wrap items-center justify-between gap-3 p-4">
                <div className="flex min-w-0 items-center gap-3">
                  <BrainCircuit className="h-5 w-5 shrink-0 text-chart-5" />
                  <div className="min-w-0">
                    <p className="text-sm font-medium">{c.provider} / <span className="font-mono text-primary">{c.model}</span></p>
                    <p className="truncate text-xs text-muted-foreground font-mono">{c.base_url ?? t("common.default")} · secret={c.api_key_secret_ref ?? t("common.notConfigured")} · v{c.version}</p>
                    <p className="truncate text-xs text-muted-foreground font-mono">temp={c.temperature} · max={c.max_tokens} · timeout={c.timeout_seconds}</p>
                    <p className="text-xs text-muted-foreground">configured <span className="font-mono">{c.structured_output_mode}</span> → effective <span className="font-mono text-primary">{c.effective_structured_output_mode}</span>{c.structured_output_override ? ` · ${c.structured_output_override}` : ""}</p>
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  <Badge variant={c.enabled ? "success" : "secondary"} className="text-xs">{c.enabled ? t("common.active") : t("common.disabled")}</Badge>
                  <LLMConfigActions config={c} />
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
