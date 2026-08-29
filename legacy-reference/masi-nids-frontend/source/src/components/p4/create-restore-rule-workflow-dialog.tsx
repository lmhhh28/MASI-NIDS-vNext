"use client";

import { useState } from "react";
import Link from "next/link";
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
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/lib/i18n";
import { useStartWorkflow } from "@/hooks/use-workflows";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { apiErrorMessage, parseApiError } from "@/lib/api";
import type { P4Deployment } from "@/types/api";

interface Props {
  deployment: P4Deployment | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated?: (workflowId: string) => void;
}

/**
 * Open a `restore_rule` workflow against an applied P4 deployment.
 *
 * The semantic template is fixed (mono Badge) — `restore_rule` is the
 * only template that targets a deployment, and TTL is intentionally not
 * accepted (rollback derives its match fields from the original
 * deployment's `rollback_entry_json` or `request_json`).
 */
export function CreateRestoreRuleWorkflowDialog({
  deployment,
  open,
  onOpenChange,
  onCreated,
}: Props) {
  const { t, locale } = useI18n();
  const startMut = useStartWorkflow();
  const runtimeSafety = useRuntimeSafety("workflow");

  const [rationale, setRationale] = useState("");
  const [rationaleOpen, setRationaleOpen] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const writerMode = String(deployment?.result?.writer_mode ?? "—");
  const isDemoFake = writerMode === "demo_fake";
  const ownerType = deployment?.owner_type
    ? String(deployment.owner_type)
    : t("common.unknown");

  function shortId(value: string | null | undefined): string {
    if (!value) return "—";
    return value.length > 12 ? value.slice(0, 12) + "…" : value;
  }

  function handleOpenChange(next: boolean) {
    if (!next) {
      setRationale("");
      setRationaleOpen(false);
      setSubmitError(null);
    }
    onOpenChange(next);
  }

  function handleSubmit() {
    if (!runtimeSafety.guard()) return;
    if (!deployment) return;
    setSubmitError(null);
    startMut.mutate(
      {
        template_name: "restore_rule",
        deployment_id: deployment.id,
        requested_intent: rationale.trim() || undefined,
      },
      {
        onSuccess: (workflow) => {
          toast.success(t("p4.createRestore.created"), {
            description: shortId(workflow.id),
          });
          onCreated?.(workflow.id);
          handleOpenChange(false);
        },
        onError: (err) => {
          const info = parseApiError(err, locale);
          // Special-case the rollback-unavailable code so users see the
          // semantic explanation instead of a generic backend message.
          if (info.code === "ROLLBACK_STATE_UNAVAILABLE") {
            setSubmitError(t("p4.createRestore.rollbackStateUnavailable"));
          } else {
            setSubmitError(apiErrorMessage(info, locale));
          }
          toast.error(t("events.workflowCreateFailed"), {
            description:
              info.code === "ROLLBACK_STATE_UNAVAILABLE"
                ? t("p4.createRestore.rollbackStateUnavailable")
                : apiErrorMessage(info, locale),
          });
        },
      },
    );
  }

  const submitDisabled = !runtimeSafety.allowed || !deployment || startMut.isPending;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("p4.createRestore.title")}</DialogTitle>
          <DialogDescription>
            {t("p4.createRestore.description")}
          </DialogDescription>
        </DialogHeader>

        {deployment && (
          <div className="space-y-2 rounded-lg border border-border bg-secondary/20 p-3">
            <p className="text-xs uppercase text-muted-foreground">
              {t("p4.createRestore.deploymentContextLabel")}
            </p>
            <div className="grid gap-1 text-xs font-mono">
              <div className="break-all">{shortId(deployment.id)}</div>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="text-xs">
                  {t("p4.createRestore.targetTable")}: {deployment.table_name}
                </Badge>
                <Badge
                  variant={isDemoFake ? "outline" : "secondary"}
                  className={
                    isDemoFake
                      ? "text-xs border-warning/40 text-warning"
                      : "text-xs"
                  }
                >
                  {t("p4.createRestore.targetWriterMode")}: {writerMode}
                </Badge>
                <span>
                  {t("p4.createRestore.targetOriginalOwner")}: {ownerType}
                </span>
              </div>
            </div>
          </div>
        )}

        <div className="space-y-1.5">
          <p className="text-xs font-medium">
            {t("dashboard.createBlock.templateBadge")}
          </p>
          <Badge variant="secondary" className="font-mono text-xs">
            restore_rule
          </Badge>
          <p className="text-xs text-muted-foreground">
            {t("dashboard.createBlock.templateBadgeHelper")}
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
                <span>{t("p4.createRestore.rationaleToggle")}</span>
                <ChevronDown
                  className={`h-3 w-3 transition-transform duration-200 ${
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
              placeholder={t("p4.createRestore.rationalePlaceholder")}
              value={rationale}
              disabled={startMut.isPending}
              onChange={(e) => setRationale(e.target.value)}
            />
          </CollapsibleContent>
        </Collapsible>

        {submitError && (
          <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive" role="alert">
            {submitError}
            {submitError === t("p4.createRestore.rollbackStateUnavailable") && (
              <Link
                href="/workflows"
                className="ml-2 underline cursor-pointer hover:text-destructive/80"
              >
                {t("p4.createRestore.viewWorkflowAction")}
              </Link>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button
            variant="outline"
            size="sm"
            disabled={startMut.isPending}
            onClick={() => handleOpenChange(false)}
          >
            {t("p4.createRestore.cancel")}
          </Button>
          <Button size="sm" onClick={handleSubmit} disabled={submitDisabled}>
            {startMut.isPending ? (
              <>
                <Loader2 className="mr-1.5 h-3 w-3 animate-spin motion-reduce:animate-none" />
                {t("p4.createRestore.submitting")}
              </>
            ) : (
              t("p4.createRestore.submit")
            )}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
