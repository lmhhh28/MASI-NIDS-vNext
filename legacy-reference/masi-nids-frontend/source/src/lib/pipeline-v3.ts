import type { PipelineBundleVariantV3, PipelineTargetV3 } from "@/types/api";

const UINT64_MAX_DECIMAL = "18446744073709551615";

export function isCanonicalUint64(value: string): boolean {
  if (!/^(?:0|[1-9][0-9]{0,19})$/.test(value)) return false;
  return value.length < UINT64_MAX_DECIMAL.length
    || (value.length === UINT64_MAX_DECIMAL.length && value <= UINT64_MAX_DECIMAL);
}

export type PipelineTargetHealth =
  | "verified"
  | "activation_pending"
  | "stale"
  | "unavailable"
  | "unbound";

export function pipelineTargetHealth(
  target: Pick<
    PipelineTargetV3,
    | "generation_state"
    | "desired_bundle_id"
    | "desired_variant_id"
    | "active_bundle_id"
    | "active_variant_id"
  >,
): PipelineTargetHealth {
  if (target.generation_state === "changed") return "stale";
  if (target.generation_state === "unavailable") return "unavailable";
  if (target.generation_state === "unbound") return "unbound";
  if (
    target.desired_bundle_id !== target.active_bundle_id ||
    target.desired_variant_id !== target.active_variant_id
  ) {
    return "activation_pending";
  }
  return "verified";
}

type PipelineHotActivationTarget = Pick<
  PipelineTargetV3,
  | "generation_state"
  | "active_bundle_id"
  | "active_variant_id"
  | "active_pipeline_cookie"
>;

type PipelineHotExecutorTarget = Pick<
  PipelineTargetV3,
  "connect_uri" | "p4_role" | "transport_security_mode"
>;

export function pipelineTargetCanHotActivate(target: PipelineHotActivationTarget): boolean {
  return target.generation_state === "verified"
    && target.active_bundle_id !== null
    && target.active_variant_id !== null
    && target.active_pipeline_cookie !== null;
}

export function pipelineTargetHotExecutorCompatible(target: PipelineHotExecutorTarget): boolean {
  return target.connect_uri.startsWith("grpc://")
    && target.p4_role === ""
    && target.transport_security_mode === "plaintext";
}

export function pipelineVariantActivationEligible(
  target: PipelineHotActivationTarget & PipelineHotExecutorTarget,
  variant: Pick<PipelineBundleVariantV3, "activation_mode" | "adapter_kind">,
): boolean {
  return variant.activation_mode === "immutable_cold_boot"
    || (
      variant.adapter_kind === "bmv2_p4runtime"
      && pipelineTargetCanHotActivate(target)
      && pipelineTargetHotExecutorCompatible(target)
    );
}

export function shortIdentity(value: string | null | undefined, visible = 10) {
  if (!value) return "—";
  return value.length <= visible ? value : `${value.slice(0, visible)}…`;
}
