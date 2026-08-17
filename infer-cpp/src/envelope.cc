#include "envelope.h"

#include <fstream>
#include <sstream>

#include "digest.h"

namespace masi::inf {

namespace {

constexpr const char *kSupportedSchema = "inference-startup-envelope/v1";

void require_field(const nlohmann::json &j, const std::string &name) {
  if (!j.contains(name))
    throw error::Exception(error::Code::kInvalidManifest, "envelope missing field: " + name);
}

template <typename T> T field(const nlohmann::json &j, const std::string &name) {
  require_field(j, name);
  try {
    return j.at(name).get<T>();
  } catch (const std::exception &e) {
    throw error::Exception(error::Code::kInvalidManifest,
                           "envelope field " + name + ": " + e.what());
  }
}

} // namespace

std::string compute_envelope_body_digest(const StartupEnvelope &env) {
  std::ostringstream oss;
  oss << env.schema_version << '\n'
      << env.model_control_incarnation_id << '\n'
      << env.operation_id << '\n'
      << env.kind << '\n'
      << env.logical_pool_id << '\n'
      << env.pool_generation << '\n'
      << env.availability_profile_id << '\n'
      << env.deployment_tier << '\n'
      << env.model_revision_digest << '\n'
      << env.feature_contract_digest << '\n'
      << env.label_contract_digest << '\n'
      << env.output_adapter_digest << '\n'
      << env.inference_wire_profile_digest << '\n'
      << env.runtime_profile_id << '\n'
      << env.runtime_profile_digest << '\n'
      << env.optimization_profile_digest << '\n'
      << env.triton_server_version << '\n'
      << env.repository_snapshot.identity << '\n'
      << env.repository_snapshot.closure_digest << '\n'
      << env.instance_group.kind << '\n'
      << env.instance_group.count << '\n'
      << env.instance_group.operator_partition_digest << '\n'
      << env.proposed_binding_generation << '\n'
      << env.issued_at_unix_ms << '\n'
      << env.expires_at_unix_ms << '\n'
      << env.trace_id << '\n';
  const std::string body = oss.str();
  return sha256_hex(body.data(), body.size());
}

void assert_incarnation_advanced(const std::string &prior, const std::string &next) {
  if (prior.empty())
    return;
  if (prior == next)
    throw error::Exception(error::Code::kFenced, "incarnation not advanced (same)");
  // Incarnations are opaque stable identities; equality is the only hard
  // rejection. A strictly-never-used requirement is enforced by the Go Model
  // Manager at issuance; the Gateway additionally rejects re-use here.
}

StartupEnvelope parse_startup_envelope(const std::string &path, int64_t now_unix_ms) {
  std::ifstream f(path, std::ios::binary);
  if (!f)
    throw error::Exception(error::Code::kInvalidManifest, "envelope open failed");
  std::string content((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
  if (content.size() > 1048576)
    throw error::Exception(error::Code::kInvalidManifest, "envelope exceeds 1MiB");

  nlohmann::json j;
  try {
    j = nlohmann::json::parse(content);
  } catch (const std::exception &e) {
    throw error::Exception(error::Code::kInvalidManifest,
                           std::string("envelope json: ") + e.what());
  }
  if (!j.is_object())
    throw error::Exception(error::Code::kInvalidManifest, "envelope not object");

  StartupEnvelope env;
  env.schema_version = field<std::string>(j, "schema_version");
  if (env.schema_version != kSupportedSchema)
    throw error::Exception(error::Code::kIncompatibleContract,
                           "envelope schema_version unsupported: " + env.schema_version);
  env.model_control_incarnation_id = field<std::string>(j, "model_control_incarnation_id");
  env.operation_id = field<std::string>(j, "operation_id");
  env.kind = field<std::string>(j, "kind");
  env.logical_pool_id = field<std::string>(j, "logical_pool_id");
  env.pool_generation = field<uint64_t>(j, "pool_generation");
  env.availability_profile_id = field<std::string>(j, "availability_profile_id");
  env.deployment_tier = field<std::string>(j, "deployment_tier");
  env.model_revision_digest = field<std::string>(j, "model_revision_digest");
  env.feature_contract_digest = field<std::string>(j, "feature_contract_digest");
  env.label_contract_digest = field<std::string>(j, "label_contract_digest");
  env.output_adapter_digest = field<std::string>(j, "output_adapter_digest");
  for (const auto *pair :
       {&env.feature_contract_digest, &env.label_contract_digest, &env.output_adapter_digest}) {
    if (!is_sha256_digest(*pair))
      throw error::Exception(error::Code::kInvalidManifest,
                             "envelope contract digest format invalid: " + *pair);
  }
  env.inference_wire_profile_digest = field<std::string>(j, "inference_wire_profile_digest");
  env.runtime_profile_id = field<std::string>(j, "runtime_profile_id");
  env.runtime_profile_digest = field<std::string>(j, "runtime_profile_digest");
  env.optimization_profile_digest = field<std::string>(j, "optimization_profile_digest");
#ifdef MASI_INF_WIRE_PROFILE_DIGEST
  // This binary implements exactly one frozen wire profile revision. An
  // envelope that declares a different revision must not be served.
  if (env.inference_wire_profile_digest != MASI_INF_WIRE_PROFILE_DIGEST)
    throw error::Exception(error::Code::kInvalidManifest,
                           "envelope inference_wire_profile_digest does not match the "
                           "wire profile this binary implements: " +
                               env.inference_wire_profile_digest);
#endif
#ifdef MASI_INF_RUNTIME_PROFILE_DIGEST
  if (env.runtime_profile_id == "model-runtime-central-cpu/v1" &&
      env.runtime_profile_digest != MASI_INF_RUNTIME_PROFILE_DIGEST)
    throw error::Exception(error::Code::kInvalidManifest,
                           "envelope runtime_profile_digest does not match the frozen CPU profile");
#endif
#ifdef MASI_INF_OPTIMIZATION_PROFILE_DIGEST
  if (env.optimization_profile_digest != MASI_INF_OPTIMIZATION_PROFILE_DIGEST)
    throw error::Exception(
        error::Code::kInvalidManifest,
        "envelope optimization_profile_digest does not match the frozen CPU profile");
#endif
  // Every digest-typed identity field is required and must be a real digest.
  // An empty or unprefixed value is a missing identity, not a permissive
  // default: accepting it would let a replica start without a verifiable
  // binding to the frozen wire/runtime/optimization profiles.
  for (const auto *required : {&env.model_revision_digest, &env.inference_wire_profile_digest,
                               &env.runtime_profile_digest, &env.optimization_profile_digest}) {
    if (!is_sha256_digest(*required))
      throw error::Exception(error::Code::kInvalidManifest,
                             "envelope profile digest format invalid: " + *required);
  }
  env.triton_server_version = field<std::string>(j, "triton_server_version");
  if (env.triton_server_version.empty())
    throw error::Exception(error::Code::kInvalidManifest, "triton_server_version empty");

  require_field(j, "repository_snapshot");
  const auto &rs = j.at("repository_snapshot");
  if (!rs.is_object())
    throw error::Exception(error::Code::kInvalidManifest, "repository_snapshot not object");
  env.repository_snapshot.identity = field<std::string>(rs, "identity");
  env.repository_snapshot.closure_digest = field<std::string>(rs, "closure_digest");
  if (!is_sha256_digest(env.repository_snapshot.closure_digest))
    throw error::Exception(error::Code::kInvalidManifest,
                           "repository_snapshot closure_digest not sha256-prefixed: " +
                               env.repository_snapshot.closure_digest);

  require_field(j, "instance_group");
  const auto &ig = j.at("instance_group");
  if (!ig.is_object())
    throw error::Exception(error::Code::kInvalidManifest, "instance_group not object");
  env.instance_group.kind = field<std::string>(ig, "kind");
  env.instance_group.count = field<int32_t>(ig, "count");
  env.instance_group.operator_partition_digest =
      field<std::string>(ig, "operator_partition_digest");

  if (env.instance_group.kind != "KIND_CPU" && env.instance_group.kind != "KIND_CUDA")
    throw error::Exception(error::Code::kInstanceGroupImplicit,
                           "instance_group kind unsupported: " + env.instance_group.kind);
  if (env.instance_group.count <= 0)
    throw error::Exception(error::Code::kInstanceGroupImplicit,
                           "instance_group count must be explicit and positive");
  if (!is_sha256_digest(env.instance_group.operator_partition_digest))
    throw error::Exception(error::Code::kInstanceGroupImplicit,
                           "instance_group operator_partition_digest format invalid");

  env.proposed_binding_generation = field<uint64_t>(j, "proposed_binding_generation");
  env.issued_at_unix_ms = field<int64_t>(j, "issued_at_unix_ms");
  env.expires_at_unix_ms = field<int64_t>(j, "expires_at_unix_ms");
  env.trace_id = field<std::string>(j, "trace_id");
  env.envelope_digest = field<std::string>(j, "envelope_digest");

  if (!is_sha256_digest(env.envelope_digest))
    throw error::Exception(error::Code::kInvalidManifest, "envelope_digest format invalid");
  if (now_unix_ms > env.expires_at_unix_ms)
    throw error::Exception(error::Code::kFenced, "envelope expired");

  const std::string computed = compute_envelope_body_digest(env);
  if (!digest_match(computed, env.envelope_digest))
    throw error::Exception(error::Code::kReadbackMismatch, "envelope_digest mismatch");

  return env;
}

} // namespace masi::inf
