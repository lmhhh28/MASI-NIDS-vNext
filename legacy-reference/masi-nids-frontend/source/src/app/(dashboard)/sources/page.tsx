"use client";

import { Database } from "lucide-react";

import { ErrorState, LoadingState } from "@/components/async-state";
import { RuntimeStatusPanel } from "@/components/runtime-status/runtime-status-panel";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useEventSources } from "@/hooks/use-events";
import { useRuntimeStatusV1 } from "@/hooks/use-runtime-status-v1";
import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { unavailableRuntimeProjection } from "@/lib/runtime-status-v1";
import type { EventSourceResponse } from "@/types/api";

function SourceRow({ source }: { source: EventSourceResponse }) {
  const { t, formatDate } = useI18n();
  return (
    <TableRow className="block rounded-lg border border-border bg-card p-4 hover:bg-secondary/20 md:table-row md:rounded-none md:border-x-0 md:border-t-0 md:bg-transparent md:p-0">
      <TableCell className="flex items-center justify-between gap-4 px-0 py-2 font-medium md:table-cell md:px-2">
        <span className="text-xs text-muted-foreground md:hidden">{t("common.name")}</span>
        <span className="min-w-0 break-words text-right md:text-left">{source.name}</span>
      </TableCell>
      <TableCell className="flex min-w-0 items-start justify-between gap-4 px-0 py-2 md:table-cell md:max-w-[200px] md:px-2">
        <span className="shrink-0 text-xs text-muted-foreground md:hidden">
          {t("dashboard.endpoint")}
        </span>
        <span
          className="min-w-0 break-all text-right font-mono text-xs text-muted-foreground md:block md:truncate md:text-left"
          title={source.endpoint}
        >
          {source.endpoint}
        </span>
      </TableCell>
      <TableCell className="flex items-center justify-between gap-4 px-0 py-2 md:table-cell md:px-2">
        <span className="text-xs text-muted-foreground md:hidden">{t("dashboard.mode")}</span>
        <Badge variant="outline">{source.mode}</Badge>
      </TableCell>
      <TableCell className="flex items-center justify-between gap-4 px-0 py-2 md:table-cell md:px-2">
        <span className="text-xs text-muted-foreground md:hidden">
          {t("sources.loopbackOnly")}
        </span>
        <Badge variant={source.loopback_only ? "default" : "secondary"}>
          {source.loopback_only ? t("sources.loopbackOnly") : "remote"}
        </Badge>
      </TableCell>
      <TableCell className="flex items-center justify-between gap-4 px-0 py-2 text-xs text-muted-foreground md:table-cell md:px-2">
        <span className="md:hidden">{t("common.created")}</span>
        <span className="font-mono">{formatDate(source.created_at)}</span>
      </TableCell>
      <TableCell className="px-0 pb-0 pt-3 text-right md:px-2 md:py-2">
        <Badge variant="secondary">{t("dashboard.legacyReadOnly")}</Badge>
      </TableCell>
    </TableRow>
  );
}

export default function SourcesPage() {
  const { t, locale } = useI18n();
  const sourcesQuery = useEventSources();
  const runtimeStatusQuery = useRuntimeStatusV1();
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("sources.managePage")}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {t("sources.managePageSubtitle")}
          </p>
        </div>
        <Badge variant="outline">{t("dashboard.legacyEventV2Badge")}</Badge>
      </div>

      {runtimeStatusQuery.isLoading ? (
        <LoadingState label={t("runtimeStatus.title")} rows={2} />
      ) : (
        <RuntimeStatusPanel
          projection={runtimeStatusQuery.data ?? unavailableRuntimeProjection()}
          roles={["telemetry_v3", "inference_v3"]}
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

      {sourcesQuery.isLoading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((item) => (
            <Skeleton key={item} className="h-12 w-full rounded-xl" />
          ))}
        </div>
      ) : sourcesQuery.isError ? (
        <div className="rounded-xl border border-destructive/30 bg-card p-6 text-sm text-destructive">
          {apiErrorMessage(sourcesQuery.error, locale)}
        </div>
      ) : !sourcesQuery.data || sourcesQuery.data.length === 0 ? (
        <Card>
          <CardContent className="p-12 text-center">
            <Database className="mx-auto mb-3 h-10 w-10 text-muted-foreground/40" aria-hidden="true" />
            <p className="text-sm text-muted-foreground">{t("dashboard.noEventSource")}</p>
          </CardContent>
        </Card>
      ) : (
        <div className="overflow-hidden rounded-xl border bg-card">
          <Table className="block md:table">
            <TableHeader className="hidden md:table-header-group">
              <TableRow className="hover:bg-transparent">
                <TableHead>{t("common.name")}</TableHead>
                <TableHead>{t("dashboard.endpoint")}</TableHead>
                <TableHead>{t("dashboard.mode")}</TableHead>
                <TableHead>{t("sources.loopbackOnly")}</TableHead>
                <TableHead>{t("common.created")}</TableHead>
                <TableHead className="w-32" />
              </TableRow>
            </TableHeader>
            <TableBody className="block space-y-3 p-3 md:table-row-group md:space-y-0 md:p-0">
              {sourcesQuery.data.map((source) => (
                <SourceRow key={source.id} source={source} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
