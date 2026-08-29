"use client";

import dynamic from "next/dynamic";
import { useState } from "react";
import { Download, Pause, Play, Search } from "lucide-react";
import { toast } from "sonner";

import { EmptyState, ErrorState, LoadingState } from "@/components/async-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAuditLogs, type AuditFilters } from "@/hooks/use-audit";
import api, { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type { AuditLog, AuditLogListResponse } from "@/types/api";

const PAGE_SIZE = 50;
const AuditDetailSheet = dynamic(() =>
  import("@/components/audit/audit-detail-sheet").then(
    (module) => module.AuditDetailSheet,
  ),
);

function csvCell(value: unknown) {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? "");
  return `"${text.replaceAll('"', '""')}"`;
}

function statusBadge(status: string) {
  const variant = status === "ok" ? "default" : status === "error" ? "destructive" : "outline";
  return <Badge variant={variant}>{status}</Badge>;
}

export default function AuditPage() {
  const { t, locale, formatDateTime } = useI18n();
  const [filters, setFilters] = useState<AuditFilters>({});
  const [page, setPage] = useState(1);
  const [live, setLive] = useState(true);
  const [selected, setSelected] = useState<AuditLog | null>(null);
  const [exporting, setExporting] = useState(false);
  const query = useAuditLogs({ ...filters, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE }, live);
  const pages = Math.max(1, Math.ceil((query.data?.total ?? 0) / PAGE_SIZE));

  function updateFilter(key: keyof AuditFilters, value: string) {
    setFilters((current) => ({ ...current, [key]: value || undefined }));
    setPage(1);
  }

  async function exportCsv() {
    setExporting(true);
    try {
      const response = await api.get<AuditLogListResponse>("/audit", {
        params: { ...filters, include_total: 1, limit: 10_000, offset: 0 },
      });
      const header = ["ts", "request_id", "actor_user_id", "actor_role", "action", "resource_type", "resource_id", "channel", "status", "details"];
      const rows = response.data.items.map((item) => header.map((key) => csvCell(item[key as keyof AuditLog])).join(","));
      const blob = new Blob([[header.join(","), ...rows].join("\n")], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `nids-audit-${new Date().toISOString().slice(0, 10)}.csv`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      toast.error(apiErrorMessage(error, locale));
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h1 className="text-2xl font-semibold">{t("audit.title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("audit.subtitle")}</p></div>
        <div className="flex gap-2"><Button variant="outline" onClick={() => setLive((value) => !value)}>{live ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}{live ? t("audit.pause") : t("audit.resume")}</Button><Button variant="outline" disabled={exporting} onClick={exportCsv}><Download aria-hidden="true" />{t("audit.export")}</Button></div>
      </div>

      <section className="grid gap-3 rounded-lg border p-4 sm:grid-cols-2 lg:grid-cols-4" aria-label={t("audit.filters")}>
        <div className="space-y-1.5"><Label htmlFor="audit-action">{t("common.action")}</Label><Input id="audit-action" value={filters.action ?? ""} onChange={(event) => updateFilter("action", event.target.value)} /></div>
        <div className="space-y-1.5"><Label htmlFor="audit-role">{t("common.role")}</Label><select id="audit-role" value={filters.actor_role ?? "all"} onChange={(event) => updateFilter("actor_role", event.currentTarget.value === "all" ? "" : event.currentTarget.value)} className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><option value="all">{t("audit.allRoles")}</option><option value="admin">admin</option><option value="analyze">analyze</option></select></div>
        <div className="space-y-1.5"><Label htmlFor="audit-resource">resource</Label><Input id="audit-resource" value={filters.resource ?? ""} onChange={(event) => updateFilter("resource", event.target.value)} /></div>
        <div className="space-y-1.5"><Label htmlFor="audit-request">request ID</Label><Input id="audit-request" className="font-mono" value={filters.request_id ?? ""} onChange={(event) => updateFilter("request_id", event.target.value)} /></div>
        <div className="space-y-1.5"><Label htmlFor="audit-status">{t("common.status")}</Label><Input id="audit-status" value={filters.status ?? ""} onChange={(event) => updateFilter("status", event.target.value)} /></div>
        <div className="space-y-1.5"><Label htmlFor="audit-start">{t("audit.startTime")}</Label><Input id="audit-start" type="datetime-local" value={filters.start_time ?? ""} onChange={(event) => updateFilter("start_time", event.target.value)} /></div>
        <div className="space-y-1.5"><Label htmlFor="audit-end">{t("audit.endTime")}</Label><Input id="audit-end" type="datetime-local" value={filters.end_time ?? ""} onChange={(event) => updateFilter("end_time", event.target.value)} /></div>
        <div className="flex items-end"><Button variant="outline" className="w-full" onClick={() => { setFilters({}); setPage(1); }}><Search aria-hidden="true" />{t("audit.clearFilters")}</Button></div>
      </section>

      {query.isLoading ? <LoadingState rows={6} /> : query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : !query.data?.items.length ? <EmptyState title={t("audit.noLogs")} /> : (
        <>
          <div className="hidden overflow-x-auto rounded-lg border md:block">
            <Table><TableHeader><TableRow><TableHead>{t("common.time")}</TableHead><TableHead>{t("common.actor")}</TableHead><TableHead>{t("common.action")}</TableHead><TableHead>resource</TableHead><TableHead>{t("common.status")}</TableHead></TableRow></TableHeader><TableBody>{query.data.items.map((log) => <TableRow key={log.id} className="cursor-pointer" tabIndex={0} onClick={() => setSelected(log)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") setSelected(log); }}><TableCell className="whitespace-nowrap font-mono text-xs">{formatDateTime(log.ts)}</TableCell><TableCell className="text-sm">{log.actor_user_id?.slice(0, 8) ?? "system"}<p className="text-xs text-muted-foreground">{log.actor_role ?? "—"}</p></TableCell><TableCell className="font-mono text-xs">{log.action}</TableCell><TableCell className="font-mono text-xs">{log.resource_type ?? "—"}/{log.resource_id?.slice(0, 8) ?? "—"}</TableCell><TableCell>{statusBadge(log.status)}</TableCell></TableRow>)}</TableBody></Table>
          </div>
          <div className="grid gap-3 md:hidden">{query.data.items.map((log) => <button key={log.id} type="button" className="rounded-lg border p-4 text-left" onClick={() => setSelected(log)}><div className="flex items-center justify-between gap-2">{statusBadge(log.status)}<span className="font-mono text-xs text-muted-foreground">{formatDateTime(log.ts)}</span></div><p className="mt-2 break-all font-mono text-sm">{log.action}</p><p className="mt-1 text-xs text-muted-foreground">{log.actor_role ?? "system"} · {log.resource_type ?? "—"}</p></button>)}</div>
          <div className="flex items-center justify-between"><p className="text-sm text-muted-foreground">{query.data.total} · {page}/{pages}</p><div className="flex gap-2"><Button variant="outline" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>{t("common.back")}</Button><Button variant="outline" disabled={page >= pages} onClick={() => setPage((value) => value + 1)}>{t("common.next")}</Button></div></div>
        </>
      )}

      {selected ? <AuditDetailSheet log={selected} onClose={() => setSelected(null)} /> : null}
    </div>
  );
}
