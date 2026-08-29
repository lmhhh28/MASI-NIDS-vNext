import type { TranslationKey } from "@/lib/messages";

export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger";

export interface StatusMetadata {
  labelKey: TranslationKey | null;
  fallbackLabel: string;
  tone: StatusTone;
  terminal: boolean;
  actions: readonly string[];
}

const status = (
  fallbackLabel: string,
  tone: StatusTone,
  terminal: boolean,
  actions: readonly string[] = []
): StatusMetadata => ({ labelKey: null, fallbackLabel, tone, terminal, actions });

export const workflowStatus: Record<string, StatusMetadata> = {
  admitted: status("Admitted", "info", false, ["cancel"]),
  queued: status("Queued", "info", false, ["cancel"]),
  running: status("Running", "info", false, ["cancel"]),
  pending_review: status("Pending review", "warning", false, ["review", "cancel"]),
  approved: status("Approved", "info", false, ["deploy", "cancel"]),
  deploying: status("Deploying", "info", false),
  completed: status("Completed", "success", true),
  inspect_only_completed: status("Analysis completed", "success", true),
  deployed: status("Deployed", "success", true),
  rejected: status("Rejected", "danger", true),
  admission_rejected: status("Admission rejected", "danger", true),
  blocked: status("Blocked", "danger", true),
  review_failed: status("Review failed", "danger", true, ["retry"]),
  deployment_failed: status("Deployment failed", "danger", true, ["retry"]),
  deployment_unknown: status("Deployment outcome unknown", "warning", false, ["resolve"]),
  cancelled: status("Cancelled", "neutral", true),
  failed: status("Failed", "danger", true, ["retry"]),
};

export const p4Status: Record<string, StatusMetadata> = {
  prepared: status("Prepared", "info", false),
  claimed: status("Claimed", "info", false),
  rpc_succeeded: status("RPC succeeded; finalizing", "warning", false),
  applied: status("Applied", "success", true, ["rollback"]),
  rolled_back: status("Rolled back", "success", true),
  rejected: status("Rejected", "danger", true),
  failed: status("Failed", "danger", true, ["reconcile"]),
  unknown: status("Outcome unknown", "warning", false, ["resolve"]),
  needs_reconcile: status("Needs reconciliation", "warning", false, ["resolve"]),
};

export const reviewStatus: Record<string, StatusMetadata> = {
  pending: status("Pending", "warning", false, ["approve", "edit", "reject"]),
  approved: status("Approved", "success", true),
  rejected: status("Rejected", "danger", true),
  superseded: status("Superseded", "neutral", true),
};

export const runtimeConsoleStatus: Record<string, StatusMetadata> = {
  operational: status("Operational", "success", false),
  hold: status("HOLD", "warning", false),
  degraded: status("Degraded", "warning", false),
  unknown: status("Unknown", "danger", false),
  restart_required: status("Restart required", "warning", false),
  failed: status("Failed", "danger", true),
};

export const maintenanceStatus: Record<string, StatusMetadata> = {
  active: status("Maintenance active", "warning", false),
  inactive: status("Available", "success", true),
  unknown: status("Status unknown", "danger", false),
};

export function metadataFor(
  table: Record<string, StatusMetadata>,
  value: string | null | undefined
): StatusMetadata {
  if (!value) return status("Unknown", "warning", false);
  return table[value] ?? status(value.replaceAll("_", " "), "neutral", false);
}
