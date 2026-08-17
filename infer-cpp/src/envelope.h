#pragma once

#include <cstdint>
#include <nlohmann/json.hpp>
#include <string>

#include "error.h"

namespace masi::inf {

// Reference into the digest-pinned repository snapshot declared by the
// startup envelope. `identity` is a stable versioned identity (e.g. a
// repository snapshot id), `closure_digest` is the digest over the exact
// closure member set that the Gateway must observe on disk.
struct RepositorySnapshotRef {
  std::string identity;
  std::string closure_digest;
};

// Explicit instance_group declaration. The Gateway fails closed unless the
// envelope names an explicit kind/count and a frozen operator-partition
// digest. KIND_CPU is the only qualified first-phase kind.
struct InstanceGroupRef {
  std::string kind;   // "KIND_CPU" | "KIND_CUDA"
  int32_t count = 0;
  std::string operator_partition_digest;
};

struct StartupEnvelope {
  std::string schema_version;
  std::string model_control_incarnation_id;
  std::string operation_id;
  std::string kind;
  std::string logical_pool_id;
  uint64_t pool_generation = 0;
  std::string availability_profile_id;
  std::string deployment_tier;
  std::string model_revision_digest;
  // Contract digests the envelope binds for this exact binding. The Gateway
  // cross-checks them against the repository closure bundle-manifest and
  // fails closed on any mismatch.
  std::string feature_contract_digest;
  std::string label_contract_digest;
  std::string output_adapter_digest;
  std::string inference_wire_profile_digest;
  std::string runtime_profile_id;
  std::string runtime_profile_digest;
  std::string optimization_profile_digest;
  // Declared Triton server version for this pool generation. Verified against
  // the live ServerMetadata before readiness.
  std::string triton_server_version;
  RepositorySnapshotRef repository_snapshot;
  InstanceGroupRef instance_group;
  uint64_t proposed_binding_generation = 0;
  int64_t issued_at_unix_ms = 0;
  int64_t expires_at_unix_ms = 0;
  std::string trace_id;
  std::string envelope_digest;  // "sha256:" over the canonical body
};

// Parse and validate a startup envelope JSON file. Rejects:
//   - missing/incompatible schema_version
//   - any missing required field
//   - expired envelope (now > expires_at)
//   - envelope_digest mismatch
//   - implicit/unsupported instance_group kind
StartupEnvelope parse_startup_envelope(const std::string& path, int64_t now_unix_ms);

// Re-derive the envelope body digest over a canonical, field-ordered
// serialization that excludes the `envelope_digest` field itself.
std::string compute_envelope_body_digest(const StartupEnvelope& env);

// Reject an envelope whose incarnation is not strictly newer than the prior
// incumbent. PITR/restore must rotate to a never-used incarnation.
void assert_incarnation_advanced(const std::string& prior, const std::string& next);

}  // namespace masi::inf