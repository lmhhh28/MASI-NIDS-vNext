"use client";

import { Suspense, useState } from "react";
import { TriangleAlert } from "lucide-react";

import { EmptyState, ErrorState, LoadingState } from "@/components/async-state";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { useIncidentsV3 } from "@/hooks/use-events-v3";
import { useI18n } from "@/lib/i18n";
import { shortIdentity } from "@/lib/pipeline-v3";
import type { IncidentV3, IncidentV3Status } from "@/types/api";
import {
  DetectionsV3ListShell,
  useDetectionsTargetFilter,
} from "../detections-v3-list-shell";

function incidentVariant(status: IncidentV3Status) {
  if (status === "recovered") return "success" as const;
  if (status === "active") return "destructive" as const;
  return "warning" as const;
}

function IncidentCard({ incident }: { incident: IncidentV3 }) {
  const { t, formatDateTime, formatNumber } = useI18n();
  return (
    <Card size="sm">
      <CardHeader className="grid-cols-[minmax(0,1fr)_auto]">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={incidentVariant(incident.status)}>{incident.status}</Badge>
            <Badge variant="outline">{incident.model_role}</Badge>
          </div>
          <CardTitle className="truncate text-sm">
            {incident.active_class || t("common.unknown")} · {incident.protected_target_id}
          </CardTitle>
        </div>
        <span className="text-xs tabular-nums text-muted-foreground">{formatDateTime(incident.last_event_at)}</span>
      </CardHeader>
      <CardContent>
        <dl className="grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2 xl:grid-cols-4">
          <div><dt className="text-muted-foreground">{t("detections.v3.incidentId")}</dt><dd className="mt-0.5 font-mono" title={incident.incident_id}>{shortIdentity(incident.incident_id)}</dd></div>
          <div><dt className="text-muted-foreground">{t("detections.v3.target")}</dt><dd className="mt-0.5 font-mono" title={incident.target_uuid}>{shortIdentity(incident.target_uuid)}</dd></div>
          <div><dt className="text-muted-foreground">{t("detections.v3.generation")}</dt><dd className="mt-0.5 font-mono tabular-nums">{formatNumber(incident.runtime_generation)}</dd></div>
          <div><dt className="text-muted-foreground">{t("detections.v3.eventCount")}</dt><dd className="mt-0.5 tabular-nums">{formatNumber(incident.event_count)}</dd></div>
        </dl>
      </CardContent>
    </Card>
  );
}

function IncidentsV3Content() {
  const { t, formatTime } = useI18n();
  const [status, setStatus] = useState<IncidentV3Status | "all">("all");
  const targetFilter = useDetectionsTargetFilter();
  const incidentsQuery = useIncidentsV3({
    status,
    targetUuid: targetFilter.appliedTargetUuid,
    limit: 100,
  });

  return (
    <DetectionsV3ListShell
      activeView="incidents"
      filter={targetFilter}
      isRefreshing={incidentsQuery.isFetching}
      onRefresh={() => void incidentsQuery.refetch()}
    >
      <section className="space-y-4" aria-label={t("detections.v3.incidentsTab")}>
        <div className="flex flex-col gap-3 rounded-xl border bg-card p-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="space-y-1.5">
            <Label htmlFor="incident-v3-status">{t("common.status")}</Label>
            <select id="incident-v3-status" value={status} onChange={(event) => setStatus(event.target.value as IncidentV3Status | "all")} className="min-h-11 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50 sm:min-h-9 sm:w-48">
              <option value="all">{t("common.all")}</option>
              <option value="active">active</option>
              <option value="unknown">unknown</option>
              <option value="recovered">recovered</option>
            </select>
          </div>
          {incidentsQuery.dataUpdatedAt ? <p className="text-xs text-muted-foreground" aria-live="polite">{incidentsQuery.isFetching ? t("detections.v3.refreshing") : t("detections.v3.updatedAt", { time: formatTime(incidentsQuery.dataUpdatedAt) })}</p> : null}
        </div>
        {incidentsQuery.isLoading ? <LoadingState label={t("detections.v3.loadingIncidents")} rows={4} /> : null}
        {incidentsQuery.isError && !incidentsQuery.data ? <ErrorState error={incidentsQuery.error} title={t("detections.v3.incidentsLoadFailed")} onRetry={() => void incidentsQuery.refetch()} /> : null}
        {incidentsQuery.isError && incidentsQuery.data ? <Alert variant="destructive"><TriangleAlert aria-hidden="true" /><AlertTitle>{t("detections.v3.staleData")}</AlertTitle><AlertDescription>{t("detections.v3.staleDataDescription")}</AlertDescription></Alert> : null}
        {incidentsQuery.data?.length === 0 ? <EmptyState title={t("detections.v3.noIncidents")} description={t("detections.v3.noIncidentsDescription")} /> : null}
        {incidentsQuery.data?.length ? <div className="space-y-3">{incidentsQuery.data.map((incident) => <IncidentCard key={incident.incident_id} incident={incident} />)}</div> : null}
      </section>
    </DetectionsV3ListShell>
  );
}

export default function IncidentsV3Page() {
  return (
    <Suspense fallback={<LoadingState rows={4} />}>
      <IncidentsV3Content />
    </Suspense>
  );
}
