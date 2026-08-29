"use client";

import { useState } from "react";
import { ChevronDown, Loader2 } from "lucide-react";
import { toast } from "sonner";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/lib/i18n";
import { useTemplates } from "@/hooks/use-templates";
import { useStartWorkflow } from "@/hooks/use-workflows";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { apiErrorMessage, parseApiError } from "@/lib/api";
import { asPositiveInteger } from "@/lib/utils";
import type { NidsEvent, WorkflowTemplateOption } from "@/types/api";

interface Props {
  event: NidsEvent | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated?: (workflowId: string, ttlSecondsSource?: string) => void;
}

/**
 * Structured form for creating a `block_exact_flow` workflow from a
 * dashboard anomaly row.
 *
 * Match fields are NOT user-editable — by design they are always derived
 * from `DirectionalEvidence.observed_*` at the backend. This dialog only
 * configures structured `template_params` (currently `ttl_seconds`).
 *
 * The semantic template is rendered as a read-only Badge instead of a
 * `<Select>` because today the backend exposes exactly one executable
 * block template (`block_exact_flow`). When a second template earns
 * `supports_ttl_seconds=true` upstream, swap the Badge back for a Select
 * driven by the same `useTemplates()` data.
 */
export function CreateBlockWorkflowDialog({
  event,
  open,
  onOpenChange,
  onCreated,
}: Props) {
  const { t, locale } = useI18n();
  const templatesQ = useTemplates();
  const startMut = useStartWorkflow();
  const runtimeSafety = useRuntimeSafety("workflow");

  const supportedTemplate: WorkflowTemplateOption | null =
    templatesQ.data?.items.find((item) => item.supports_ttl_seconds) ?? null;
  const templateName = supportedTemplate?.name ?? "";

  const [ttlSecondsDraft, setTtlSecondsDraft] = useState("");
  const [rationale, setRationale] = useState("");
  const [rationaleOpen, setRationaleOpen] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const ttlInvalid =
    ttlSecondsDraft !== "" && asPositiveInteger(ttlSecondsDraft) == null;
  const noExecutableTemplate = !templatesQ.isLoading && !supportedTemplate;
  const submitDisabled =
    !runtimeSafety.allowed ||
    !event ||
    !templateName ||
    startMut.isPending ||
    templatesQ.isLoading ||
    ttlInvalid;

  const flowLabel =
    event?.flow_id ?? `${event?.src_ip ?? "?"}->${event?.dst_ip ?? "?"}`;

  function handleOpenChange(next: boolean) {
    if (!next) {
      setTtlSecondsDraft("");
      setRationale("");
      setRationaleOpen(false);
      setSubmitError(null);
    }
    onOpenChange(next);
  }

  function handleSubmit() {
    if (!runtimeSafety.guard()) return;
    if (!event || !templateName) {
      return;
    }
    setSubmitError(null);
    const trimmed = ttlSecondsDraft.trim();
    let ttl: number | undefined;
    if (trimmed !== "") {
      const parsed = asPositiveInteger(trimmed);
      if (parsed == null) {
        setSubmitError(t("dashboard.createBlock.ttlInvalid"));
        return;
      }
      ttl = parsed;
    }
    startMut.mutate(
      {
        nids_event_id: event.id,
        template_name: templateName,
        template_params: ttl != null ? { ttl_seconds: ttl } : null,
        requested_intent: rationale.trim() || undefined,
      },
      {
        onSuccess: (workflow) => {
          const intent = (workflow.intent ?? {}) as {
            ttl_seconds?: number | null;
            ttl_seconds_source?: string | null;
          };
          const source = intent.ttl_seconds_source ?? null;
          const sourceKey =
            source === "template_params"
              ? ("events.workflowTtlSourceTemplateParams" as const)
              : ("events.workflowTtlSourceTemplateDefault" as const);
          const ttlText = intent.ttl_seconds ?? "—";
          toast.success(t("events.workflowCreated"), {
            description: `${workflow.template_name} · TTL ${ttlText} · ${t(sourceKey)}`,
          });
          onCreated?.(workflow.id, source ?? undefined);
          handleOpenChange(false);
        },
        onError: (err) => {
          const info = parseApiError(err, locale);
          setSubmitError(apiErrorMessage(info, locale));
          toast.error(t("events.workflowCreateFailed"), {
            description: apiErrorMessage(info, locale),
          });
        },
      }
    );
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("dashboard.createBlock.title")}</DialogTitle>
          <DialogDescription>
            {t("dashboard.createBlock.description")}
          </DialogDescription>
        </DialogHeader>

        {event && (
          <div className="space-y-2 rounded-lg border border-border bg-secondary/20 p-3">
            <p className="text-xs uppercase text-muted-foreground">
              {t("dashboard.createBlock.eventContextLabel")}
            </p>
            <div className="grid gap-1 text-xs font-mono">
              <div className="break-all">{flowLabel}</div>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="text-xs">
                  {event.nearest_class ?? "—"}
                </Badge>
                <Badge variant="destructive" className="text-xs">
                  {event.decision ?? "—"}
                </Badge>
                <span>
                  {t("dashboard.createBlock.score")}:{" "}
                  {event.legacy_score?.toFixed(4) ?? "—"}
                </span>
              </div>
            </div>
          </div>
        )}

        <div className="space-y-1.5">
          <p className="text-xs font-medium">
            {t("dashboard.createBlock.templateBadge")}
          </p>
          {supportedTemplate ? (
            <div className="flex flex-wrap items-center gap-2">
              <Badge
                variant="secondary"
                className="font-mono text-xs"
                title={t("dashboard.createBlock.templateBadgeHelper")}
              >
                {supportedTemplate.name}
              </Badge>
              {supportedTemplate.description && (
                <span className="text-xs text-muted-foreground line-clamp-2">
                  {supportedTemplate.description}
                </span>
              )}
            </div>
          ) : templatesQ.isLoading ? (
            <Badge
              variant="outline"
              className="font-mono text-xs opacity-50"
            >
              <Loader2 className="mr-1 h-3 w-3 animate-spin motion-reduce:animate-none" />
              ...
            </Badge>
          ) : (
            <Badge
              variant="outline"
              className="font-mono text-xs opacity-50"
            >
              —
            </Badge>
          )}
          <p className="text-xs text-muted-foreground">
            {t("dashboard.createBlock.templateBadgeHelper")}
          </p>
        </div>

        {noExecutableTemplate && (
          <div className="rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning">
            {t("dashboard.createBlock.noExecutableTemplate")}
          </div>
        )}

        <div className="space-y-1.5">
          <Label htmlFor="block-ttl" className="text-xs">
            {t("dashboard.createBlock.ttlSeconds")}
          </Label>
          <Input
            id="block-ttl"
            type="number"
            inputMode="numeric"
            min={1}
            step={1}
            placeholder="300"
            className="h-8 text-xs"
            value={ttlSecondsDraft}
            disabled={startMut.isPending || !supportedTemplate}
            aria-invalid={ttlInvalid}
            aria-describedby="block-ttl-helper"
            onChange={(e) => setTtlSecondsDraft(e.target.value)}
          />
          <p
            id="block-ttl-helper"
            className={
              ttlInvalid
                ? "text-xs text-destructive"
                : "text-xs text-muted-foreground"
            }
          >
            {ttlInvalid
              ? t("dashboard.createBlock.ttlInvalid")
              : t("dashboard.createBlock.ttlSecondsHelper")}
          </p>
        </div>

        <Collapsible open={rationaleOpen} onOpenChange={setRationaleOpen}>
          <CollapsibleTrigger
            render={
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-full justify-between text-xs"
              >
                <span>{t("dashboard.createBlock.rationaleToggle")}</span>
                <ChevronDown
                  className={`h-3 w-3 transition-transform ${
                    rationaleOpen ? "rotate-180" : ""
                  }`}
                />
              </Button>
            }
          />
          <CollapsibleContent className="space-y-1.5 pt-2">
            <Textarea
              rows={3}
              className="resize-none text-xs font-mono"
              placeholder={t("dashboard.createBlock.rationalePlaceholder")}
              value={rationale}
              disabled={startMut.isPending}
              onChange={(e) => setRationale(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              {templatesQ.data?.meta.llm_overlay_enabled
                ? t("dashboard.createBlock.rationaleHelperLlm")
                : t("dashboard.createBlock.rationaleHelperOff")}
            </p>
          </CollapsibleContent>
        </Collapsible>

        {submitError && (
          <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive" role="alert">
            {submitError}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button
            variant="outline"
            size="sm"
            disabled={startMut.isPending}
            onClick={() => handleOpenChange(false)}
          >
            {t("dashboard.createBlock.cancel")}
          </Button>
          <Button size="sm" onClick={handleSubmit} disabled={submitDisabled}>
            {startMut.isPending ? (
              <>
                <Loader2 className="mr-1.5 h-3 w-3 animate-spin motion-reduce:animate-none" />
                {t("dashboard.createBlock.submitting")}
              </>
            ) : (
              t("dashboard.createBlock.submit")
            )}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
