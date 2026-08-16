#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "envelope.h"
#include "ort_session.h"
#include "triton_client.h"

#include "edge.pb.h"

namespace masi::inf {

// Staged verification result from each startup phase. The readback aggregates
// these so the Edge can compare an exact loaded model/runtime against the
// immutable startup envelope.
struct StageResult {
  std::string stage;          // "verification" | "load" | "backend-session"
  std::string status;         // "OK" | "HOLD" | "FAIL"
  int64_t started_at_unix_ms = 0;
  int64_t completed_at_unix_ms = 0;
  std::string observed_digest;
  std::string reason_code;
};

struct WorkerRuntimeIdentity {
  std::string worker_id;          // stable per replica
  std::string worker_digest;       // sha256: of worker_id + provider identity
  std::string provider_identity;   // from OrtSession::provider_identity()
  std::string session_options_identity;
};

struct PoolReadback {
  // Identity carried from the startup envelope.
  std::string model_control_incarnation_id;
  std::string operation_id;
  std::string logical_pool_id;
  uint64_t pool_generation = 0;
  uint64_t proposed_binding_generation = 0;
  std::string startup_envelope_digest;
  std::string repository_identity;
  std::string repository_closure_digest;
  std::string instance_group_kind;
  int32_t instance_group_count = 0;
  std::string instance_group_operator_partition_digest;

  // Observed identity (from loaded model + session).
  std::string model_revision_digest;
  std::string model_bundle_digest;
  std::string feature_contract_digest;
  std::string label_contract_digest;
  std::string output_adapter_digest;
  std::string wire_profile;
  std::string wire_profile_digest;
  std::string runtime_profile;
  std::string runtime_profile_digest;
  std::string optimization_profile_digest;

  WorkerRuntimeIdentity worker;
  std::vector<WorkerRuntimeIdentity> eligible_workers;

  // Staged results with timestamps.
  std::vector<StageResult> stages;

  int64_t observed_at_unix_ms = 0;
  std::string readback_attempt_id;
  bool loaded_not_current = false;  // true if observed != proposed binding
};

// Build a readback object from the verified startup state. `eligible_workers`
// is bounded to 64 by contract.
PoolReadback build_pool_readback(const StartupEnvelope& env,
                                 const OrtSession& session,
                                 const TritonClient& triton,
                                 int64_t now_unix_ms);

// Serialize the readback into the canonical Edge `BindingReadback` protobuf.
masi::edge::v1::BindingReadback to_binding_readback_proto(const PoolReadback& rb);

}  // namespace masi::inf