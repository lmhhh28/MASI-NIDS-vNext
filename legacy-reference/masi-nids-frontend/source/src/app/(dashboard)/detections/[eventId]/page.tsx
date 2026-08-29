"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, Braces, Fingerprint, Network, ShieldCheck, Workflow } from "lucide-react";

import { ErrorState, LoadingState } from "@/components/async-state";
import { FlowCapturePanel } from "@/components/evidence/flow-capture-panel";
import { FlowEvidenceActionPanel } from "@/components/evidence/flow-evidence-action-panel";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAdmitEventV4Workflow, useEventV3 } from "@/hooks/use-events-v3";
import { useFlowEvidenceV3 } from "@/hooks/use-flow-evidence-v3";
import { useI18n } from "@/lib/i18n";

export default function EventV3DetailPage() {
  const params = useParams<{ eventId: string }>();
  const eventId = params.eventId;
  const eventQuery = useEventV3(eventId);
  const flowEvidenceQuery = useFlowEvidenceV3(eventId);
  const admission = useAdmitEventV4Workflow(eventId);
  const { t, locale, formatDateTime, formatNumber } = useI18n();

  if (eventQuery.isLoading) return <LoadingState label={t("detections.v3.loadingEventDetail")} rows={5} />;
  if (eventQuery.isError || !eventQuery.data) {
    return <ErrorState error={eventQuery.error} title={t("detections.v3.eventDetailLoadFailed")} onRetry={() => void eventQuery.refetch()} />;
  }

  const event = eventQuery.data;
  const flowEvidenceItems = flowEvidenceQuery.data?.items ?? [];
  const primaryEvidence = flowEvidenceItems.find((item) => item.status === "active") ?? flowEvidenceItems[0] ?? null;
  const workflowAdmission = event.workflow_admission ?? {
    eligible: false,
    policy_id: "masi.event-v4-workflow-admission.v1" as const,
    template_name: "inspect_only" as const,
    reason_code: "EVENT_V4_WORKFLOW_CAPABILITY_MISSING",
  };
  return (
    <div className="mx-auto w-full max-w-6xl space-y-6">
      <header className="space-y-3">
        <Link href="/detections" prefetch={false} className={buttonVariants({ variant: "ghost", size: "sm" })}>
          <ArrowLeft aria-hidden="true" />{t("detections.v3.backToEvents")}
        </Link>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h1 className="text-2xl font-heading font-semibold">{t("detections.v3.eventDetailTitle")}</h1>
            <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{event.event_id}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge variant={event.decision === "anomaly" ? "destructive" : event.decision === "normal" ? "success" : "warning"}>{event.decision}</Badge>
            <Badge variant="outline">{event.event_kind}</Badge>
            <Badge variant="outline">{event.model_role}</Badge>
          </div>
        </div>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Workflow className="h-4 w-4" aria-hidden="true" />
            {t("detections.v3.workflowAdmission")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            {t("detections.v3.workflowAdmissionDescription")}
          </p>
          {!workflowAdmission.eligible && (
            <p className="break-all font-mono text-xs text-warning" role="status">
              {t("detections.v3.workflowAdmissionBlocked")}: {workflowAdmission.reason_code ?? "UNKNOWN"}
            </p>
          )}
          {admission.isError && (
            <p className="text-sm text-destructive" role="alert">
              {admission.error.message === "WORKFLOW_ADMISSION_OUTCOME_UNKNOWN"
                ? t("detections.v3.workflowAdmissionUnknown")
                : admission.error.message}
            </p>
          )}
          {admission.data ? (
            <div className="flex flex-wrap items-center gap-3" aria-live="polite">
              <Badge variant="success">{admission.data.status}</Badge>
              <Link
                href={`/workflows/${admission.data.id}`}
                prefetch={false}
                className={buttonVariants({ variant: "outline", size: "sm" })}
              >
                {t("detections.v3.openWorkflow")}
              </Link>
            </div>
          ) : (
            <Button
              type="button"
              className="min-h-11"
              onClick={() => admission.mutate({ locale })}
              disabled={!workflowAdmission.eligible || admission.isPending}
            >
              {admission.isPending
                ? t("detections.v3.workflowAdmissionPending")
                : admission.isError && admission.error.message === "WORKFLOW_ADMISSION_OUTCOME_UNKNOWN"
                  ? t("detections.v3.workflowAdmissionCheck")
                  : t("detections.v3.startWorkflow")}
            </Button>
          )}
          <p className="text-xs text-muted-foreground">
            {t("detections.v3.workflowAdmissionSafety")}
          </p>
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2"><Network className="h-4 w-4" aria-hidden="true" />{t("detections.v3.targetWindow")}</CardTitle></CardHeader>
          <CardContent>
            <dl className="space-y-3 text-sm">
              <div><dt className="text-muted-foreground">{t("detections.v3.target")}</dt><dd className="mt-1 break-all font-mono text-xs">{event.target_uuid}</dd></div>
              <div><dt className="text-muted-foreground">{t("detections.v3.destination")}</dt><dd className="mt-1 font-mono">{event.dst_ip}:{event.dst_port ?? "*"} / {event.ip_protocol}</dd></div>
              <div><dt className="text-muted-foreground">{t("detections.v3.window")}</dt><dd className="mt-1 tabular-nums">{formatDateTime(event.window_start_at)} – {formatDateTime(event.window_end_at)}</dd></div>
              <div><dt className="text-muted-foreground">{t("detections.v3.sequence")}</dt><dd className="mt-1 font-mono tabular-nums">{formatNumber(event.window_sequence)}</dd></div>
            </dl>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2"><ShieldCheck className="h-4 w-4" aria-hidden="true" />{t("detections.v3.modelDecision")}</CardTitle></CardHeader>
          <CardContent>
            <dl className="space-y-3 text-sm">
              <div><dt className="text-muted-foreground">{t("detections.v3.nearestClass")}</dt><dd className="mt-1">{event.nearest_class}</dd></div>
              <div><dt className="text-muted-foreground">{t("detections.v3.compatibility")}</dt><dd className="mt-1 tabular-nums">{formatNumber(event.compatibility, { style: "percent", maximumFractionDigits: 2 })}</dd></div>
              <div><dt className="text-muted-foreground">{t("detections.v3.completeness")}</dt><dd className="mt-1 tabular-nums">{formatNumber(event.completeness, { style: "percent", maximumFractionDigits: 2 })}</dd></div>
              <div><dt className="text-muted-foreground">{t("detections.v3.rejectReason")}</dt><dd className="mt-1 break-words">{event.reject_reason || "—"}</dd></div>
            </dl>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2"><Fingerprint className="h-4 w-4" aria-hidden="true" />{t("detections.v3.provenance")}</CardTitle></CardHeader>
          <CardContent>
            <dl className="space-y-3 text-sm">
              <div><dt className="text-muted-foreground">{t("detections.v3.generation")}</dt><dd className="mt-1 font-mono tabular-nums">{formatNumber(event.runtime_generation)}</dd></div>
              <div><dt className="text-muted-foreground">{t("pipeline.v3.bundle")}</dt><dd className="mt-1 break-all font-mono text-xs">{event.pipeline_bundle_id} / {event.bundle_variant_id}</dd></div>
              <div><dt className="text-muted-foreground">{t("detections.v3.featureContract")}</dt><dd className="mt-1 break-all font-mono text-xs">{event.feature_contract_id}</dd></div>
              <div><dt className="text-muted-foreground">{t("detections.v3.modelRelease")}</dt><dd className="mt-1 break-all font-mono text-xs">{event.model_release_id}</dd></div>
            </dl>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <FlowCapturePanel event={event} evidence={primaryEvidence} />
        <FlowEvidenceActionPanel items={flowEvidenceItems} />
      </div>
      {flowEvidenceQuery.isError ? (
        <p className="text-xs text-destructive" role="alert">
          {flowEvidenceQuery.error.message}
        </p>
      ) : null}

      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2"><Braces className="h-4 w-4" aria-hidden="true" />{t("detections.v3.rawEvent")}</CardTitle></CardHeader>
        <CardContent>
          <pre className="max-h-[36rem] overflow-auto rounded-lg border bg-muted/40 p-4 text-xs leading-relaxed" tabIndex={0}>
            {JSON.stringify(event.event, null, 2)}
          </pre>
        </CardContent>
      </Card>
    </div>
  );
}
