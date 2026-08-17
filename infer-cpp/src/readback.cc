#include "readback.h"

#include <algorithm>
#include <sstream>

#include "digest.h"

namespace masi::inf {

namespace {

std::string gen_attempt_id(int64_t now_unix_ms, const std::string &inc) {
  std::ostringstream oss;
  oss << "readback:" << inc << ":" << now_unix_ms;
  const std::string s = oss.str();
  return sha256_hex(s.data(), s.size());
}

std::string digest_of(const std::string &s) { return sha256_hex(s.data(), s.size()); }

} // namespace

PoolReadback build_pool_readback(const StartupEnvelope &env, const BundleManifest &bundle,
                                 const TritonObservation &observed, int64_t now_unix_ms) {
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

  // Exact binding digests. These were cross-checked against the digest-pinned
  // bundle manifest during startup, so the readback reports the verified value
  // from the bundle rather than echoing the envelope alone.
  rb.model_revision_digest = env.model_revision_digest;
  rb.model_bundle_digest = bundle.model_digest;
  rb.feature_contract_digest = bundle.feature_contract_digest;
  rb.label_contract_digest = bundle.label_contract_digest;
  rb.output_adapter_digest = bundle.output_adapter_digest;
  rb.wire_profile = "inference-central-grpc-batch/v1";
  rb.wire_profile_digest = env.inference_wire_profile_digest;
  rb.runtime_profile = env.runtime_profile_id;
  rb.runtime_profile_digest = env.runtime_profile_digest;
  rb.optimization_profile_digest = env.optimization_profile_digest;

  // Worker identity is derived from what the serving runtime actually reports.
  std::ostringstream wid;
  wid << env.logical_pool_id << ':' << env.pool_generation << ':' << env.runtime_profile_id << ':'
      << observed.model_name << ':' << observed.model_version;
  rb.worker.worker_id = digest_of(wid.str());
  rb.worker.runtime_observation_digest = digest_of(observed.canonical_projection);

  std::ostringstream obs;
  obs << "backend=" << observed.config.backend << ";platform=" << observed.config.platform
      << ";server_version=" << observed.server.version;
  for (const auto &ig : observed.config.instance_groups)
    obs << ";instance_group=" << ig.first << ':' << ig.second;
  obs << ";max_batch_size=" << observed.config.max_batch_size
      << ";dynamic_batching=" << (observed.config.dynamic_batching ? "1" : "0");
  rb.worker.runtime_observation = obs.str();

  std::ostringstream wp;
  wp << rb.worker.worker_id << '\n'
     << rb.worker.runtime_observation_digest << '\n'
     << rb.worker.runtime_observation << '\n';
  rb.worker.worker_digest = digest_of(wp.str());

  // Same-generation equivalent replica set: bounded to 64 by contract. In a
  // single-replica first-phase deployment the only eligible worker is this one.
  rb.eligible_workers.push_back(rb.worker);
  if (rb.eligible_workers.size() > 64)
    rb.eligible_workers.resize(64);

  // pool_observation_digest binds what was observed from the serving runtime
  // plus the verified on-disk closure; binding_digest binds the exact binding
  // tuple. They are distinct dimensions and neither is a copy of the envelope
  // digest.
  {
    std::ostringstream po;
    po << observed.canonical_projection << "closure_digest=" << rb.repository_closure_digest << '\n'
       << "repository_identity=" << rb.repository_identity << '\n'
       << "instance_group=" << rb.instance_group_kind << ':' << rb.instance_group_count << '\n'
       << "model_ready=" << (observed.model_ready ? "1" : "0") << '\n'
       << "server_ready=" << (observed.server_ready ? "1" : "0") << '\n';
    rb.pool_observation_digest = digest_of(po.str());
  }
  {
    std::ostringstream bd;
    bd << "incarnation=" << rb.model_control_incarnation_id << '\n'
       << "pool=" << rb.logical_pool_id << '\n'
       << "pool_generation=" << rb.pool_generation << '\n'
       << "binding_generation=" << rb.proposed_binding_generation << '\n'
       << "model_revision_digest=" << rb.model_revision_digest << '\n'
       << "model_bundle_digest=" << rb.model_bundle_digest << '\n'
       << "feature_contract_digest=" << rb.feature_contract_digest << '\n'
       << "label_contract_digest=" << rb.label_contract_digest << '\n'
       << "output_adapter_digest=" << rb.output_adapter_digest << '\n'
       << "wire_profile_digest=" << rb.wire_profile_digest << '\n'
       << "runtime_profile=" << rb.runtime_profile << '\n'
       << "runtime_profile_digest=" << rb.runtime_profile_digest << '\n'
       << "optimization_profile_digest=" << rb.optimization_profile_digest << '\n'
       << "repository_closure_digest=" << rb.repository_closure_digest << '\n';
    rb.binding_digest = digest_of(bd.str());
  }

  rb.observed_at_unix_ms = now_unix_ms;
  rb.readback_attempt_id = gen_attempt_id(now_unix_ms, env.model_control_incarnation_id);

  // loaded_not_current: the replica reports what it loaded; PostgreSQL current
  // is owned by Go. A future rollout sets this when observed != proposed.
  rb.loaded_not_current = false;

  return rb;
}

masi::edge::v1::BindingReadback to_binding_readback_proto(const PoolReadback &rb) {
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
  for (const auto &w : rb.eligible_workers) {
    auto *ew = out.add_eligible_workers();
    ew->set_worker_id(w.worker_id);
    ew->set_worker_digest(w.worker_digest);
  }
  out.set_pool_observation_digest(rb.pool_observation_digest);
  out.set_binding_digest(rb.binding_digest);
  return out;
}

} // namespace masi::inf
