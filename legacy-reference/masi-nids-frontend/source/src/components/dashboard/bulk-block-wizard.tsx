"use client";

import * as React from "react";
import { ShieldAlert, FileSearch } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useI18n } from "@/lib/i18n";
import { isKnownAttackAnomaly } from "@/lib/nids-events";
import { BULK_HARD_LIMIT } from "@/hooks/use-bulk-action";
import type { NidsEvent } from "@/types/api";

export type BlockWizardStep = "precheck" | "confirm";

interface BulkBlockWizardProps {
  open: boolean;
  step: BlockWizardStep;
  selectedEvents: NidsEvent[];
  /** Called when the user dismisses the wizard. */
  onClose: () => void;
  /** Called from step 1 when the operator wants to resolve missing evidence first. */
  onResolveEvidenceFirst: () => void;
  /** Called when the operator advances from precheck → confirm. */
  onContinueToConfirm: () => void;
  /** Called when the operator finally confirms; expected to launch the bulk runner. */
  onConfirmCreate: () => void;
  /** True while another bulk action is in flight (disables CTAs). */
  isAnyBulkRunning: boolean;
  /** Runtime is fresh and workflow writes are allowed. */
  runtimeAllowed: boolean;
}

/**
 * Chained 3-step wizard for bulk creating block_exact_flow workflows.
 *
 * Step 1 — Precheck: surfaces how many selected events still lack
 *   resolved DirectionalEvidence. The block_exact_flow workflow strictly
 *   requires evidence; events without it would land in `blocked` status
 *   immediately. The CTA shortcuts to a bulk evidence runner so the
 *   operator never has to leave this flow.
 * Step 2 — Confirm: explicit warning that the operation creates
 *   `needs_human_review` workflows and **does not deploy any P4 rule**;
 *   requires an ack checkbox to enable the final action.
 * Step 3 — Progress: not rendered here; the parent dashboard mounts
 *   `<BulkProgressCard>` once `onConfirmCreate` flips bulkKind="block".
 *
 * Pre-Delivery Checklist conformance:
 * - All buttons / checkboxes carry visible labels and `cursor-pointer`.
 * - Focus-visible ring inherited from shared Button/Checkbox styles.
 * - Step labels (precheck / confirm) and counts use aria-live="polite".
 */
export function BulkBlockWizard({
  open,
  step,
  selectedEvents,
  onClose,
  onResolveEvidenceFirst,
  onContinueToConfirm,
  onConfirmCreate,
  isAnyBulkRunning,
  runtimeAllowed,
}: BulkBlockWizardProps) {
  const { t } = useI18n();
  const [confirmAck, setConfirmAck] = React.useState(false);

  // Note: the parent unmounts this component when the dialog closes
  // (see dashboard `{blockWizardOpen ? <BulkBlockWizard ... /> : null}`),
  // so `confirmAck` resets implicitly on every reopen. We don't reset
  // it via useEffect because React 19 disallows setState in effects.

  const blockableEvents = selectedEvents.filter(isKnownAttackAnomaly);
  const nonBlockableCount = selectedEvents.length - blockableEvents.length;
  const evidenceReadyEvents = blockableEvents.filter(
    (e) =>
      e.directional_evidence_status === "resolved" ||
      e.directional_evidence_status === "reviewed",
  );
  const missingCount = blockableEvents.length - evidenceReadyEvents.length;
  const overLimit = selectedEvents.length > BULK_HARD_LIMIT;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-warning" aria-hidden />
            {step === "precheck"
              ? t("events.bulk.block.precheck.title")
              : t("events.bulk.block.confirm.title")}
          </DialogTitle>
          <DialogDescription>
            {step === "precheck"
              ? t("events.bulk.block.precheck.summary", {
                  ready: evidenceReadyEvents.length,
                  missing: missingCount,
                })
              : t("events.bulk.block.confirm.explainer", {
                  n: blockableEvents.length,
                })}
          </DialogDescription>
        </DialogHeader>

        {step === "precheck" ? (
          <div className="space-y-3 text-sm">
            {missingCount > 0 ? (
              <div
                className="rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning"
                aria-live="polite"
              >
                {t("events.bulk.block.precheck.summary", {
                  ready: evidenceReadyEvents.length,
                  missing: missingCount,
                })}
              </div>
            ) : (
              <div className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs text-primary">
                {t("events.bulk.block.precheck.allReady")}
              </div>
            )}
            {nonBlockableCount > 0 ? (
              <div className="rounded-md border border-muted bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                {t("events.bulk.block.precheck.skipped", {
                  skipped: nonBlockableCount,
                  ready: blockableEvents.length,
                })}
              </div>
            ) : null}
            {overLimit ? (
              <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
                {t("events.bulk.limit.exceeded", { max: BULK_HARD_LIMIT })}
              </div>
            ) : null}
            <DialogFooter>
              <Button
                variant="outline"
                className="cursor-pointer"
                onClick={onClose}
              >
                {t("common.cancel")}
              </Button>
              {missingCount > 0 ? (
                <Button
                  variant="outline"
                  className="cursor-pointer"
                  disabled={!runtimeAllowed || isAnyBulkRunning}
                  onClick={onResolveEvidenceFirst}
                >
                  <FileSearch className="mr-1 h-3 w-3" aria-hidden />
                  {t("events.bulk.block.precheck.resolveFirst")}
                </Button>
              ) : null}
              <Button
                variant="default"
                className="cursor-pointer"
                disabled={!runtimeAllowed || blockableEvents.length === 0 || missingCount > 0 || overLimit || isAnyBulkRunning}
                onClick={onContinueToConfirm}
              >
                {t("common.next")}
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <div className="space-y-3 text-sm">
            <div
              className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning"
              aria-live="polite"
            >
              {t("events.bulk.block.confirm.explainer", {
                n: blockableEvents.length,
              })}
            </div>
            <ul className="max-h-40 overflow-y-auto rounded-md border border-border bg-muted/30 p-2 font-mono text-xs">
              {blockableEvents.map((event) => (
                <li
                  key={event.id}
                  className="truncate text-muted-foreground"
                  title={event.id}
                >
                  {event.flow_id ?? event.id}
                </li>
              ))}
            </ul>
            <label className="flex cursor-pointer items-center gap-2">
              <Checkbox
                checked={confirmAck}
                onCheckedChange={(v) => setConfirmAck(v === true)}
                aria-label={t("events.bulk.block.confirm.ack")}
              />
              <span>{t("events.bulk.block.confirm.ack")}</span>
            </label>
            <DialogFooter>
              <Button
                variant="outline"
                className="cursor-pointer"
                onClick={onClose}
              >
                {t("common.cancel")}
              </Button>
              <Button
                variant="default"
                className="cursor-pointer border-warning/40 bg-warning/10 text-warning hover:bg-warning/20"
                disabled={!runtimeAllowed || !confirmAck || isAnyBulkRunning}
                onClick={onConfirmCreate}
              >
                <ShieldAlert className="mr-1 h-3 w-3" aria-hidden />
                {t("events.bulk.block.confirm.continue")}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
