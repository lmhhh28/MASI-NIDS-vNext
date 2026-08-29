"use client";

import { RefreshCw, TriangleAlert } from "lucide-react";

import { EmptyState, ErrorState, LoadingState } from "@/components/async-state";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { usePipelineActivationsV3 } from "@/hooks/use-pipeline-control-v3";
import { useI18n } from "@/lib/i18n";
import { shortIdentity } from "@/lib/pipeline-v3";
import type { PipelineActivationResponseV3, PipelineActivationStatus, PipelineTargetV3 } from "@/types/api";

function statusVariant(status: PipelineActivationStatus) {
  if (status === "applied") return "success" as const;
  if (status === "failed" || status === "rejected" || status === "outcome_unknown") {
    return "destructive" as const;
  }
  if (status === "superseded") return "secondary" as const;
  return "warning" as const;
}

function ActivationItem({ activation }: { activation: PipelineActivationResponseV3 }) {
  const { t, formatDateTime, formatNumber } = useI18n();
  const uncertain = activation.status === "outcome_unknown";
  const restartRequired = activation.status === "restart_required";
  return (
    <li className="space-y-3 rounded-lg border p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={statusVariant(activation.status)}>
              {t(`pipeline.v3.activationStatus.${activation.status}`)}
            </Badge>
            <span className="font-mono text-xs text-muted-foreground">
              {shortIdentity(activation.id)}
            </span>
          </div>
          <p className="break-all font-mono text-xs">
            {shortIdentity(activation.bundle_id)} / {activation.variant_id}
          </p>
        </div>
        <time className="text-xs tabular-nums text-muted-foreground" dateTime={activation.updated_at}>
          {formatDateTime(activation.updated_at)}
        </time>
      </div>

      {uncertain ? (
        <Alert variant="destructive">
          <TriangleAlert aria-hidden="true" />
          <AlertTitle>{t("pipeline.v3.activationUnknownTitle")}</AlertTitle>
          <AlertDescription>{t("pipeline.v3.activationUnknownDescription")}</AlertDescription>
        </Alert>
      ) : null}
      {restartRequired ? (
        <Alert>
          <TriangleAlert aria-hidden="true" />
          <AlertTitle>{t("runtimeStatus.state.restartRequired")}</AlertTitle>
          <AlertDescription>{t("runtimeStatus.next.controlledRestart")}</AlertDescription>
        </Alert>
      ) : null}

      <dl className="grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <dt className="text-muted-foreground">{t("pipeline.v3.activationAttempts")}</dt>
          <dd className="mt-1 font-mono tabular-nums">{formatNumber(activation.attempt_count)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t("detections.v3.generation")}</dt>
          <dd className="mt-1 font-mono tabular-nums">{formatNumber(activation.expected_runtime_generation)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t("pipeline.v3.observationReady")}</dt>
          <dd className="mt-1">{activation.observation_ready === true ? t("pipeline.v3.yes") : activation.observation_ready === false ? t("pipeline.v3.no") : "—"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t("pipeline.v3.configuredDigests")}</dt>
          <dd className="mt-1 font-mono tabular-nums">{formatNumber(activation.configured_digest_count)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t("pipeline.v3.lastAttempt")}</dt>
          <dd className="mt-1 tabular-nums">{formatDateTime(activation.last_attempt_at)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t("pipeline.v3.nextRetry")}</dt>
          <dd className="mt-1 tabular-nums">{formatDateTime(activation.next_retry_at)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t("pipeline.v3.observedCookie")}</dt>
          <dd className="mt-1 font-mono tabular-nums">{activation.observed_pipeline_cookie ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t("pipeline.v3.agentSession")}</dt>
          <dd className="mt-1 font-mono tabular-nums">{formatNumber(activation.observed_agent_session_version)}</dd>
        </div>
      </dl>
      {activation.observed_live_p4info_sha256 ? (
        <p className="break-all font-mono text-xs text-muted-foreground" title={activation.observed_live_p4info_sha256}>
          {t("pipeline.v3.liveP4Info")}: {shortIdentity(activation.observed_live_p4info_sha256)}
        </p>
      ) : null}
      {activation.error_code ? (
        <p className="break-all rounded-md bg-destructive/10 px-3 py-2 font-mono text-xs text-destructive">
          {activation.error_code}
        </p>
      ) : null}
    </li>
  );
}

export function PipelineActivationHistoryDialog({
  target,
  onClose,
}: {
  target: PipelineTargetV3;
  onClose: () => void;
}) {
  const query = usePipelineActivationsV3(target.target_uuid);
  const { t } = useI18n();
  return (
    <Dialog open onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{t("pipeline.v3.activationHistoryTitle")}</DialogTitle>
          <DialogDescription>
            {t("pipeline.v3.activationHistoryDescription", { target: shortIdentity(target.target_uuid) })}
          </DialogDescription>
        </DialogHeader>
        <div className="flex justify-end">
          <Button type="button" variant="outline" size="sm" onClick={() => void query.refetch()} disabled={query.isFetching}>
            <RefreshCw className={query.isFetching ? "animate-spin motion-reduce:animate-none" : ""} aria-hidden="true" />
            {t("common.refresh")}
          </Button>
        </div>
        <div aria-live="polite">
          {query.isLoading ? <LoadingState label={t("pipeline.v3.loadingActivations")} rows={3} /> : null}
          {query.isError ? <ErrorState error={query.error} title={t("pipeline.v3.activationsLoadFailed")} onRetry={() => void query.refetch()} /> : null}
          {query.data?.length === 0 ? (
            <EmptyState title={t("pipeline.v3.noActivations")} description={t("pipeline.v3.noActivationsDescription")} />
          ) : null}
          {query.data?.length ? (
            <ol className="max-h-[60dvh] space-y-3 overflow-y-auto pr-1">
              {query.data.map((activation) => <ActivationItem key={activation.id} activation={activation} />)}
            </ol>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
