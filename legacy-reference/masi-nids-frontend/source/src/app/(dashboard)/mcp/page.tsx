"use client";

import dynamic from "next/dynamic";
import { useState } from "react";
import { ChevronDown, ChevronRight, Terminal } from "lucide-react";

import { ErrorState, LoadingState } from "@/components/async-state";
import { Badge } from "@/components/ui/badge";
import { useMcpManifest } from "@/hooks/use-mcp-tools";
import { useAuthStore } from "@/lib/auth";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import type { MCPToolManifestEntry } from "@/types/api";

const ToolPanel = dynamic(() =>
  import("@/components/mcp/tool-panel").then((module) => module.ToolPanel),
);

const GROUP_ORDER: MCPToolManifestEntry["side_effect"][] = [
  "read",
  "validate",
  "workflow_command",
  "p4_write",
];

const GROUP_LABELS: Record<MCPToolManifestEntry["side_effect"], TranslationKey> = {
  read: "mcp.groupRead",
  validate: "mcp.groupValidate",
  workflow_command: "mcp.groupWorkflow",
  p4_write: "mcp.groupP4Write",
};

function ToolCard({ tool }: { tool: MCPToolManifestEntry }) {
  const [open, setOpen] = useState(false);
  const user = useAuthStore((state) => state.user);
  const roleAllowed = tool.role_required !== "admin" || user?.role === "admin";

  return (
    <article>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        className="flex min-h-12 w-full items-center justify-between rounded-lg border bg-card px-4 py-3 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <div className="flex min-w-0 items-start gap-3">
          {open ? <ChevronDown className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" /> : <ChevronRight className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />}
          <div className="min-w-0">
            <span className="break-all font-mono text-sm font-medium text-primary">{tool.name}</span>
            <p className="mt-1 text-sm text-muted-foreground">{tool.description}</p>
          </div>
        </div>
        <div className="ml-3 flex shrink-0 flex-col items-end gap-1 sm:flex-row sm:items-center">
          <Badge variant="outline">{tool.side_effect}</Badge>
          <Badge variant={roleAllowed ? "secondary" : "destructive"}>{tool.role_required}</Badge>
        </div>
      </button>
      {open ? <ToolPanel tool={tool} roleAllowed={roleAllowed} /> : null}
    </article>
  );
}

export default function McpPage() {
  const { t } = useI18n();
  const manifestQuery = useMcpManifest();

  if (manifestQuery.isLoading) return <LoadingState rows={6} />;
  if (manifestQuery.isError || !manifestQuery.data) {
    return <ErrorState title={t("mcp.manifestUnavailable")} error={manifestQuery.error} onRetry={() => manifestQuery.refetch()} />;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold">{t("mcp.title")}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{t("mcp.subtitle", { count: manifestQuery.data.count })}</p>
        </div>
        <Badge variant="default">{t("mcp.fromManifest")} · {manifestQuery.data.count}</Badge>
      </div>

      {GROUP_ORDER.map((group) => {
        const tools = manifestQuery.data.tools.filter((tool) => tool.side_effect === group);
        if (tools.length === 0) return null;
        return (
          <section key={group} className="space-y-2" aria-labelledby={`mcp-${group}`}>
            <h2 id={`mcp-${group}`} className="flex items-center gap-2 text-sm font-semibold text-muted-foreground">
              <Terminal className="size-4" aria-hidden="true" />
              {t(GROUP_LABELS[group])}
              <Badge variant="outline">{tools.length}</Badge>
            </h2>
            <div className="space-y-2">{tools.map((tool) => <ToolCard key={tool.name} tool={tool} />)}</div>
          </section>
        );
      })}
    </div>
  );
}
