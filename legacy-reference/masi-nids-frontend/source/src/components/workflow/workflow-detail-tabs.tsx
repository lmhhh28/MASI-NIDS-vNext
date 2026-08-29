"use client";

import { useQuery } from "@tanstack/react-query";

import api from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { WorkflowResponse, WorkflowSummary } from "@/types/api";
import { metadataFor, workflowStatus } from "@/lib/status-metadata";
import { isFlowEvidenceActionWorkflow } from "@/lib/workflow-action";

import { ActionDeploymentTimeline } from "./action-deployment-timeline";
import { PipelineGraph } from "./pipeline-graph";
import { StepTimeline } from "./step-timeline";
import { EvidenceChain } from "./evidence-chain";
import { ReviewPacketCard } from "./review-packet-card";
import { PlanDiffCard } from "./plan-diff-card";
import { RiskCard } from "./risk-card";

interface WorkflowDetailTabsProps {
  summary: WorkflowSummary;
  workflowId: string;
  tab?: string;
  onTabChange?: (tab: string) => void;
}

function safeJsonString(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function WorkflowDetailTabs({ summary, workflowId, tab = "overview", onTabChange }: WorkflowDetailTabsProps) {
  const { t } = useI18n();
  const detailQ = useQuery<WorkflowResponse>({
    queryKey: ["workflow-detail", workflowId],
    queryFn: ({ signal }) => api.get(`/workflows/${workflowId}`, { signal }).then((r) => r.data),
    refetchInterval: (query) => {
      const detail = query.state.data;
      return detail && !metadataFor(workflowStatus, detail.status).terminal ? 5_000 : false;
    },
    refetchIntervalInBackground: false,
  });

  const detail = detailQ.data;
  const agentSteps = (detail?.agent_steps ?? []) as unknown as Parameters<typeof StepTimeline>[0]["steps"];
  const artifacts = (detail?.artifacts ?? []) as Array<Record<string, unknown>>;

  const reviewPacketEnvelope = (summary.review_packet ?? null) as {
    artifact_id?: string;
    packet_hash?: string | null;
    payload?: Record<string, unknown>;
  } | null;
  const reviewPacket = reviewPacketEnvelope
    ? ({
        ...(reviewPacketEnvelope.payload ?? {}),
        artifact_id: reviewPacketEnvelope.artifact_id,
        packet_hash: reviewPacketEnvelope.packet_hash,
      } as Parameters<typeof ReviewPacketCard>[0]["packet"])
    : null;
  const plan = (summary.plan ?? null) as Parameters<typeof PlanDiffCard>[0]["plan"];
  const risk = (summary.risk ?? null) as Parameters<typeof RiskCard>[0]["risk"];
  const isActionWorkflow = isFlowEvidenceActionWorkflow(summary);

  return (
    <Tabs value={tab} onValueChange={onTabChange} className="min-w-0 space-y-3">
      <TabsList className="w-full max-w-full justify-start overflow-x-auto bg-secondary/30">
        <TabsTrigger value="overview" className="text-xs cursor-pointer">
          {t("workflow.tabs.overview")}
        </TabsTrigger>
        <TabsTrigger value="pipeline" className="text-xs cursor-pointer">
          {t("workflow.tabs.pipeline")}
        </TabsTrigger>
        <TabsTrigger value="timeline" className="text-xs cursor-pointer">
          {t("workflow.tabs.timeline")}
        </TabsTrigger>
        <TabsTrigger value="evidence" className="text-xs cursor-pointer">
          {t("workflow.tabs.evidence")}
        </TabsTrigger>
        <TabsTrigger value="raw" className="text-xs cursor-pointer">
          {t("workflow.tabs.raw")}
        </TabsTrigger>
      </TabsList>

      <TabsContent value="overview" className="min-w-0 space-y-3">
        <RiskCard risk={risk} />
        <PlanDiffCard plan={plan} templateName={summary.template_name} />
        <ReviewPacketCard packet={reviewPacket} />
        {isActionWorkflow ? <ActionDeploymentTimeline summary={summary} /> : null}
        <DeploymentCard deployment={summary.deployment as Record<string, unknown> | null} />
      </TabsContent>

      <TabsContent value="pipeline" className="min-w-0">
        <PipelineGraph
          steps={agentSteps as unknown as Parameters<typeof PipelineGraph>[0]["steps"]}
          agentAnalysisContext={
            summary.revision_id
              ? { workflowId: workflowId, revisionId: summary.revision_id }
              : null
          }
        />
      </TabsContent>

      <TabsContent value="timeline" className="min-w-0 space-y-3">
        {isActionWorkflow ? <ActionDeploymentTimeline summary={summary} /> : null}
        <StepTimeline steps={agentSteps as unknown as Parameters<typeof StepTimeline>[0]["steps"]} />
      </TabsContent>

      <TabsContent value="evidence" className="min-w-0">
        <EvidenceChain
          reviewPacket={reviewPacket as unknown as Parameters<typeof EvidenceChain>[0]["reviewPacket"]}
          artifacts={artifacts as unknown as Parameters<typeof EvidenceChain>[0]["artifacts"]}
        />
      </TabsContent>

      <TabsContent value="raw" className="min-w-0 space-y-2">
        <RawPanel title={t("workflows.reviewPacket")} value={summary.review_packet} />
        <RawPanel title={t("workflows.risk")} value={summary.risk} />
        <RawPanel title={t("workflows.plan")} value={summary.plan} />
        <RawPanel title={t("workflows.report")} value={summary.report} />
        {summary.deployment != null && (
          <RawPanel title={t("p4.latestDeployment")} value={summary.deployment} />
        )}
      </TabsContent>
    </Tabs>
  );
}

function DeploymentCard({ deployment }: { deployment: Record<string, unknown> | null }) {
  const { t } = useI18n();
  if (!deployment) {
    return (
      <div className="rounded-xl border border-border bg-card p-4 text-xs text-muted-foreground">
        {t("workflow.deployment.empty")}
      </div>
    );
  }
  const result = (deployment.result ?? {}) as Record<string, unknown>;
  const writerMode = String(result.writer_mode ?? t("common.unknown"));
  const verificationScope = String(result.verification_scope ?? t("common.unknown"));
  const status = String(deployment.status ?? t("common.unknown"));
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium">{t("workflow.deployment.title")}</span>
        <Badge variant={status === "applied" ? "success" : "secondary"} className="text-xs">
          {status}
        </Badge>
        <Badge
          variant={writerMode === "demo_fake" ? "outline" : "secondary"}
          className="text-xs font-mono"
        >
          {writerMode}
        </Badge>
      </div>
      <div className="grid min-w-0 gap-3 md:grid-cols-2">
        <DeploymentField label={t("p4.deploymentId")} value={deployment.id} />
        <DeploymentField label={t("p4.tableName")} value={deployment.table_name} />
        <DeploymentField label={t("p4.operation")} value={deployment.operation} />
        <DeploymentField label={t("p4.rollback")} value={deployment.rollback_state} />
        <DeploymentField label={t("workflow.deployment.idempotencyKey")} value={deployment.idempotency_key} />
        <DeploymentField label={t("workflow.deployment.verificationScope")} value={verificationScope} />
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        {t("workflow.deployment.verificationHint")}
      </p>
    </div>
  );
}

function DeploymentField({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="min-w-0">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="mt-0.5 break-all font-mono text-xs">{value == null || value === "" ? "—" : String(value)}</p>
    </div>
  );
}

function RawPanel({ title, value }: { title: string; value: unknown }) {
  return (
    <div className="min-w-0 rounded-lg border border-border bg-secondary/20 p-3">
      <p className="mb-1.5 text-xs uppercase tracking-wide text-muted-foreground">
        {title}
      </p>
      <pre className="max-h-48 max-w-full overflow-auto whitespace-pre-wrap break-all text-xs font-mono text-muted-foreground">
        {value == null ? "—" : safeJsonString(value)}
      </pre>
    </div>
  );
}
