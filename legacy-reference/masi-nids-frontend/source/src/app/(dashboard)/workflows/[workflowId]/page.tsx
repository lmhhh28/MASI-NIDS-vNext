"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, use, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, Ban, Check, Loader2, Rocket, RotateCcw } from "lucide-react";
import { toast } from "sonner";

import { ErrorState, LoadingState, StaleState } from "@/components/async-state";
import { ClosedLoopPanel } from "@/components/workflow/closed-loop-panel";
import { WorkflowDetailTabs } from "@/components/workflow/workflow-detail-tabs";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { useIdempotentOperation } from "@/hooks/use-idempotent-operation";
import { useSwitches } from "@/hooks/use-p4";
import { useWorkflowSummary } from "@/hooks/use-workflows";
import api, { apiErrorMessage, parseApiError } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import { metadataFor, workflowStatus } from "@/lib/status-metadata";
import { useOperationRegistry } from "@/lib/operations";
import { asPositiveInteger } from "@/lib/utils";
import { isFlowEvidenceActionWorkflow } from "@/lib/workflow-action";
import type { WorkflowDeployRequest, WorkflowReviewRequest, WorkflowSummary } from "@/types/api";

const STALE_CODES = new Set([
  "STALE_REVIEW_PACKET",
  "STALE_REVIEW_ARTIFACTS",
  "STALE_P4_RULE_PLAN",
  "STALE_RISK_POLICY",
  "STALE_P4INFO",
  "STALE_TABLE_SCHEMA",
  "STALE_DIRECTIONAL_EVIDENCE",
  "WORKFLOW_STATE_CONFLICT",
  "BATCH_STATE_CONFLICT",
]);

type ReviewDecision = "approve" | "edit" | "reject";
type RetryKind = "node" | "deployment";

interface WorkflowRetryPayload {
  kind: RetryKind;
  target_id: string;
  expected_state_version: number;
  expected_attempt?: number;
  reason: string;
}

function fact(value: unknown): string {
  return value == null || value === "" ? "—" : String(value);
}

function SummaryFact({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-all font-mono text-sm">{fact(value)}</dd>
    </div>
  );
}

function WorkflowDetailContent({ workflowId }: { workflowId: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const tab = searchParams.get("tab") ?? "overview";
  const { t, locale } = useI18n();
  const summaryQuery = useWorkflowSummary(workflowId);
  const switchesQuery = useSwitches();
  const workflowSafety = useRuntimeSafety("workflow");
  const p4Safety = useRuntimeSafety("p4");
  const queryClient = useQueryClient();
  const isAdmin = useAuthStore((state) => state.user?.role === "admin");
  const canReview = isAdmin;
  const [reviewReason, setReviewReason] = useState("");
  const [editedIntent, setEditedIntent] = useState("");
  const [ttlDraft, setTtlDraft] = useState("");
  const [pendingReview, setPendingReview] = useState<WorkflowReviewRequest | null>(null);
  const [deploySwitchId, setDeploySwitchId] = useState("");
  const [confirmDeploy, setConfirmDeploy] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [retryReason, setRetryReason] = useState("");
  const [confirmRetry, setConfirmRetry] = useState(false);
  const [retryKey, setRetryKey] = useState<string | null>(null);
  const [deployKey, setDeployKey] = useState<string | null>(null);
  const [cancelKey, setCancelKey] = useState<string | null>(null);
  const [staleRevision, setStaleRevision] = useState<{ old: number; latest?: number } | null>(null);
  const operationRecords = useOperationRegistry((state) => state.operations);

  const summary = summaryQuery.data;
  const plan = (summary?.plan ?? {}) as Record<string, unknown>;
  const risk = (summary?.risk ?? {}) as Record<string, unknown>;
  const packet = (summary?.review_packet ?? {}) as Record<string, unknown>;
  const packetPayload = (packet.payload ?? {}) as Record<string, unknown>;
  const deployment = (summary?.deployment ?? {}) as Record<string, unknown>;
  const retrySpec = useMemo(() => {
    if (summary?.next_actions?.includes("retry_node")) {
      const node = (summary.nodes ?? []).find((item) => item.status === "failed");
      const id = typeof node?.id === "string" ? node.id : null;
      if (id) {
        return {
          kind: "node" as const,
          targetId: id,
          expectedAttempt: Math.max(1, Number(node?.attempt ?? 1)),
        };
      }
    }
    if (summary?.next_actions?.includes("retry_deployment")) {
      const id = typeof deployment.id === "string"
        ? deployment.id
        : summary.current_deployment_intent_id ?? null;
      if (id) return { kind: "deployment" as const, targetId: id };
    }
    return null;
  }, [deployment.id, summary]);
  const eligibleSwitches = useMemo(
    () =>
      (switchesQuery.data ?? []).filter(
        (item) => item.pipeline_owner === "p4_agent" && item.writes_enabled
      ),
    [switchesQuery.data]
  );
  const selectedSwitch = deploySwitchId || eligibleSwitches[0]?.id || "";
  const persistedDeployKey = useMemo(
    () => Object.values(operationRecords).find(
      (operation) => operation.kind === "workflow.deploy"
        && operation.target === `${workflowId}:${selectedSwitch}`
        && ["prepared", "submitting", "outcome_unknown"].includes(operation.status)
    )?.key ?? null,
    [operationRecords, selectedSwitch, workflowId]
  );
  const persistedCancelKey = useMemo(
    () => Object.values(operationRecords).find(
      (operation) => operation.kind === "workflow.cancel"
        && operation.target === workflowId
        && ["prepared", "submitting", "outcome_unknown"].includes(operation.status)
    )?.key ?? null,
    [operationRecords, workflowId]
  );
  const persistedRetryKey = useMemo(
    () => retrySpec
      ? Object.values(operationRecords).find(
          (operation) => operation.kind === "workflow.retry"
            && operation.target === `${workflowId}:${retrySpec.kind}:${retrySpec.targetId}`
            && ["prepared", "submitting", "outcome_unknown"].includes(operation.status)
        )?.key ?? null
      : null,
    [operationRecords, retrySpec, workflowId]
  );
  const effectiveDeployKey = deployKey ?? persistedDeployKey;
  const effectiveCancelKey = cancelKey ?? persistedCancelKey;
  const effectiveRetryKey = retryKey ?? persistedRetryKey;
  const runtimeUnknown = ["loading", "error", "stale"].includes(workflowSafety.reason ?? "");
  const workflowWritesDisabled = !workflowSafety.allowed;
  const p4WritesDisabled = !p4Safety.allowed;

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["workflow-summary", workflowId] }),
      queryClient.invalidateQueries({ queryKey: ["workflow-detail", workflowId] }),
      queryClient.invalidateQueries({ queryKey: ["workflows"] }),
    ]);
  };

  const reviewMutation = useMutation({
    retry: false,
    mutationFn: (payload: WorkflowReviewRequest) =>
      api.post(`/workflows/${workflowId}/review`, payload, {
        headers: { "Idempotency-Key": crypto.randomUUID() },
      }),
    onSuccess: async () => {
      setPendingReview(null);
      setReviewReason("");
      setEditedIntent("");
      setTtlDraft("");
      setStaleRevision(null);
      await refresh();
      toast.success(t("workflows.reviewSubmitted"));
    },
    onError: async (error) => {
      const info = parseApiError(error, locale);
      if (STALE_CODES.has(info.code ?? "") || info.status === 409 || info.status === 412) {
        setPendingReview(null);
        setStaleRevision({ old: summary?.revision ?? 0 });
        await refresh();
      }
      toast.error(t("workflows.reviewFailed"), { description: apiErrorMessage(info, locale) });
    },
  });

  const deployOperationOptions = useMemo(
    () => ({
      kind: "workflow.deploy",
      target: () => `${workflowId}:${selectedSwitch}`,
      execute: (payload: WorkflowDeployRequest, key: string) =>
        api
          .post(`/workflows/${workflowId}/deployments`, payload, {
            headers: { "Idempotency-Key": key },
          })
          .then((response) => response.data as WorkflowSummary),
      resultStatus: (result: WorkflowSummary) => result.deployment_status,
    }),
    [selectedSwitch, workflowId]
  );
  const deployOperation = useIdempotentOperation(deployOperationOptions);
  const deployMutation = useMutation({
    retry: false,
    mutationFn: async (payload: WorkflowDeployRequest) => {
      const key = effectiveDeployKey ?? crypto.randomUUID();
      setDeployKey(key);
      return deployOperation.run(payload, key);
    },
    onSuccess: async ({ result, key }) => {
      setConfirmDeploy(false);
      await refresh();
      if (["unknown", "needs_reconcile"].includes(result.deployment_status ?? "")) {
        toast.warning(t("p4.operationOutcomeUnknown"), { description: key });
        return;
      }
      if (["failed", "p4_write_failed", "rejected"].includes(result.deployment_status ?? "")) {
        setDeployKey(null);
        toast.error(t("workflows.deploymentFailed"));
        return;
      }
      setDeployKey(null);
      toast.success(t("workflows.deploymentApplied"));
    },
    onError: async (error) => {
      setConfirmDeploy(false);
      const info = parseApiError(error, locale);
      const outcome = effectiveDeployKey
        ? useOperationRegistry.getState().operations[effectiveDeployKey]?.status
        : undefined;
      if (outcome !== "outcome_unknown") setDeployKey(null);
      if (STALE_CODES.has(info.code ?? "") || info.status === 409 || info.status === 412) {
        setStaleRevision({ old: summary?.revision ?? 0 });
        await refresh();
      }
      toast.error(t("workflows.deploymentFailed"), { description: apiErrorMessage(info, locale) });
    },
  });

  const cancelOperationOptions = useMemo(
    () => ({
      kind: "workflow.cancel",
      target: () => workflowId,
      execute: (payload: { expected_state_version: number }, key: string) =>
        api
          .post(`/workflows/${workflowId}/cancel`, payload, {
            headers: { "Idempotency-Key": key },
          })
          .then((response) => response.data as WorkflowSummary),
      resultStatus: (result: WorkflowSummary) => result.status,
    }),
    [workflowId]
  );
  const cancelOperation = useIdempotentOperation(cancelOperationOptions);
  const cancelMutation = useMutation({
    retry: false,
    mutationFn: () => {
      if (summary?.state_version == null) throw new Error("WORKFLOW_STATE_UNAVAILABLE");
      const key = effectiveCancelKey ?? `cancel-${crypto.randomUUID()}`;
      setCancelKey(key);
      return cancelOperation.run({ expected_state_version: summary.state_version }, key);
    },
    onSuccess: async ({ key }) => {
      setConfirmCancel(false);
      await refresh();
      const outcome = useOperationRegistry.getState().operations[key]?.status;
      if (outcome === "outcome_unknown") {
        toast.warning(t("p4.operationOutcomeUnknown"), { description: key });
        return;
      }
      setCancelKey(null);
      toast.success(t("workflows.cancelSubmitted"));
    },
    onError: async (error) => {
      setConfirmCancel(false);
      const info = parseApiError(error, locale);
      const outcome = effectiveCancelKey
        ? useOperationRegistry.getState().operations[effectiveCancelKey]?.status
        : undefined;
      if (outcome !== "outcome_unknown") setCancelKey(null);
      if (STALE_CODES.has(info.code ?? "") || info.status === 409 || info.status === 412) {
        setStaleRevision({ old: summary?.revision ?? 0 });
        await refresh();
      }
      toast.error(t("workflows.cancelFailed"), { description: apiErrorMessage(info, locale) });
    },
  });

  const retryOperationOptions = useMemo(
    () => ({
      kind: "workflow.retry",
      target: (payload: WorkflowRetryPayload) =>
        `${workflowId}:${payload.kind}:${payload.target_id}`,
      execute: (payload: WorkflowRetryPayload, key: string) => {
        const endpoint = payload.kind === "node"
          ? `/workflows/${workflowId}/nodes/${payload.target_id}/retry`
          : `/workflows/${workflowId}/deployments/${payload.target_id}/retry`;
        const body = payload.kind === "node"
          ? {
              expected_state_version: payload.expected_state_version,
              expected_attempt: payload.expected_attempt,
              reason: payload.reason,
            }
          : {
              expected_state_version: payload.expected_state_version,
              reason: payload.reason,
            };
        return api
          .post(endpoint, body, { headers: { "Idempotency-Key": key } })
          .then((response) => response.data as WorkflowSummary);
      },
      resultStatus: (result: WorkflowSummary) => result.deployment_status ?? result.status,
    }),
    [workflowId]
  );
  const retryOperation = useIdempotentOperation(retryOperationOptions);
  const retryMutation = useMutation({
    retry: false,
    mutationFn: () => {
      if (!retrySpec || summary?.state_version == null || !retryReason.trim()) {
        throw new Error("WORKFLOW_RETRY_CONTEXT_REQUIRED");
      }
      const payload: WorkflowRetryPayload = {
        kind: retrySpec.kind,
        target_id: retrySpec.targetId,
        expected_state_version: summary.state_version,
        expected_attempt: retrySpec.kind === "node" ? retrySpec.expectedAttempt : undefined,
        reason: retryReason.trim(),
      };
      const key = effectiveRetryKey ?? `retry-${crypto.randomUUID()}`;
      setRetryKey(key);
      return retryOperation.run(payload, key);
    },
    onSuccess: async ({ key }) => {
      setConfirmRetry(false);
      await refresh();
      const outcome = useOperationRegistry.getState().operations[key]?.status;
      if (outcome === "outcome_unknown") {
        toast.warning(t("p4.operationOutcomeUnknown"), { description: key });
        return;
      }
      setRetryKey(null);
      setRetryReason("");
      toast.success("重试请求已提交");
    },
    onError: async (error) => {
      setConfirmRetry(false);
      const info = parseApiError(error, locale);
      const outcome = effectiveRetryKey
        ? useOperationRegistry.getState().operations[effectiveRetryKey]?.status
        : undefined;
      if (outcome !== "outcome_unknown") setRetryKey(null);
      if (STALE_CODES.has(info.code ?? "") || info.status === 409 || info.status === 412) {
        setStaleRevision({ old: summary?.revision ?? 0 });
        await refresh();
      }
      toast.error("重试请求失败", { description: apiErrorMessage(info, locale) });
    },
  });

  function prepareReview(decision: ReviewDecision) {
    if (!workflowSafety.guard()) return;
    if (!summary) return;
    if (decision === "reject" && !reviewReason.trim()) {
      toast.error("拒绝必须填写原因");
      return;
    }
    const currentTtl = asPositiveInteger((plan.ttl_seconds ?? summary.requested_template_params.ttl_seconds) as unknown);
    const nextTtl = ttlDraft.trim() ? asPositiveInteger(ttlDraft) : currentTtl;
    if (decision === "edit") {
      if (!reviewReason.trim()) {
        toast.error("编辑必须填写变更原因");
        return;
      }
      const hasIntentChange = Boolean(editedIntent.trim());
      const hasTtlChange = nextTtl != null && nextTtl !== currentTtl;
      if (!hasIntentChange && !hasTtlChange) {
        toast.error(t("workflows.editChangeRequired"));
        return;
      }
    }
    const context = (summary.approval_request ?? {}) as Record<string, unknown>;
    setPendingReview({
      decision,
      comment: reviewReason.trim() || undefined,
      edited_intent: decision === "edit" ? editedIntent.trim() || undefined : undefined,
      edited_template_params:
        decision === "edit" && nextTtl != null && nextTtl !== currentTtl
          ? { ttl_seconds: nextTtl }
          : undefined,
      review_packet_id: fact(context.review_packet_id ?? packet.id),
      review_packet_hash: fact(context.review_packet_hash ?? packet.packet_hash),
      artifact_ids: context.artifact_ids as string[] | undefined,
      plan_revision: Number(context.plan_revision ?? summary.revision),
      risk_policy_version: Number(context.risk_policy_version ?? risk.version) || undefined,
      p4info_hash: fact(context.p4info_hash ?? packetPayload.p4info_hash),
      table_schema_hash: fact(context.table_schema_hash ?? packetPayload.table_schema_hash),
      expected_revision: Number(context.expected_revision ?? summary.revision),
      expected_state_version: Number(context.expected_state_version ?? summary.state_version),
    });
  }

  function deployPayload(): WorkflowDeployRequest | null {
    if (!summary || !selectedSwitch || !plan.id) return null;
    return {
      switch_id: selectedSwitch,
      plan_id: String(plan.id),
      plan_hash: fact(plan.plan_hash),
      review_packet_id: fact(packet.id),
      review_packet_hash: fact(packet.packet_hash),
      expected_revision: summary.revision,
      expected_state_version: summary.state_version,
    };
  }

  function openDeployConfirmation() {
    if (workflowSafety.guard() && p4Safety.guard()) setConfirmDeploy(true);
  }

  function confirmReviewAction() {
    if (!workflowSafety.guard() || !pendingReview) return;
    reviewMutation.mutate(pendingReview);
  }

  function confirmDeployAction() {
    if (!workflowSafety.guard() || !p4Safety.guard()) return;
    const payload = deployPayload();
    if (payload) deployMutation.mutate(payload);
  }

  function confirmCancelAction() {
    if (workflowSafety.guard()) cancelMutation.mutate();
  }

  function confirmRetryAction() {
    if (!workflowSafety.guard()) return;
    if (retrySpec?.kind === "deployment" && !p4Safety.guard()) return;
    retryMutation.mutate();
  }

  if (summaryQuery.isLoading) return <LoadingState rows={5} label="正在加载工作流详情" />;
  if (summaryQuery.isError) return <ErrorState error={summaryQuery.error} onRetry={() => summaryQuery.refetch()} />;
  if (!summary) return <ErrorState error={new Error("WORKFLOW_NOT_FOUND")} />;

  const meta = metadataFor(workflowStatus, summary.status);
  const isActionWorkflow = isFlowEvidenceActionWorkflow(summary);
  const canSubmitReview =
    canReview && (summary.review_status === "open" || summary.next_actions?.includes("approve"));
  const canDeploy =
    !isActionWorkflow && (summary.next_actions?.includes("deploy") || summary.next_action === "deploy_approved_plan");
  const canCancel = summary.next_actions?.includes("cancel") ?? false;

  return (
    <div className="space-y-6">
      <nav aria-label="面包屑" className="flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
        <Link href="/workflows" className="hover:text-foreground">工作流</Link>
        <span aria-hidden="true">/</span>
        <span aria-current="page" className="min-w-0 truncate font-mono">{workflowId}</span>
      </nav>
      <div className="flex flex-wrap items-start gap-3">
        <Link href="/workflows" className={buttonVariants({ variant: "ghost" })}>
          <ArrowLeft aria-hidden="true" />返回列表
        </Link>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="break-all font-mono text-xl font-semibold">{workflowId}</h1>
            <Badge
              variant={meta.tone === "danger" ? "destructive" : meta.tone === "success" ? "success" : meta.tone === "warning" ? "warning" : "outline"}
            >
              {meta.fallbackLabel}
            </Badge>
            <Badge variant="secondary">revision {summary.revision}</Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">{summary.template_name}</p>
        </div>
      </div>

      {runtimeUnknown ? (
        <Alert variant="destructive">
          <AlertTriangle aria-hidden="true" />
          <AlertTitle>运行状态未知</AlertTitle>
          <AlertDescription>为避免 fail-open，审核、部署和其他高风险操作已禁用。</AlertDescription>
        </Alert>
      ) : null}

      {staleRevision ? (
        <StaleState>
          草稿已保留。提交时 revision 为 {staleRevision.old}，服务端当前 revision 为 {summary.revision}；请核对后重新确认，系统不会自动重放决定。
        </StaleState>
      ) : null}

      <Card>
        <CardHeader><CardTitle className="text-base">并发与审核事实</CardTitle></CardHeader>
        <CardContent>
          <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <SummaryFact label="packet hash" value={packet.packet_hash} />
            <SummaryFact label="plan hash" value={plan.plan_hash} />
            <SummaryFact label="risk version" value={risk.version ?? packetPayload.risk_policy_version} />
            <SummaryFact label="P4Info hash" value={packetPayload.p4info_hash ?? plan.p4info_hash} />
            <SummaryFact label="schema hash" value={packetPayload.table_schema_hash ?? plan.table_schema_hash} />
            <SummaryFact label="state version" value={summary.state_version} />
            <SummaryFact label={t("runtimeStatus.workflow.sourceEvent")} value={summary.source_event_id} />
            <SummaryFact label={t("runtimeStatus.workflow.eventSchema")} value={summary.source_event_schema_version} />
            <SummaryFact label={t("runtimeStatus.workflow.eventHash")} value={summary.source_event_hash} />
            <SummaryFact label={t("runtimeStatus.workflow.admissionPolicy")} value={summary.event_admission_policy_id} />
            <SummaryFact label="deployment" value={summary.deployment_status} />
          </dl>
        </CardContent>
      </Card>

      {summary.source_event_schema_version === 4 ? (
        <Alert>
          <AlertTriangle aria-hidden="true" />
          <AlertTitle>{t("runtimeStatus.workflow.inspectOnlyTitle")}</AlertTitle>
          <AlertDescription>{t("runtimeStatus.workflow.inspectOnlyDescription")}</AlertDescription>
        </Alert>
      ) : null}

      {isActionWorkflow ? (
        <Alert>
          <AlertTriangle aria-hidden="true" />
          <AlertTitle>Action workflow</AlertTitle>
          <AlertDescription>
            FlowEvidence block actions deploy through the prepared intent timeline; manual deployment is disabled.
          </AlertDescription>
        </Alert>
      ) : null}

      {canSubmitReview ? (
        <Card>
          <CardHeader><CardTitle className="text-base">人工审核</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="review-reason">原因 / 备注</Label>
              <Textarea id="review-reason" value={reviewReason} onChange={(event) => setReviewReason(event.target.value)} placeholder="拒绝与编辑时必填" />
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="edited-intent">变更内容</Label>
                <Textarea id="edited-intent" value={editedIntent} onChange={(event) => setEditedIntent(event.target.value)} />
              </div>
              <div className="space-y-2">
                <Label htmlFor="edited-ttl">TTL（秒）</Label>
                <Input id="edited-ttl" type="number" min={1} value={ttlDraft} onChange={(event) => setTtlDraft(event.target.value)} />
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button disabled={workflowWritesDisabled} onClick={() => prepareReview("approve")}><Check aria-hidden="true" />批准</Button>
              <Button variant="outline" disabled={workflowWritesDisabled} onClick={() => prepareReview("edit")}><RotateCcw aria-hidden="true" />编辑后重跑</Button>
              <Button variant="destructive" disabled={workflowWritesDisabled} onClick={() => prepareReview("reject")}><Ban aria-hidden="true" />拒绝</Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {(canDeploy || canCancel || (isAdmin && retrySpec)) ? (
        <Card>
          <CardHeader><CardTitle className="text-base">受控操作</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            {canDeploy ? (
              <div className="flex flex-wrap items-end gap-3">
                <div className="min-w-64 space-y-2">
                  <Label htmlFor="workflow-deploy-switch">目标交换机</Label>
                  <Select
                    value={selectedSwitch}
                    onValueChange={(value) => value !== null && setDeploySwitchId(value)}
                    disabled={Boolean(effectiveDeployKey)}
                  >
                    <SelectTrigger id="workflow-deploy-switch"><SelectValue placeholder="选择可写交换机" /></SelectTrigger>
                    <SelectContent>{eligibleSwitches.map((item) => <SelectItem key={item.id} value={item.id}>{item.name} · {item.id}</SelectItem>)}</SelectContent>
                  </Select>
                </div>
                <Button disabled={workflowWritesDisabled || p4WritesDisabled || !selectedSwitch || !plan.id} onClick={openDeployConfirmation}>
                  <Rocket aria-hidden="true" />{effectiveDeployKey ? "使用原键恢复部署" : "部署"}
                </Button>
                {effectiveDeployKey ? <span className="break-all font-mono text-xs text-warning">recovery key: {effectiveDeployKey}</span> : null}
              </div>
            ) : null}
            {canCancel ? (
              <div className="flex flex-wrap items-center gap-3">
                <Button variant="destructive" disabled={workflowWritesDisabled} onClick={() => { if (workflowSafety.guard()) setConfirmCancel(true); }}>
                  {effectiveCancelKey ? "使用原键恢复取消" : "取消工作流"}
                </Button>
                {effectiveCancelKey ? <span className="break-all font-mono text-xs text-warning">recovery key: {effectiveCancelKey}</span> : null}
              </div>
            ) : null}
            {isAdmin && retrySpec ? (
              <div className="flex flex-wrap items-center gap-3">
                <Button
                  variant="outline"
                  disabled={workflowWritesDisabled || (retrySpec.kind === "deployment" && p4WritesDisabled)}
                  onClick={() => { if (workflowSafety.guard() && (retrySpec.kind !== "deployment" || p4Safety.guard())) setConfirmRetry(true); }}
                >
                  <RotateCcw aria-hidden="true" />
                  {effectiveRetryKey ? "使用原键恢复重试" : retrySpec.kind === "node" ? "重试失败节点" : "重试失败部署"}
                </Button>
                {effectiveRetryKey ? <span className="break-all font-mono text-xs text-warning">recovery key: {effectiveRetryKey}</span> : null}
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      <WorkflowDetailTabs
        summary={summary}
        workflowId={workflowId}
        tab={tab}
        onTabChange={(nextTab) => {
          const params = new URLSearchParams(searchParams.toString());
          params.set("tab", nextTab);
          router.replace(`/workflows/${encodeURIComponent(workflowId)}?${params.toString()}`);
        }}
      />

      <ClosedLoopPanel deploymentId={typeof deployment.id === "string" ? deployment.id : null} />

      <Dialog open={pendingReview !== null} onOpenChange={(open) => !open && setPendingReview(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>确认审核决定</DialogTitle><DialogDescription>该决定不会在版本冲突后自动重放。</DialogDescription></DialogHeader>
          <dl className="grid gap-3 sm:grid-cols-2"><SummaryFact label="decision" value={pendingReview?.decision} /><SummaryFact label="revision" value={pendingReview?.expected_revision} /><SummaryFact label="packet hash" value={pendingReview?.review_packet_hash} /><SummaryFact label="plan hash" value={plan.plan_hash} /></dl>
          <DialogFooter><Button variant="outline" onClick={() => setPendingReview(null)}>返回修改</Button><Button disabled={workflowWritesDisabled || reviewMutation.isPending} onClick={confirmReviewAction}>{reviewMutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" /> : null}确认提交</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!isActionWorkflow && confirmDeploy} onOpenChange={setConfirmDeploy}>
        <DialogContent>
          <DialogHeader><DialogTitle>确认部署意图</DialogTitle><DialogDescription>部署将进入 durable P4 outbox；超时结果必须使用同一恢复键查询。</DialogDescription></DialogHeader>
          <dl className="grid gap-3 sm:grid-cols-2"><SummaryFact label="switch" value={selectedSwitch} /><SummaryFact label="TTL" value={plan.ttl_seconds} /><SummaryFact label="revision" value={summary.revision} /><SummaryFact label="risk" value={risk.risk_level ?? risk.level} /></dl>
          <pre className="max-h-48 overflow-auto rounded-md bg-muted p-3 text-xs">{JSON.stringify(plan, null, 2)}</pre>
          <DialogFooter><Button variant="outline" onClick={() => setConfirmDeploy(false)}>返回</Button><Button disabled={workflowWritesDisabled || p4WritesDisabled || deployMutation.isPending || !deployPayload()} onClick={confirmDeployAction}>{deployMutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" /> : <Rocket aria-hidden="true" />}确认部署</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmCancel} onOpenChange={setConfirmCancel}>
        <DialogContent>
          <DialogHeader><DialogTitle>确认取消工作流</DialogTitle><DialogDescription>将使用 state version {summary.state_version} 作为并发 fence；结果未决时只允许使用同一幂等键恢复。</DialogDescription></DialogHeader>
          <dl className="grid gap-3 sm:grid-cols-2"><SummaryFact label="workflow" value={workflowId} /><SummaryFact label="revision" value={summary.revision} /><SummaryFact label="state version" value={summary.state_version} /><SummaryFact label="risk" value={risk.risk_level ?? risk.level} /></dl>
          <DialogFooter><Button variant="outline" onClick={() => setConfirmCancel(false)}>返回</Button><Button variant="destructive" disabled={workflowWritesDisabled || cancelMutation.isPending} onClick={confirmCancelAction}>确认取消</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmRetry} onOpenChange={setConfirmRetry}>
        <DialogContent>
          <DialogHeader><DialogTitle>确认重试</DialogTitle><DialogDescription>重试不会覆盖并发更新；结果未决时只允许使用同一幂等键恢复。</DialogDescription></DialogHeader>
          <dl className="grid gap-3 sm:grid-cols-2"><SummaryFact label="target" value={retrySpec?.targetId} /><SummaryFact label="kind" value={retrySpec?.kind} /><SummaryFact label="revision" value={summary.revision} /><SummaryFact label="risk" value={risk.risk_level ?? risk.level} /></dl>
          <div className="space-y-2">
            <Label htmlFor="workflow-retry-reason">重试原因</Label>
            <Textarea id="workflow-retry-reason" value={retryReason} onChange={(event) => setRetryReason(event.target.value)} placeholder="必填；最多 500 字" maxLength={500} />
          </div>
          <DialogFooter><Button variant="outline" onClick={() => setConfirmRetry(false)}>返回</Button><Button disabled={workflowWritesDisabled || (retrySpec?.kind === "deployment" && p4WritesDisabled) || retryMutation.isPending || !retryReason.trim()} onClick={confirmRetryAction}>{retryMutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" /> : <RotateCcw aria-hidden="true" />}确认重试</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default function WorkflowDetailPage({ params }: { params: Promise<{ workflowId: string }> }) {
  const { workflowId } = use(params);
  return <Suspense fallback={<LoadingState rows={5} label="正在加载工作流详情" />}><WorkflowDetailContent workflowId={workflowId} /></Suspense>;
}
