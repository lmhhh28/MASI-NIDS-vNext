"use client";

import { useMemo, useState } from "react";
import { Loader2, Rocket, TriangleAlert } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useActivatePipelineV3, usePipelineBundlesV3 } from "@/hooks/use-pipeline-control-v3";
import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import {
  pipelineTargetCanHotActivate,
  pipelineTargetHotExecutorCompatible,
  pipelineVariantActivationEligible,
  shortIdentity,
} from "@/lib/pipeline-v3";
import type { PipelineTargetV3 } from "@/types/api";

export function PipelineActivationDialog({
  target,
  onClose,
  writeAllowed,
  writeGuard,
}: {
  target: PipelineTargetV3;
  onClose: () => void;
  writeAllowed: boolean;
  writeGuard: () => boolean;
}) {
  const bundlesQuery = usePipelineBundlesV3();
  const hotActivationReady = pipelineTargetCanHotActivate(target);
  const hotExecutorCompatible = pipelineTargetHotExecutorCompatible(target);
  const options = useMemo(
    () => (bundlesQuery.data ?? []).flatMap((bundle) => bundle.status === "available" ? bundle.variants.map((variant) => ({
      bundle,
      variant,
      value: `${bundle.bundle_id}:${variant.variant_id}`,
      eligible: pipelineVariantActivationEligible(target, variant),
    })) : []),
    [bundlesQuery.data, target],
  );
  const desiredValue = target.desired_bundle_id && target.desired_variant_id
    ? `${target.desired_bundle_id}:${target.desired_variant_id}`
    : "";
  const [selection, setSelection] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const mutation = useActivatePipelineV3();
  const { t, locale } = useI18n();
  const effectiveSelection = selection || (
    options.some((option) => option.value === desiredValue && option.eligible)
      ? desiredValue
      : options.find((option) => option.eligible)?.value ?? ""
  );
  const selected = options.find((option) => option.value === effectiveSelection);

  async function submit() {
    if (!writeAllowed || !writeGuard() || !target.enabled || !selected?.eligible || !acknowledged) return;
    setFormError(null);
    try {
      const result = await mutation.mutateAsync({
        targetUuid: target.target_uuid,
        payload: {
          schema_version: 1,
          bundle_id: selected.bundle.bundle_id,
          variant_id: selected.variant.variant_id,
        },
        idempotencyKey: `ui-activation-${crypto.randomUUID()}`,
      });
      toast.success(t("pipeline.v3.activationAccepted"), { description: result.status });
      onClose();
    } catch (error) {
      setFormError(apiErrorMessage(error, locale));
    }
  }

  return (
    <Dialog open onOpenChange={(next) => { if (!next && !mutation.isPending) onClose(); }}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("pipeline.v3.activateTitle")}</DialogTitle>
          <DialogDescription>{t("pipeline.v3.activateDescription", { target: shortIdentity(target.target_uuid) })}</DialogDescription>
        </DialogHeader>
        {bundlesQuery.isLoading ? (
          <div className="flex min-h-32 items-center justify-center" role="status">
            <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            <span className="ml-2 text-sm text-muted-foreground">{t("pipeline.v3.loadingBundles")}</span>
          </div>
        ) : bundlesQuery.isError ? (
          <Alert variant="destructive"><AlertDescription>{apiErrorMessage(bundlesQuery.error, locale)}</AlertDescription></Alert>
        ) : options.length ? (
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="pipeline-activation-variant">{t("pipeline.v3.bundleVariant")}</Label>
              <Select value={effectiveSelection} onValueChange={(value) => { if (value) { setSelection(value); setAcknowledged(false); setFormError(null); } }}>
                <SelectTrigger id="pipeline-activation-variant">
                  <SelectValue placeholder={t("pipeline.v3.noEligibleVariantShort")} />
                </SelectTrigger>
                <SelectContent>
                  {options.map(({ bundle, variant, value, eligible }) => (
                    <SelectItem key={value} value={value} disabled={!eligible}>
                      {bundle.bundle_name} {bundle.bundle_version} · {variant.variant_id}
                      {!eligible ? ` · ${t(hotActivationReady && !hotExecutorCompatible ? "pipeline.v3.hotExecutorUnsupportedShort" : "pipeline.v3.hotRequiresVerifiedShort")}` : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {!hotActivationReady ? (
              <Alert>
                <TriangleAlert aria-hidden="true" />
                <AlertTitle>{t("pipeline.v3.hotRequiresVerified")}</AlertTitle>
                <AlertDescription>{t("pipeline.v3.hotRequiresVerifiedDescription")}</AlertDescription>
              </Alert>
            ) : null}
            {hotActivationReady && !hotExecutorCompatible ? (
              <Alert>
                <TriangleAlert aria-hidden="true" />
                <AlertTitle>{t("pipeline.v3.hotExecutorUnsupported")}</AlertTitle>
                <AlertDescription>{t("pipeline.v3.hotExecutorUnsupportedDescription")}</AlertDescription>
              </Alert>
            ) : null}
            {selected ? (
              <>
                <dl className="grid gap-3 rounded-lg border bg-muted/25 p-3 text-xs sm:grid-cols-2">
                  <div><dt className="text-muted-foreground">{t("pipeline.v3.activationMode")}</dt><dd className="mt-1 font-mono">{selected.variant.activation_mode}</dd></div>
                  <div><dt className="text-muted-foreground">{t("pipeline.v3.pipelineCookie")}</dt><dd className="mt-1 font-mono tabular-nums">{selected.variant.pipeline_cookie}</dd></div>
                </dl>
                <Alert>
                  <TriangleAlert aria-hidden="true" />
                  <AlertTitle>{selected.variant.activation_mode === "immutable_cold_boot" ? t("pipeline.v3.restartRequired") : t("pipeline.v3.activationFence")}</AlertTitle>
                  <AlertDescription>{selected.variant.activation_mode === "immutable_cold_boot" ? t("pipeline.v3.restartRequiredDescription") : t("pipeline.v3.activationFenceDescription")}</AlertDescription>
                </Alert>
                <div className="flex items-start gap-3 rounded-lg border p-3">
                  <Checkbox id="pipeline-activation-ack" checked={acknowledged} onCheckedChange={(checked) => setAcknowledged(checked === true)} />
                  <Label htmlFor="pipeline-activation-ack" className="cursor-pointer text-sm leading-5">{t("pipeline.v3.activationAcknowledge")}</Label>
                </div>
              </>
            ) : (
              <Alert variant="destructive">
                <AlertTitle>{t("pipeline.v3.noEligibleVariant")}</AlertTitle>
                <AlertDescription>{t("pipeline.v3.noEligibleVariantDescription")}</AlertDescription>
              </Alert>
            )}
            {formError ? <Alert variant="destructive"><AlertDescription>{formError}</AlertDescription></Alert> : null}
          </div>
        ) : (
          <Alert variant="destructive"><AlertDescription>{t("pipeline.v3.noAvailableVariants")}</AlertDescription></Alert>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={mutation.isPending}>{t("common.cancel")}</Button>
          <Button onClick={() => void submit()} disabled={!writeAllowed || mutation.isPending || !selected?.eligible || !acknowledged || !target.enabled}>
            {mutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Rocket aria-hidden="true" />}
            {t("pipeline.v3.requestActivation")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
