"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useState } from "react";
import {
  Boxes,
  Clock3,
  Edit3,
  Loader2,
  Plus,
  RefreshCw,
  Rocket,
  ServerCog,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";

import { EmptyState, ErrorState, LoadingState } from "@/components/async-state";
import { RuntimeStatusPanel } from "@/components/runtime-status/runtime-status-panel";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { usePipelineTargetsV3 } from "@/hooks/use-pipeline-control-v3";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { useRuntimeStatusV1 } from "@/hooks/use-runtime-status-v1";
import { useI18n } from "@/lib/i18n";
import { pipelineTargetHealth, shortIdentity, type PipelineTargetHealth } from "@/lib/pipeline-v3";
import { unavailableRuntimeProjection } from "@/lib/runtime-status-v1";
import type { PipelineTargetV3 } from "@/types/api";

function DialogLoading() {
  const { t } = useI18n();
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" role="status">
      <div className="flex items-center gap-2 rounded-lg bg-popover px-4 py-3 text-sm text-popover-foreground shadow-lg">
        <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
        <span className="sr-only">{t("pipeline.v3.loadingDialog")}</span>
      </div>
    </div>
  );
}

const TargetRegistrationDialog = dynamic(
  () => import("@/components/pipeline-v3/target-registration-dialog").then((module) => module.TargetRegistrationDialog),
  { ssr: false, loading: DialogLoading },
);

const PipelineActivationDialog = dynamic(
  () => import("@/components/pipeline-v3/pipeline-activation-dialog").then((module) => module.PipelineActivationDialog),
  { ssr: false, loading: DialogLoading },
);

const PipelineActivationHistoryDialog = dynamic(
  () => import("@/components/pipeline-v3/pipeline-activation-history-dialog").then((module) => module.PipelineActivationHistoryDialog),
  { ssr: false, loading: DialogLoading },
);

function healthVariant(health: PipelineTargetHealth) {
  if (health === "verified") return "success" as const;
  if (health === "activation_pending" || health === "unbound") return "warning" as const;
  return "destructive" as const;
}

function TargetCard({
  target,
  onEdit,
  onActivate,
  onHistory,
  writesAllowed,
}: {
  target: PipelineTargetV3;
  onEdit: () => void;
  onActivate: () => void;
  onHistory: () => void;
  writesAllowed: boolean;
}) {
  const { t, formatDateTime, formatNumber } = useI18n();
  const health = pipelineTargetHealth(target);
  return (
    <Card>
      <CardHeader className="grid-cols-[minmax(0,1fr)_auto]">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={healthVariant(health)}>{t(`pipeline.v3.health.${health}`)}</Badge>
            <Badge variant={target.enabled ? "success" : "secondary"}>{target.enabled ? t("common.active") : t("common.disabled")}</Badge>
          </div>
          <CardTitle className="truncate text-base">{target.connect_uri}</CardTitle>
          <p className="break-all font-mono text-xs text-muted-foreground">{target.target_uuid}</p>
        </div>
        <span className="text-xs tabular-nums text-muted-foreground">v{target.registration_version}</span>
      </CardHeader>
      <CardContent className="space-y-4">
        {health !== "verified" ? (
          <Alert variant={health === "activation_pending" || health === "unbound" ? "default" : "destructive"}>
            <TriangleAlert aria-hidden="true" />
            <AlertTitle>{t(`pipeline.v3.healthTitle.${health}`)}</AlertTitle>
            <AlertDescription>{t(`pipeline.v3.healthDescription.${health}`)}</AlertDescription>
          </Alert>
        ) : null}
        <dl className="grid gap-x-5 gap-y-3 text-xs sm:grid-cols-2 xl:grid-cols-4">
          <div><dt className="text-muted-foreground">{t("pipeline.v3.deviceScope")}</dt><dd className="mt-1 font-mono tabular-nums">{target.device_id} / {target.p4_role || "default"}</dd></div>
          <div><dt className="text-muted-foreground">{t("detections.v3.generation")}</dt><dd className="mt-1 font-mono tabular-nums">{formatNumber(target.runtime_generation)}</dd></div>
          <div><dt className="text-muted-foreground">{t("pipeline.v3.activeBundle")}</dt><dd className="mt-1 font-mono" title={target.active_bundle_id ?? undefined}>{shortIdentity(target.active_bundle_id)} / {target.active_variant_id ?? "—"}</dd></div>
          <div><dt className="text-muted-foreground">{t("pipeline.v3.desiredBundle")}</dt><dd className="mt-1 font-mono" title={target.desired_bundle_id ?? undefined}>{shortIdentity(target.desired_bundle_id)} / {target.desired_variant_id ?? "—"}</dd></div>
          <div><dt className="text-muted-foreground">{t("pipeline.v3.transportSecurity")}</dt><dd className="mt-1">{target.transport_security_mode}</dd></div>
          <div><dt className="text-muted-foreground">{t("pipeline.v3.pipelineCookie")}</dt><dd className="mt-1 font-mono tabular-nums">{target.active_pipeline_cookie ?? "—"}</dd></div>
          <div className="sm:col-span-2"><dt className="text-muted-foreground">{t("pipeline.v3.lastUpdated")}</dt><dd className="mt-1 tabular-nums">{formatDateTime(target.updated_at)}</dd></div>
        </dl>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={onEdit} disabled={!writesAllowed}>
            <Edit3 aria-hidden="true" />{t("pipeline.v3.editTarget")}
          </Button>
          <Button variant="outline" size="sm" onClick={onHistory}>
            <Clock3 aria-hidden="true" />{t("pipeline.v3.activationHistory")}
          </Button>
          <Button size="sm" onClick={onActivate} disabled={!writesAllowed || !target.enabled}>
            <Rocket aria-hidden="true" />{t("pipeline.v3.activate")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export default function PipelineControlV3Page() {
  const targetsQuery = usePipelineTargetsV3();
  const p4Safety = useRuntimeSafety("p4");
  const runtimeStatusQuery = useRuntimeStatusV1();
  const { t } = useI18n();
  const [targetDialog, setTargetDialog] = useState<{ key: string; target: PipelineTargetV3 | null } | null>(null);
  const [activationTarget, setActivationTarget] = useState<PipelineTargetV3 | null>(null);
  const [historyTarget, setHistoryTarget] = useState<PipelineTargetV3 | null>(null);
  const writesAllowed = p4Safety.allowed;

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-2xl font-heading font-semibold">{t("pipeline.v3.title")}</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{t("pipeline.v3.subtitle")}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void Promise.all([targetsQuery.refetch(), p4Safety.retry()])} disabled={targetsQuery.isFetching}>
          <RefreshCw className={targetsQuery.isFetching ? "animate-spin motion-reduce:animate-none" : ""} aria-hidden="true" />
          {t("common.refresh")}
        </Button>
      </header>

      <Alert>
        <ShieldCheck aria-hidden="true" />
        <AlertTitle>{t("pipeline.v3.fenceTitle")}</AlertTitle>
        <AlertDescription>{t("pipeline.v3.fenceDescription")}</AlertDescription>
      </Alert>

      {runtimeStatusQuery.isLoading ? (
        <LoadingState label={t("runtimeStatus.title")} rows={1} />
      ) : (
        <RuntimeStatusPanel
          projection={runtimeStatusQuery.data ?? unavailableRuntimeProjection()}
          roles={["p4_agent"]}
          refreshing={runtimeStatusQuery.isFetching}
          onRefresh={() => void runtimeStatusQuery.refetch()}
        />
      )}
      {runtimeStatusQuery.isError ? (
        <ErrorState
          title={t("operations.runtimeUnknown")}
          error={runtimeStatusQuery.error}
          onRetry={() => void runtimeStatusQuery.refetch()}
        />
      ) : null}

      {!writesAllowed ? (
        <Alert variant="destructive">
          <TriangleAlert aria-hidden="true" />
          <AlertTitle>{t("pipeline.v3.writesUnavailable")}</AlertTitle>
          <AlertDescription>{t(`pipeline.v3.safety.${p4Safety.reason ?? "loading"}`)}</AlertDescription>
        </Alert>
      ) : null}

      <nav className="flex flex-wrap gap-2" aria-label={t("pipeline.v3.views")}>
        <Link href="/admin/pipeline" aria-current="page" className={buttonVariants({ variant: "secondary", size: "sm" })}>
          <ServerCog aria-hidden="true" />{t("pipeline.v3.targetsTab")}
        </Link>
        <Link href="/admin/pipeline/bundles" prefetch={false} className={buttonVariants({ variant: "outline", size: "sm" })}>
          <Boxes aria-hidden="true" />{t("pipeline.v3.bundlesTab")}
        </Link>
      </nav>

        <section className="space-y-4" aria-label={t("pipeline.v3.targetsTab")}>
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold">{t("pipeline.v3.targetsTitle")}</h2>
              <p className="text-sm text-muted-foreground">{t("pipeline.v3.targetsDescription")}</p>
            </div>
            <Button size="sm" onClick={() => setTargetDialog({ key: `new-${Date.now()}`, target: null })} disabled={!writesAllowed}>
              <Plus aria-hidden="true" />{t("pipeline.v3.createTarget")}
            </Button>
          </div>
          {targetsQuery.isLoading ? <LoadingState label={t("pipeline.v3.loadingTargets")} rows={4} /> : null}
          {targetsQuery.isError ? <ErrorState error={targetsQuery.error} title={t("pipeline.v3.targetsLoadFailed")} onRetry={() => void targetsQuery.refetch()} /> : null}
          {targetsQuery.data?.length === 0 ? <EmptyState title={t("pipeline.v3.noTargets")} description={t("pipeline.v3.noTargetsDescription")} /> : null}
          {targetsQuery.data?.length ? (
            <div className="space-y-4">
              {targetsQuery.data.map((target) => (
                <TargetCard
                  key={target.target_uuid}
                  target={target}
                  writesAllowed={writesAllowed}
                  onEdit={() => setTargetDialog({ key: `${target.target_uuid}-${target.registration_version}`, target })}
                  onActivate={() => setActivationTarget(target)}
                  onHistory={() => setHistoryTarget(target)}
                />
              ))}
            </div>
          ) : null}
        </section>

      {targetDialog ? (
        <TargetRegistrationDialog
          key={targetDialog.key}
          target={targetDialog.target}
          onClose={() => setTargetDialog(null)}
          writeAllowed={writesAllowed}
          writeGuard={p4Safety.guard}
        />
      ) : null}
      {activationTarget ? (
        <PipelineActivationDialog
          key={`${activationTarget.target_uuid}-${activationTarget.registration_version}-${activationTarget.runtime_generation}`}
          target={activationTarget}
          onClose={() => setActivationTarget(null)}
          writeAllowed={writesAllowed}
          writeGuard={p4Safety.guard}
        />
      ) : null}
      {historyTarget ? (
        <PipelineActivationHistoryDialog
          key={historyTarget.target_uuid}
          target={historyTarget}
          onClose={() => setHistoryTarget(null)}
        />
      ) : null}
    </div>
  );
}
