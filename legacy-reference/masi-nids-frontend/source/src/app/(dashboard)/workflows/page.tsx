"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronLeft, ChevronRight, GitBranch, Search } from "lucide-react";

import { EmptyState, ErrorState, LoadingState } from "@/components/async-state";
import { TemplateCapabilityCard } from "@/components/workflow/template-capability-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useWorkflows } from "@/hooks/use-workflows";
import { useI18n } from "@/lib/i18n";
import { metadataFor, workflowStatus } from "@/lib/status-metadata";

const PAGE_SIZE = 20;

function toneVariant(
  tone: string,
): "default" | "secondary" | "destructive" | "outline" | "success" | "warning" {
  if (tone === "success") return "success";
  if (tone === "warning") return "warning";
  if (tone === "danger") return "destructive";
  return "outline";
}

function WorkflowsContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { t, formatDateTime } = useI18n();
  const tab = searchParams.get("tab") === "pending" ? "pending" : "all";
  const page = Math.max(1, Number(searchParams.get("page") ?? 1) || 1);
  const template = searchParams.get("template") ?? "";
  const explicitStatus = searchParams.get("status") ?? "";
  const search = searchParams.get("search") ?? "";
  const [searchDraft, setSearchDraft] = useState(search);
  const status = tab === "pending" ? "open" : explicitStatus;

  const queryParams = useMemo(
    () => ({
      ...(status ? { status } : {}),
      ...(template ? { template } : {}),
      ...(search ? { search } : {}),
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    [page, search, status, template]
  );
  const workflows = useWorkflows(queryParams);
  const total = workflows.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  function update(next: Record<string, string | null>) {
    const params = new URLSearchParams(searchParams.toString());
    for (const [key, value] of Object.entries(next)) {
      if (!value) params.delete(key);
      else params.set(key, value);
    }
    if (!("page" in next)) params.set("page", "1");
    router.replace(`/workflows?${params.toString()}`);
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{t("workflows.title")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{t("workflows.subtitle")}</p>
      </div>

      <TemplateCapabilityCard />

      <div
        role="tablist"
        aria-label={t("workflows.title")}
        className="inline-flex min-h-9 items-center gap-1 rounded-lg bg-muted p-1"
      >
          <button
            type="button"
            role="tab"
            aria-selected={tab === "pending"}
            className="inline-flex min-h-8 items-center gap-1.5 rounded-md px-3 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground aria-selected:bg-background aria-selected:text-foreground aria-selected:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={() => update({ tab: "pending", status: null })}
          >
            <AlertTriangle aria-hidden="true" />
            {t("workflows.pendingReview")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "all"}
            className="inline-flex min-h-8 items-center gap-1.5 rounded-md px-3 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground aria-selected:bg-background aria-selected:text-foreground aria-selected:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={() => update({ tab: "all", status: null })}
          >
            <GitBranch aria-hidden="true" />
            {t("workflows.allWorkflows")}
          </button>
      </div>

      <div className="grid gap-3 rounded-lg border bg-card p-4 md:grid-cols-[minmax(0,1fr)_13rem_13rem]">
        <form
          className="flex min-w-0 gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            update({ search: searchDraft.trim() || null });
          }}
        >
          <Label htmlFor="workflow-search" className="sr-only">搜索工作流</Label>
          <Input
            id="workflow-search"
            value={searchDraft}
            onChange={(event) => setSearchDraft(event.target.value)}
            placeholder="工作流、事件或告警组 ID"
          />
          <Button type="submit" variant="outline" aria-label="搜索">
            <Search aria-hidden="true" />
          </Button>
        </form>
        <select
          aria-label="模板筛选"
          value={template || "all"}
          onChange={(event) =>
            update({ template: event.currentTarget.value === "all" ? null : event.currentTarget.value })
          }
          className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <option value="all">{t("common.all")}</option>
          <option value="inspect_only">{t("templates.inspect_only.label")}</option>
          <option value="block_exact_flow">{t("templates.block_exact_flow.label")}</option>
          <option value="restore_rule">{t("templates.restore_rule.label")}</option>
        </select>
        <select
          aria-label="状态筛选"
          value={tab === "pending" ? "open" : explicitStatus || "all"}
          disabled={tab === "pending"}
          onChange={(event) =>
            update({ status: event.currentTarget.value === "all" ? null : event.currentTarget.value })
          }
          className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        >
          <option value="all">{t("common.all")}</option>
          <option value="open">pending review</option>
          <option value="queued">queued</option>
          <option value="running">running</option>
          <option value="failed">failed</option>
          <option value="applied">deployed</option>
        </select>
      </div>

      {workflows.isLoading ? (
        <LoadingState rows={5} label="正在加载工作流" />
      ) : workflows.isError ? (
        <ErrorState error={workflows.error} onRetry={() => workflows.refetch()} />
      ) : workflows.data?.items.length === 0 ? (
        <EmptyState
          title={tab === "pending" ? t("workflows.noPendingReviews") : t("common.noData")}
          description="调整筛选条件或从事件页面创建新的分析工作流。"
        />
      ) : (
        <div className="space-y-3">
          {workflows.data?.items.map((workflow) => {
            const meta = metadataFor(workflowStatus, workflow.status);
            return (
              <Link
                key={workflow.id}
                href={`/workflows/${encodeURIComponent(workflow.id)}?tab=overview`}
                className="block rounded-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Card className="transition-colors hover:border-primary/40">
                  <CardContent className="grid gap-3 p-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        {meta.terminal && meta.tone === "success" ? (
                          <CheckCircle2 className="h-4 w-4 text-success" aria-hidden="true" />
                        ) : (
                          <GitBranch className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                        )}
                        <span className="font-mono text-sm font-medium">{workflow.id}</span>
                        <Badge variant={toneVariant(meta.tone)}>{meta.fallbackLabel}</Badge>
                        <Badge variant="outline">{workflow.template_name}</Badge>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
                        <span>revision {workflow.revision}</span>
                        {workflow.source_event_id ? <span className="font-mono">event {workflow.source_event_id}</span> : null}
                        {workflow.alert_group_id ? <span className="font-mono">alert {workflow.alert_group_id}</span> : null}
                      </div>
                    </div>
                    <time className="text-sm text-muted-foreground" dateTime={workflow.updated_at}>
                      {formatDateTime(workflow.updated_at)}
                    </time>
                  </CardContent>
                </Card>
              </Link>
            );
          })}
        </div>
      )}

      <nav className="flex items-center justify-between" aria-label="工作流分页">
        <p className="text-sm text-muted-foreground">{total} items · {page}/{pageCount}</p>
        <div className="flex gap-2">
          <Button type="button" variant="outline" disabled={page <= 1} onClick={() => update({ page: String(page - 1) })}>
            <ChevronLeft aria-hidden="true" />上一页
          </Button>
          <Button type="button" variant="outline" disabled={page >= pageCount} onClick={() => update({ page: String(page + 1) })}>
            下一页<ChevronRight aria-hidden="true" />
          </Button>
        </div>
      </nav>
    </div>
  );
}

export default function WorkflowsPage() {
  return <Suspense fallback={<LoadingState rows={5} label="正在加载工作流" />}><WorkflowsContent /></Suspense>;
}
