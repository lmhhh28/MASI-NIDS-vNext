#include "readback.h"

#include <algorithm>
#include <sstream>

#include "digest.h"

namespace masi::inf {

namespace {

std::string gen_attempt_id(int64_t now_unix_ms, const std::string& inc) {
  std::ostringstream oss;
  oss << "readback:" << inc << ":" << now_unix_ms;
  const std::string s = oss.str();
  return sha256_hex(s.data(), s.size());
}

}  // namespace

PoolReadback build_pool_readback(const StartupEnvelope& env,
                                 const OrtSession& session,
                                 const TritonClient& triton,
                                 int64_t now_unix_ms) {
  PoolReadback rb;
  rb.model_control_incarnation_id = env.model_control_incarnation_id;
  rb.operation_id = env.operation_id;
  rb.logical_pool_id = env.logical_pool_id;
  rb.pool_generation = env.pool_generation;
  rb.proposed_binding_generation = env.proposed_binding_generation;
  rb.startup_envelope_digest = env.envelope_digest;
  rb.repository_identity = env.repository_snapshot.identity;
  rb.repository_closure_digest = env.repository_snapshot.closure_digest;
  rb.instance_group_kind = env.instance_group.kind;
  rb.instance_group_count = env.instance_group.count;
  rb.instance_group_operator_partition_digest = env.instance_group.operator_partition_digest;

  // Model identity from the loaded ORT session. The model's stored metadata
  // (custom metadata map: masi.model_revision_digest etc.) is the source of
  // truth for the exact bundle digest set.
  rb.model_revision_digest = env.model_revision_digest;
  rb.runtime_profile = env.runtime_profile_id;
  rb.wire_profile = "inference-central-grpc-batch/v1";

  std::ostringstream wid;
  wid << env.logical_pool_id << ':' << env.pool_generation << ':'
      << env.runtime_profile_id;
  rb.worker.worker_id = sha256_hex(wid.str().data(), wid.str().size());

  std::ostringstream wp;
  wp << rb.worker.worker_id << '\n' << session.provider_identity() << '\n'
     << session.session_options_identity();
  rb.worker.worker_digest = sha256_hex(wp.str().data(), wp.str().size());
  rb.worker.provider_identity = session.provider_identity();
  rb.worker.session_options_identity = session.session_options_identity();

  // Same-generation equivalent replica set: bounded to 64 by contract. In a
  // single-replica first-phase deployment the only eligible worker is this one.
  rb.eligible_workers.push_back(rb.worker);
  if (rb.eligible_workers.size() > 64) rb.eligible_workers.resize(64);

  rb.observed_at_unix_ms = now_unix_ms;
  rb.readback_attempt_id = gen_attempt_id(now_unix_ms, env.model_control_incarnation_id);

  // loaded_not_current: in first phase the proposed binding equals the only
  // loaded binding; a future rollout would set this when observed != proposed.
  rb.loaded_not_current = false;

  (void)triton;
  return rb;
}

masi::edge::v1::BindingReadback to_binding_readback_proto(const PoolReadback& rb) {
  masi::edge::v1::BindingReadback out;
  out.set_logical_pool_id(rb.logical_pool_id);
  out.set_pool_generation(rb.pool_generation);
  out.set_binding_generation(rb.proposed_binding_generation);
  out.set_model_revision_digest(rb.model_revision_digest);
  out.set_feature_contract_digest(rb.feature_contract_digest);
  out.set_label_contract_digest(rb.label_contract_digest);
  out.set_output_adapter_digest(rb.output_adapter_digest);
  out.set_runtime_profile(rb.runtime_profile);
  out.set_worker_id(rb.worker.worker_id);
  out.set_worker_digest(rb.worker.worker_digest);
  out.set_schema_version("inference-committed-binding/v1");
  out.set_model_control_incarnation_id(rb.model_control_incarnation_id);
  out.set_operation_id(rb.operation_id);
  out.set_startup_envelope_digest(rb.startup_envelope_digest);
  out.set_model_bundle_digest(rb.model_bundle_digest);
  out.set_wire_profile(rb.wire_profile);
  out.set_wire_profile_digest(rb.wire_profile_digest);
  out.set_runtime_profile_digest(rb.runtime_profile_digest);
  out.set_optimization_profile_digest(rb.optimization_profile_digest);
  out.set_readback_attempt_id(rb.readback_attempt_id);
  out.set_observed_at_unix_ms(rb.observed_at_unix_ms);
  for (const auto& w : rb.eligible_workers) {
    auto* ew = out.add_eligible_workers();
    ew->set_worker_id(w.worker_id);
    ew->set_worker_digest(w.worker_digest);
  }
  out.set_pool_observation_digest("");
  out.set_binding_digest("");
  return out;
}

}  // namespace masi::inf