import { describe, expect, it } from "vitest";

import {
  pipelineTargetCanHotActivate,
  pipelineTargetHotExecutorCompatible,
  pipelineTargetHealth,
  pipelineVariantActivationEligible,
  isCanonicalUint64,
  shortIdentity,
} from "@/lib/pipeline-v3";

const synchronized = {
  generation_state: "verified" as const,
  desired_bundle_id: "bundle-a",
  desired_variant_id: "bmv2",
  active_bundle_id: "bundle-a",
  active_variant_id: "bmv2",
};

describe("pipeline target health", () => {
  it("distinguishes a verified generation from a requested activation", () => {
    expect(pipelineTargetHealth(synchronized)).toBe("verified");
    expect(
      pipelineTargetHealth({
        ...synchronized,
        desired_bundle_id: "bundle-b",
      }),
    ).toBe("activation_pending");
  });

  it("gives generation fences precedence over desired state", () => {
    expect(pipelineTargetHealth({ ...synchronized, generation_state: "changed" })).toBe("stale");
    expect(pipelineTargetHealth({ ...synchronized, generation_state: "unavailable" })).toBe(
      "unavailable",
    );
    expect(pipelineTargetHealth({ ...synchronized, generation_state: "unbound" })).toBe("unbound");
  });

  it("shortens durable identifiers without changing short values", () => {
    expect(shortIdentity("1234567890abcdef")).toBe("1234567890…");
    expect(shortIdentity("short")).toBe("short");
    expect(shortIdentity(null)).toBe("—");
  });
});

describe("pipeline activation eligibility", () => {
  const hotVariant = {
    activation_mode: "p4runtime_reconcile_and_commit" as const,
    adapter_kind: "bmv2_p4runtime" as const,
  };
  const coldVariant = {
    activation_mode: "immutable_cold_boot" as const,
    adapter_kind: "p4_dpdk" as const,
  };
  const verifiedTarget = {
    generation_state: "verified" as const,
    active_bundle_id: "bundle-a",
    active_variant_id: "bmv2",
    active_pipeline_cookie: "3",
    connect_uri: "grpc://127.0.0.1:50051",
    p4_role: "",
    transport_security_mode: "plaintext" as const,
  };

  it("allows hot activation only when the exact current generation is verified", () => {
    expect(pipelineTargetCanHotActivate(verifiedTarget)).toBe(true);
    expect(pipelineVariantActivationEligible(verifiedTarget, hotVariant)).toBe(true);

    for (const generationState of ["unbound", "changed", "unavailable"] as const) {
      const target = { ...verifiedTarget, generation_state: generationState };
      expect(pipelineTargetCanHotActivate(target)).toBe(false);
      expect(pipelineVariantActivationEligible(target, hotVariant)).toBe(false);
    }

    expect(pipelineTargetCanHotActivate({ ...verifiedTarget, active_pipeline_cookie: null })).toBe(false);
  });

  it("validates the complete canonical uint64 wire range without Number coercion", () => {
    expect(isCanonicalUint64("0")).toBe(true);
    expect(isCanonicalUint64("18446744073709551615")).toBe(true);
    expect(isCanonicalUint64("18446744073709551616")).toBe(false);
    expect(isCanonicalUint64("01")).toBe(false);
    expect(isCanonicalUint64("9007199254740993")).toBe(true);
  });

  it("keeps immutable cold boot eligible for an unbound target", () => {
    const unboundTarget = {
      generation_state: "unbound" as const,
      active_bundle_id: null,
      active_variant_id: null,
      active_pipeline_cookie: null,
      connect_uri: "grpcs://switch.example:9559",
      p4_role: "restricted-role",
      transport_security_mode: "mtls" as const,
    };

    expect(pipelineVariantActivationEligible(unboundTarget, hotVariant)).toBe(false);
    expect(pipelineVariantActivationEligible(unboundTarget, coldVariant)).toBe(true);
  });

  it("does not advertise hot activation beyond the current executor capability", () => {
    expect(pipelineTargetHotExecutorCompatible(verifiedTarget)).toBe(true);
    for (const target of [
      { ...verifiedTarget, connect_uri: "grpcs://switch.example:9559", transport_security_mode: "tls" as const },
      { ...verifiedTarget, p4_role: "restricted-role" },
      { ...verifiedTarget, connect_uri: "unix:///run/p4.sock" },
    ]) {
      expect(pipelineTargetHotExecutorCompatible(target)).toBe(false);
      expect(pipelineVariantActivationEligible(target, hotVariant)).toBe(false);
      expect(pipelineVariantActivationEligible(target, coldVariant)).toBe(true);
    }
    expect(
      pipelineVariantActivationEligible(verifiedTarget, {
        ...hotVariant,
        adapter_kind: "p4_dpdk",
      }),
    ).toBe(false);
  });
});
