"use client";

import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { FlaskConical, Loader2, Pencil, PlayCircle, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useDeleteLLMConfig, usePatchLLMConfig, useProbeLLMConfig, useTestLLMConfig } from "@/hooks/use-llm";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { apiErrorMessage, parseApiError } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { parseFiniteNumberDraft, parsePositiveIntegerDraft } from "@/lib/utils";
import type { LLMConfig, LLMConfigPatch, LLMTestResult, StructuredOutputMode } from "@/types/api";

const MODES: StructuredOutputMode[] = ["auto", "function_calling", "json_schema", "json_mode", "json_prompt"];

function formFromConfig(config: LLMConfig) {
  return {
    provider: config.provider,
    model: config.model,
    base_url: config.base_url ?? "",
    api_key_secret_ref: "",
    temperature: String(config.temperature),
    max_tokens: String(config.max_tokens),
    timeout_seconds: String(config.timeout_seconds),
    structured_output_mode: config.structured_output_mode,
    enabled: Boolean(config.enabled),
  };
}

function validBaseUrl(value: string) {
  if (!value.trim()) return true;
  try { return ["http:", "https:"].includes(new URL(value).protocol); }
  catch { return false; }
}

function effectiveDraftMode(form: ReturnType<typeof formFromConfig>) {
  const text = `${form.provider} ${form.model} ${form.base_url}`.toLowerCase();
  return text.includes("deepseek") ? "json_prompt" : form.structured_output_mode;
}

function ResultPanel({ label, result }: { label: string; result: LLMTestResult }) {
  return (
    <div className={`w-full rounded-md border p-3 text-xs ${result.ok ? "border-success/40 bg-success/5" : "border-destructive/40 bg-destructive/5"}`}>
      <p className="font-medium">{label}: {result.ok ? "ok" : "failed"}</p>
      <p className="mt-1 font-mono">phase={result.phase} · mode={result.effective_structured_output_mode ?? "—"} · {result.elapsed_ms.toFixed(1)} ms</p>
      {result.structured_output_override ? <p className="mt-1 text-warning">{result.structured_output_override}</p> : null}
      {result.error ? <p className="mt-1 break-words text-destructive">{result.error}</p> : null}
    </div>
  );
}

export function LLMConfigActions({ config }: { config: LLMConfig }) {
  const { t, locale } = useI18n();
  const queryClient = useQueryClient();
  const runtimeSafety = useRuntimeSafety("admin");
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [probeOpen, setProbeOpen] = useState(false);
  const [form, setForm] = useState(() => formFromConfig(config));
  const [initialForm, setInitialForm] = useState(() => formFromConfig(config));
  const [editBaseVersion, setEditBaseVersion] = useState(config.version);
  const [checkResult, setCheckResult] = useState<LLMTestResult | null>(null);
  const [probeResult, setProbeResult] = useState<LLMTestResult | null>(null);
  const patchMutation = usePatchLLMConfig(config.id);
  const deleteMutation = useDeleteLLMConfig();
  const testMutation = useTestLLMConfig();
  const probeMutation = useProbeLLMConfig();
  const serverChanged = editOpen && config.version !== editBaseVersion;
  const runtimeUnknown = !runtimeSafety.allowed;
  const dirty = useMemo(() => JSON.stringify(form) !== JSON.stringify(initialForm), [form, initialForm]);
  const temperature = parseFiniteNumberDraft(form.temperature);
  const maxTokens = parsePositiveIntegerDraft(form.max_tokens);
  const timeoutSeconds = parseFiniteNumberDraft(form.timeout_seconds);
  const canSave = Boolean(
    form.provider.trim()
      && form.model.trim()
      && validBaseUrl(form.base_url)
      && temperature !== null
      && maxTokens !== null
      && timeoutSeconds !== null
      && timeoutSeconds > 0
      && dirty
      && !serverChanged,
  );

  function openEditor() {
    const latest = formFromConfig(config);
    setForm(latest);
    setInitialForm(latest);
    setEditBaseVersion(config.version);
    setEditOpen(true);
  }

  function handleSave() {
    if (!runtimeSafety.guard()) return;
    if (temperature === null || maxTokens === null || timeoutSeconds === null) return;
    const payload: LLMConfigPatch = {
      provider: form.provider.trim(),
      model: form.model.trim(),
      base_url: form.base_url.trim() || null,
      temperature,
      max_tokens: maxTokens,
      timeout_seconds: timeoutSeconds,
      structured_output_mode: form.structured_output_mode,
      enabled: form.enabled,
    };
    if (form.api_key_secret_ref.trim()) payload.api_key_secret_ref = form.api_key_secret_ref.trim();
    patchMutation.mutate({ payload, version: editBaseVersion }, {
      onSuccess: () => { toast.success(t("common.saved")); setEditOpen(false); },
      onError: async (error) => {
        const info = parseApiError(error, locale);
        if (info.status === 412) await queryClient.invalidateQueries({ queryKey: ["admin-llm"] });
        toast.error(info.status === 412 ? t("admin.llm.versionConflict") : t("common.failed"), { description: apiErrorMessage(info, locale) });
      },
    });
  }

  function handleDelete() {
    if (!runtimeSafety.guard()) return;
    deleteMutation.mutate({ configId: config.id, version: config.version }, {
      onSuccess: () => { toast.success(t("admin.llm.deleted")); setDeleteOpen(false); },
      onError: (error) => toast.error(t("common.failed"), { description: apiErrorMessage(error, locale) }),
    });
  }

  function handleCheck() {
    if (!runtimeSafety.guard()) return;
    testMutation.mutate(config.id, {
      onSuccess: setCheckResult,
      onError: (error) => toast.error(t("admin.llm.testFailed"), { description: apiErrorMessage(error, locale) }),
    });
  }

  function handleProbe() {
    if (!runtimeSafety.guard()) return;
    probeMutation.mutate(config.id, {
      onSuccess: (result) => { setProbeResult(result); setProbeOpen(false); },
      onError: (error) => toast.error(t("admin.llm.testFailed"), { description: apiErrorMessage(error, locale) }),
    });
  }

  return (
    <div className="flex min-w-64 flex-col items-end gap-2">
      <div className="flex items-center gap-1.5">
        <Button size="icon" variant="ghost" aria-label={t("admin.llm.edit")} disabled={runtimeUnknown} onClick={openEditor}><Pencil aria-hidden="true" /></Button>
        <Button size="icon" variant="ghost" aria-label={t("admin.llm.test")} disabled={runtimeUnknown || testMutation.isPending} onClick={handleCheck}>{testMutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <PlayCircle aria-hidden="true" />}</Button>
        <Button size="icon" variant="ghost" aria-label={t("admin.llm.providerProbe")} disabled={runtimeUnknown || probeMutation.isPending} onClick={() => setProbeOpen(true)}><FlaskConical aria-hidden="true" /></Button>
        <Button size="icon" variant="ghost" className="text-destructive" aria-label={t("admin.llm.delete")} disabled={runtimeUnknown} onClick={() => setDeleteOpen(true)}><Trash2 aria-hidden="true" /></Button>
      </div>
      {checkResult ? <ResultPanel label={t("admin.llm.test")} result={checkResult} /> : null}
      {probeResult ? <ResultPanel label={t("admin.llm.providerProbe")} result={probeResult} /> : null}

      <Dialog open={editOpen} onOpenChange={(open) => !patchMutation.isPending && setEditOpen(open)}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader><DialogTitle>{t("admin.llm.editTitle")}</DialogTitle><DialogDescription>{t("admin.llm.editDescription")}</DialogDescription></DialogHeader>
          {serverChanged ? <p className="rounded-md border border-warning/50 bg-warning/10 p-3 text-sm text-warning" role="alert">{t("admin.llm.dirtyServerUpdate", { old: editBaseVersion, latest: config.version })}</p> : null}
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5"><Label htmlFor={`llm-${config.id}-provider`}>{t("admin.llm.provider")}</Label><Select value={form.provider} onValueChange={(value) => value && setForm((current) => ({ ...current, provider: value }))}><SelectTrigger id={`llm-${config.id}-provider`}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="compatible_openai">Compatible OpenAI</SelectItem><SelectItem value="openai">OpenAI</SelectItem><SelectItem value="anthropic">Anthropic</SelectItem><SelectItem value="deepseek">DeepSeek</SelectItem><SelectItem value="local">Local</SelectItem></SelectContent></Select></div>
            <div className="space-y-1.5"><Label htmlFor={`llm-${config.id}-model`}>{t("admin.llm.model")}</Label><Input id={`llm-${config.id}-model`} value={form.model} onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))} /></div>
            <div className="space-y-1.5 sm:col-span-2"><Label htmlFor={`llm-${config.id}-base-url`}>{t("admin.llm.baseUrl")}</Label><Input id={`llm-${config.id}-base-url`} className="font-mono" aria-invalid={!validBaseUrl(form.base_url)} value={form.base_url} onChange={(event) => setForm((current) => ({ ...current, base_url: event.target.value }))} /></div>
            <div className="space-y-1.5 sm:col-span-2"><Label htmlFor={`llm-${config.id}-secret`}>{t("admin.llm.apiKeySecretRef")}</Label><Input id={`llm-${config.id}-secret`} className="font-mono" placeholder={config.api_key_secret_ref ?? "env:OPENAI_API_KEY"} value={form.api_key_secret_ref} onChange={(event) => setForm((current) => ({ ...current, api_key_secret_ref: event.target.value }))} /></div>
            <div className="space-y-1.5"><Label htmlFor={`llm-${config.id}-mode`}>{t("admin.llm.structuredMode")}</Label><Select value={form.structured_output_mode} onValueChange={(value) => value && setForm((current) => ({ ...current, structured_output_mode: value as StructuredOutputMode }))}><SelectTrigger id={`llm-${config.id}-mode`}><SelectValue /></SelectTrigger><SelectContent>{MODES.map((mode) => <SelectItem key={mode} value={mode}>{mode}</SelectItem>)}</SelectContent></Select><p className="text-xs text-muted-foreground">effective: <span className="font-mono">{effectiveDraftMode(form)}</span></p></div>
            <div className="flex items-center justify-between rounded-md border p-3"><Label htmlFor={`llm-${config.id}-enabled`}>{t("common.active")}</Label><Switch id={`llm-${config.id}-enabled`} checked={form.enabled} onCheckedChange={(enabled) => setForm((current) => ({ ...current, enabled }))} /></div>
            <div className="space-y-1.5"><Label htmlFor={`llm-${config.id}-temperature`}>{t("admin.llm.temperature")}</Label><Input id={`llm-${config.id}-temperature`} type="number" step="0.1" value={form.temperature} onChange={(event) => setForm((current) => ({ ...current, temperature: event.target.value }))} /></div>
            <div className="space-y-1.5"><Label htmlFor={`llm-${config.id}-max-tokens`}>{t("admin.llm.maxTokens")}</Label><Input id={`llm-${config.id}-max-tokens`} type="number" value={form.max_tokens} onChange={(event) => setForm((current) => ({ ...current, max_tokens: event.target.value }))} /></div>
            <div className="space-y-1.5"><Label htmlFor={`llm-${config.id}-timeout`}>{t("admin.llm.timeout")}</Label><Input id={`llm-${config.id}-timeout`} type="number" value={form.timeout_seconds} onChange={(event) => setForm((current) => ({ ...current, timeout_seconds: event.target.value }))} /></div>
          </div>
          <DialogFooter><Button variant="outline" disabled={patchMutation.isPending} onClick={() => setEditOpen(false)}>{t("common.cancel")}</Button><Button disabled={runtimeUnknown || patchMutation.isPending || !canSave} onClick={handleSave}>{patchMutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}{t("common.save")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={probeOpen} onOpenChange={(open) => !probeMutation.isPending && setProbeOpen(open)}><DialogContent><DialogHeader><DialogTitle>{t("admin.llm.providerProbe")}</DialogTitle><DialogDescription>{t("admin.llm.providerProbeHint")}</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={probeMutation.isPending} onClick={() => setProbeOpen(false)}>{t("common.cancel")}</Button><Button disabled={runtimeUnknown || probeMutation.isPending} onClick={handleProbe}>{probeMutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <FlaskConical aria-hidden="true" />}{t("admin.llm.providerProbe")}</Button></DialogFooter></DialogContent></Dialog>
      <Dialog open={deleteOpen} onOpenChange={(open) => !deleteMutation.isPending && setDeleteOpen(open)}><DialogContent><DialogHeader><DialogTitle>{t("admin.llm.confirmDelete")}</DialogTitle><DialogDescription>{t("admin.llm.confirmDeleteHint")}</DialogDescription></DialogHeader><p className="rounded-md border p-3 font-mono text-sm">{config.provider}/{config.model} · v{config.version}</p><DialogFooter><Button variant="outline" disabled={deleteMutation.isPending} onClick={() => setDeleteOpen(false)}>{t("common.cancel")}</Button><Button variant="destructive" disabled={runtimeUnknown || deleteMutation.isPending} onClick={handleDelete}>{deleteMutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}{t("admin.llm.delete")}</Button></DialogFooter></DialogContent></Dialog>
    </div>
  );
}
