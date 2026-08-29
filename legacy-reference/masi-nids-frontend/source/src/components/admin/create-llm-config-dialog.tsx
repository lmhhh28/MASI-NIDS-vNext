"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import api, { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { parseFiniteNumberDraft, parsePositiveIntegerDraft } from "@/lib/utils";
import type { StructuredOutputMode } from "@/types/api";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";

type CreateLLMConfigPayload = {
  provider: string;
  model: string;
  base_url: string | null;
  api_key_secret_ref: string | null;
  temperature: number;
  max_tokens: number;
  timeout_seconds: number;
  structured_output_mode: StructuredOutputMode;
  enabled: boolean;
};

const DEFAULT_FORM = {
  provider: "compatible_openai",
  model: "gpt-4o",
  base_url: "",
  api_key_secret_ref: "",
  temperature: "0.2",
  max_tokens: "4096",
  timeout_seconds: "60",
  structured_output_mode: "auto" as StructuredOutputMode,
};

const MODES: StructuredOutputMode[] = [
  "auto",
  "function_calling",
  "json_schema",
  "json_mode",
  "json_prompt",
];

function validBaseUrl(value: string) {
  if (!value.trim()) return true;
  try {
    return ["http:", "https:"].includes(new URL(value).protocol);
  } catch {
    return false;
  }
}

function effectiveMode(
  provider: string,
  model: string,
  baseUrl: string,
  configured: StructuredOutputMode,
) {
  return `${provider} ${model} ${baseUrl}`.toLowerCase().includes("deepseek")
    ? "json_prompt"
    : configured;
}

export function CreateLLMConfigDialog({ onClose }: { onClose: () => void }) {
  const { t, locale } = useI18n();
  const queryClient = useQueryClient();
  const runtimeSafety = useRuntimeSafety("admin");
  const [form, setForm] = useState(DEFAULT_FORM);
  const temperature = parseFiniteNumberDraft(form.temperature);
  const maxTokens = parsePositiveIntegerDraft(form.max_tokens);
  const timeoutSeconds = parseFiniteNumberDraft(form.timeout_seconds);
  const canSubmit = Boolean(
    form.provider.trim() &&
      form.model.trim() &&
      validBaseUrl(form.base_url) &&
      temperature !== null &&
      maxTokens !== null &&
      timeoutSeconds !== null &&
      timeoutSeconds > 0,
  );

  const createMutation = useMutation({
    mutationFn: (payload: CreateLLMConfigPayload) =>
      api.post("/admin/llm-config", payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin-llm"] });
      toast.success(t("admin.llm.configCreated"));
      onClose();
    },
    onError: (error) =>
      toast.error(t("common.failed"), {
        description: apiErrorMessage(error, locale),
      }),
  });

  function submit() {
    if (!runtimeSafety.guard()) return;
    if (
      temperature === null ||
      maxTokens === null ||
      timeoutSeconds === null ||
      timeoutSeconds <= 0
    ) {
      toast.error(t("admin.llm.invalidNumbers"));
      return;
    }
    createMutation.mutate({
      provider: form.provider.trim(),
      model: form.model.trim(),
      base_url: form.base_url.trim() || null,
      api_key_secret_ref: form.api_key_secret_ref.trim() || null,
      temperature,
      max_tokens: maxTokens,
      timeout_seconds: timeoutSeconds,
      structured_output_mode: form.structured_output_mode,
      enabled: true,
    });
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !createMutation.isPending && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("admin.llm.addConfigTitle")}</DialogTitle>
          <DialogDescription>{t("admin.llm.addConfigDescription")}</DialogDescription>
        </DialogHeader>
        <div className="mt-2 space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="llm-create-provider">{t("admin.llm.provider")}</Label>
              <select
                id="llm-create-provider"
                value={form.provider}
                onChange={(event) =>
                  setForm((current) => ({ ...current, provider: event.currentTarget.value }))
                }
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="compatible_openai">Compatible OpenAI</option>
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
                <option value="deepseek">DeepSeek</option>
                <option value="local">Local</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="llm-create-model">{t("admin.llm.model")}</Label>
              <Input
                id="llm-create-model"
                value={form.model}
                onChange={(event) =>
                  setForm((current) => ({ ...current, model: event.currentTarget.value }))
                }
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="llm-create-base-url">{t("admin.llm.baseUrl")}</Label>
            <Input
              id="llm-create-base-url"
              className="font-mono"
              aria-invalid={!validBaseUrl(form.base_url)}
              placeholder="https://api.openai.com/v1"
              value={form.base_url}
              onChange={(event) =>
                setForm((current) => ({ ...current, base_url: event.currentTarget.value }))
              }
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="llm-create-secret">{t("admin.llm.apiKeySecretRef")}</Label>
            <Input
              id="llm-create-secret"
              className="font-mono"
              placeholder="env:OPENAI_API_KEY"
              value={form.api_key_secret_ref}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  api_key_secret_ref: event.currentTarget.value,
                }))
              }
            />
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="llm-create-temperature">{t("admin.llm.temperature")}</Label>
              <Input
                id="llm-create-temperature"
                type="number"
                step="0.1"
                value={form.temperature}
                onChange={(event) =>
                  setForm((current) => ({ ...current, temperature: event.currentTarget.value }))
                }
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="llm-create-max-tokens">{t("admin.llm.maxTokens")}</Label>
              <Input
                id="llm-create-max-tokens"
                type="number"
                value={form.max_tokens}
                onChange={(event) =>
                  setForm((current) => ({ ...current, max_tokens: event.currentTarget.value }))
                }
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="llm-create-timeout">{t("admin.llm.timeout")}</Label>
              <Input
                id="llm-create-timeout"
                type="number"
                value={form.timeout_seconds}
                onChange={(event) =>
                  setForm((current) => ({ ...current, timeout_seconds: event.currentTarget.value }))
                }
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="llm-create-mode">{t("admin.llm.structuredMode")}</Label>
            <select
              id="llm-create-mode"
              value={form.structured_output_mode}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  structured_output_mode: event.currentTarget.value as StructuredOutputMode,
                }))
              }
              className="h-9 w-full rounded-md border border-input bg-background px-3 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {MODES.map((mode) => (
                <option key={mode} value={mode}>
                  {mode}
                </option>
              ))}
            </select>
            <p className="text-xs text-muted-foreground">
              effective:{" "}
              <span className="font-mono">
                {effectiveMode(
                  form.provider,
                  form.model,
                  form.base_url,
                  form.structured_output_mode,
                )}
              </span>
            </p>
          </div>
          <Button className="w-full" onClick={submit} disabled={!runtimeSafety.allowed || createMutation.isPending || !canSubmit}>
            {createMutation.isPending ? (
              <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : null}
            {t("common.create")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
