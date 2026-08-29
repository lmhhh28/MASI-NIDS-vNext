import type { WorkflowSummary } from "@/types/api";

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function packetPayload(summary: WorkflowSummary): Record<string, unknown> | null {
  return record(record(summary.review_packet)?.payload);
}

export function isFlowEvidenceActionWorkflow(summary: WorkflowSummary): boolean {
  const plan = record(summary.plan);
  const packet = packetPayload(summary);
  return (
    summary.template_name === "block_exact_flow" &&
    (typeof plan?.flow_evidence_v3_id === "string" ||
      typeof packet?.flow_evidence_v3_id === "string" ||
      typeof summary.current_deployment_intent_id === "string")
  );
}

export function actionTimeline(summary: WorkflowSummary) {
  const deployment = record(summary.deployment);
  const status = String(summary.deployment_status ?? deployment?.status ?? "not_submitted");
  return [
    {
      id: "review",
      label: "review",
      status: summary.review_status ?? (summary.approval_request ? "open" : "not_required"),
    },
    {
      id: "intent",
      label: "deployment_intent",
      status: summary.current_deployment_intent_id ? "prepared" : "not_created",
    },
    {
      id: "edge",
      label: "edge_effect",
      status,
    },
  ];
}
