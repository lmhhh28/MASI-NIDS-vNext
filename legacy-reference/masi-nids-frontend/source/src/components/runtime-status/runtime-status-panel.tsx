"use client";

import { Activity, DatabaseZap, RefreshCw, ServerCog, TriangleAlert } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useI18n } from "@/lib/i18n";
import type { TranslationKey } from "@/lib/messages";
import { metadataFor, runtimeConsoleStatus } from "@/lib/status-metadata";
import type {
  RuntimeConsoleProjection,
  RuntimeConsoleState,
  RuntimeRoleProjection,
} from "@/lib/runtime-status-v1";
import type { RuntimeStatusRole } from "@/types/api";

const ROLE_LABELS: Record<RuntimeStatusRole, TranslationKey> = {
  telemetry_v3: "runtimeStatus.role.telemetry",
  inference_v3: "runtimeStatus.role.inference",
  p4_agent: "runtimeStatus.role.agent",
};
const STATE_LABELS: Record<RuntimeConsoleState, TranslationKey> = {
  operational: "runtimeStatus.state.operational",
  hold: "runtimeStatus.state.hold",
  degraded: "runtimeStatus.state.degraded",
  unknown: "runtimeStatus.state.unknown",
  restart_required: "runtimeStatus.state.restartRequired",
  failed: "runtimeStatus.state.failed",
};
const NEXT_STEP_LABELS: Record<RuntimeConsoleState, TranslationKey> = {
  operational: "runtimeStatus.next.monitor",
  hold: "runtimeStatus.next.inspectHold",
  degraded: "runtimeStatus.next.restoreQuality",
  unknown: "runtimeStatus.next.restoreHeartbeat",
  restart_required: "runtimeStatus.next.controlledRestart",
  failed: "runtimeStatus.next.inspectFailure",
};

function stateVariant(state: RuntimeConsoleState) {
  const tone = metadataFor(runtimeConsoleStatus, state).tone;
  if (tone === "success") return "success" as const;
  if (tone === "danger") return "destructive" as const;
  if (tone === "warning") return "warning" as const;
  return "secondary" as const;
}

function Fact({ label, value, mono = true }: { label: string; value: string | number | null; mono?: boolean }) {
  const { t } = useI18n();
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={`mt-1 break-all text-sm ${mono ? "font-mono tabular-nums" : ""}`}>
        {value === null ? t("runtimeStatus.unmeasured") : value}
      </dd>
    </div>
  );
}

function TelemetryFacts({ role }: { role: RuntimeRoleProjection }) {
  const { t } = useI18n();
  const metrics = role.metrics?.kind === "telemetry_v3" ? role.metrics : null;
  if (!metrics) return null;
  return (
    <dl className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Fact label={t("runtimeStatus.metric.generation")} value={metrics.runtime_generation} />
      <Fact label={t("runtimeStatus.metric.bankAge")} value={`${metrics.bank_age_seconds.toFixed(3)} s`} />
      <Fact label={t("runtimeStatus.metric.rotationLateness")} value={`${metrics.rotation_lateness_seconds.toFixed(3)} s`} />
      <Fact label={t("runtimeStatus.metric.rotationReady")} value={metrics.next_rotation_ready ? t("pipeline.v3.yes") : t("pipeline.v3.no")} mono={false} />
      <Fact label={t("runtimeStatus.metric.output")} value={`${metrics.output_bytes} / ${metrics.output_capacity_bytes} B`} />
      <Fact label={t("runtimeStatus.metric.outputHorizon")} value={`${metrics.output_horizon_seconds} s`} />
      <Fact label={t("runtimeStatus.metric.activeSegment")} value={metrics.active_segment_sequence} />
      <Fact label={t("runtimeStatus.metric.segmentBytes")} value={metrics.active_segment_bytes} />
      <Fact label={t("runtimeStatus.metric.segmentRotations")} value={metrics.active_segment_rotations} />
      <Fact label={t("runtimeStatus.metric.oldestUnacked")} value={metrics.oldest_unacknowledged_sequence} />
      <Fact label={t("runtimeStatus.metric.unackedSegments")} value={metrics.unacknowledged_segments} />
      <Fact label={t("runtimeStatus.metric.unackedBytes")} value={metrics.unacknowledged_bytes} />
      <Fact label={t("runtimeStatus.metric.ackHighWater")} value={metrics.ack_high_water_sequence} />
      <Fact label={t("runtimeStatus.metric.deletionCandidates")} value={metrics.deletion_candidates} />
      <Fact label={t("runtimeStatus.metric.handoffPhase")} value={metrics.segment_handoff_phase} />
      <Fact label={t("runtimeStatus.metric.quality")} value={metrics.quality} mono={false} />
    </dl>
  );
}

function InferenceFacts({ role }: { role: RuntimeRoleProjection }) {
  const { t } = useI18n();
  const metrics = role.metrics?.kind === "inference_v3" ? role.metrics : null;
  if (!metrics) return null;
  return (
    <dl className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Fact label={t("runtimeStatus.metric.sourceLag")} value={metrics.source_lag_records} />
      <Fact label={t("runtimeStatus.metric.wal")} value={`${metrics.wal_bytes} / ${metrics.wal_capacity_bytes} B`} />
      <Fact label={t("runtimeStatus.metric.queue")} value={`${metrics.queue_depth} / ${metrics.queue_capacity}`} />
      <Fact label={t("runtimeStatus.metric.checkpoint")} value={metrics.checkpoint_sequence} />
      <Fact label={t("runtimeStatus.metric.modelRelease")} value={metrics.model_release_id} />
      <Fact label={t("runtimeStatus.metric.modelScope")} value={metrics.model_scope_id} />
      <Fact label={t("runtimeStatus.metric.modelGate")} value={metrics.model_gate.toUpperCase()} mono={false} />
      <Fact label={t("runtimeStatus.metric.eventRetries")} value={metrics.event_retry_count} />
      <Fact label={t("runtimeStatus.metric.fsyncRate")} value={metrics.fsync_rate_hz === null ? null : `${metrics.fsync_rate_hz} Hz`} />
    </dl>
  );
}

function AgentFacts({ role }: { role: RuntimeRoleProjection }) {
  const { t } = useI18n();
  const metrics = role.metrics?.kind === "p4_agent" ? role.metrics : null;
  if (!metrics) return null;
  return (
    <dl className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Fact label={t("runtimeStatus.metric.primary")} value={metrics.primary ? t("pipeline.v3.yes") : t("pipeline.v3.no")} mono={false} />
      <Fact label={t("runtimeStatus.metric.session")} value={metrics.session_state} mono={false} />
      <Fact label={t("runtimeStatus.metric.generation")} value={metrics.generation} />
      <Fact label={t("runtimeStatus.metric.operation")} value={metrics.operation_state} mono={false} />
      <Fact label={t("runtimeStatus.metric.journal")} value={`${metrics.journal_operations} / ${metrics.journal_bytes} B`} />
      <Fact label={t("runtimeStatus.metric.bulkQueue")} value={metrics.bulk_queue_depth} />
      <Fact label={t("runtimeStatus.metric.controlWaiters")} value={metrics.control_waiters} />
      <Fact label={t("runtimeStatus.metric.rpc")} value={metrics.p4_rpc_entity} />
      <Fact label={t("runtimeStatus.metric.rpcLatency")} value={metrics.p4_rpc_latency_ms === null ? null : `${metrics.p4_rpc_latency_ms} ms`} />
    </dl>
  );
}

export function RuntimeRoleCard({ role, compact = false }: { role: RuntimeRoleProjection; compact?: boolean }) {
  const { t, formatDateTime } = useI18n();
  const Icon = role.role === "telemetry_v3" ? Activity : role.role === "inference_v3" ? DatabaseZap : ServerCog;
  return (
    <Card data-runtime-role={role.role}>
      <CardHeader className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-base"><Icon className="size-4" aria-hidden="true" />{t(ROLE_LABELS[role.role])}</CardTitle>
          <div className="flex flex-wrap gap-2">
            <Badge variant={stateVariant(role.state)}>{t(STATE_LABELS[role.state])}</Badge>
            <Badge variant="warning">{t("runtimeStatus.releaseHold")}</Badge>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">
          {t("runtimeStatus.freshness")}: {t(`runtimeStatus.freshness.${role.freshness}` as TranslationKey)} · {t("runtimeStatus.observed")}: {formatDateTime(role.observedAt)}
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {role.reasonCodes.length ? (
          <ul className="space-y-1" aria-label={t("runtimeStatus.reasons")}>
            {role.reasonCodes.map((code) => <li key={code} className="break-all font-mono text-xs text-warning">{code}</li>)}
          </ul>
        ) : <p className="text-xs text-muted-foreground">{t("runtimeStatus.noRoleReasons")}</p>}
        <p className="text-sm"><span className="text-muted-foreground">{t("runtimeStatus.nextStep")}: </span>{t(NEXT_STEP_LABELS[role.state])}</p>
        {!compact ? (
          <>
            {role.role === "telemetry_v3" ? <TelemetryFacts role={role} /> : null}
            {role.role === "inference_v3" ? <InferenceFacts role={role} /> : null}
            {role.role === "p4_agent" ? <AgentFacts role={role} /> : null}
            <details className="text-xs">
              <summary className="cursor-pointer font-medium">{t("runtimeStatus.identity")}</summary>
              <dl className="mt-3 grid gap-3 sm:grid-cols-2">
                <Fact label={t("runtimeStatus.processInstance")} value={role.processInstanceId} />
                <Fact label={t("runtimeStatus.configId")} value={role.configId} />
                <Fact label={t("runtimeStatus.sequence")} value={role.sequence} />
                <Fact label={t("runtimeStatus.expires")} value={role.expiresAt ? formatDateTime(role.expiresAt) : null} mono={false} />
              </dl>
            </details>
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function RuntimeStatusPanel({
  projection,
  roles = ["telemetry_v3", "inference_v3", "p4_agent"],
  compact = false,
  refreshing = false,
  onRefresh,
}: {
  projection: RuntimeConsoleProjection;
  roles?: readonly RuntimeStatusRole[];
  compact?: boolean;
  refreshing?: boolean;
  onRefresh?: () => void;
}) {
  const { t, formatDateTime } = useI18n();
  const quiescenceUnsupported = projection.roles.telemetry_v3.reasonCodes.includes("DATAPLANE_QUIESCENCE_UNPROVEN");
  return (
    <section className="space-y-4" aria-labelledby="runtime-status-heading">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 id="runtime-status-heading" className="text-lg font-semibold">{t("runtimeStatus.title")}</h2>
          <p className="text-sm text-muted-foreground">{t("runtimeStatus.subtitle")}</p>
          <p className="mt-1 text-xs text-muted-foreground">{t("runtimeStatus.generated")}: {formatDateTime(projection.generatedAt)}</p>
        </div>
        {onRefresh ? (
          <Button type="button" variant="outline" className="min-h-11 sm:min-h-9" onClick={onRefresh} disabled={refreshing}>
            <RefreshCw className={refreshing ? "animate-spin motion-reduce:animate-none" : ""} aria-hidden="true" />{t("common.refresh")}
          </Button>
        ) : null}
      </div>
      <Alert variant="destructive">
        <TriangleAlert aria-hidden="true" />
        <AlertTitle>{t("runtimeStatus.qualificationHoldTitle")}</AlertTitle>
        <AlertDescription className="break-words">{t("runtimeStatus.qualificationHoldDescription")} <span className="font-mono">{projection.qualificationReason}</span></AlertDescription>
      </Alert>
      {quiescenceUnsupported ? (
        <Alert>
          <TriangleAlert aria-hidden="true" />
          <AlertTitle>{t("runtimeStatus.quiescenceUnsupportedTitle")}</AlertTitle>
          <AlertDescription>{t("runtimeStatus.quiescenceUnsupportedDescription")}</AlertDescription>
        </Alert>
      ) : null}
      <div className={`grid gap-4 ${compact ? "md:grid-cols-3" : ""}`} aria-live="polite">
        {roles.map((role) => <RuntimeRoleCard key={role} role={projection.roles[role]} compact={compact} />)}
      </div>
    </section>
  );
}
