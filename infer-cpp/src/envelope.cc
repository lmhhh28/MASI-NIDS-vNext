#include "envelope.h"

#include <fstream>
#include <sstream>

#include "digest.h"

namespace masi::inf {

namespace {

constexpr const char* kSupportedSchema = "inference-startup-envelope/v1";

void require_field(const nlohmann::json& j, const std::string& name) {
  if (!j.contains(name))
    throw error::Exception(error::Code::kInvalidManifest, "envelope missing field: " + name);
}

template <typename T>
T field(const nlohmann::json& j, const std::string& name) {
  require_field(j, name);
  try {
    return j.at(name).get<T>();
  } catch (const std::exception& e) {
    throw error::Exception(error::Code::kInvalidManifest, "envelope field " + name + ": " + e.what());
  }
}

}  // namespace

std::string compute_envelope_body_digest(const StartupEnvelope& env) {
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
      << env.inference_wire_profile_digest << '\n'
      << env.runtime_profile_id << '\n'
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

void assert_incarnation_advanced(const std::string& prior, const std::string& next) {
  if (prior.empty()) return;
  if (prior == next)
    throw error::Exception(error::Code::kFenced, "incarnation not advanced (same)");
  // Incarnations are opaque stable identities; equality is the only hard
  // rejection. A strictly-never-used requirement is enforced by the Go Model
  // Manager at issuance; the Gateway additionally rejects re-use here.
}

StartupEnvelope parse_startup_envelope(const std::string& path, int64_t now_unix_ms) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw error::Exception(error::Code::kInvalidManifest, "envelope open failed");
  std::string content((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
  if (content.size() > 1048576)
    throw error::Exception(error::Code::kInvalidManifest, "envelope exceeds 1MiB");

  nlohmann::json j;
  try {
    j = nlohmann::json::parse(content);
  } catch (const std::exception& e) {
    throw error::Exception(error::Code::kInvalidManifest, std::string("envelope json: ") + e.what());
  }
  if (!j.is_object())
    throw error::Exception(error::Code::kInvalidManifest, "envelope not object");

  StartupEnvelope env;
  env.schema_version = field<std::string>(j, "schema_version");
  if (env.schema_version != kSupportedSchema)
    throw error::Exception(error::Code::kIncompatibleContract, "envelope schema_version unsupported: " + env.schema_version);
  env.model_control_incarnation_id = field<std::string>(j, "model_control_incarnation_id");
  env.operation_id = field<std::string>(j, "operation_id");
  env.kind = field<std::string>(j, "kind");
  env.logical_pool_id = field<std::string>(j, "logical_pool_id");
  env.pool_generation = field<uint64_t>(j, "pool_generation");
  env.availability_profile_id = field<std::string>(j, "availability_profile_id");
  env.deployment_tier = field<std::string>(j, "deployment_tier");
  env.model_revision_digest = field<std::string>(j, "model_revision_digest");
  env.inference_wire_profile_digest = field<std::string>(j, "inference_wire_profile_digest");
  env.runtime_profile_id = field<std::string>(j, "runtime_profile_id");

  require_field(j, "repository_snapshot");
  const auto& rs = j.at("repository_snapshot");
  if (!rs.is_object())
    throw error::Exception(error::Code::kInvalidManifest, "repository_snapshot not object");
  env.repository_snapshot.identity = field<std::string>(rs, "identity");
  env.repository_snapshot.closure_digest = field<std::string>(rs, "closure_digest");

  require_field(j, "instance_group");
  const auto& ig = j.at("instance_group");
  if (!ig.is_object())
    throw error::Exception(error::Code::kInvalidManifest, "instance_group not object");
  env.instance_group.kind = field<std::string>(ig, "kind");
  env.instance_group.count = field<int32_t>(ig, "count");
  env.instance_group.operator_partition_digest = field<std::string>(ig, "operator_partition_digest");

  if (env.instance_group.kind != "KIND_CPU" && env.instance_group.kind != "KIND_CUDA")
    throw error::Exception(error::Code::kInstanceGroupImplicit, "instance_group kind unsupported: " + env.instance_group.kind);
  if (env.instance_group.count <= 0)
    throw error::Exception(error::Code::kInstanceGroupImplicit, "instance_group count must be explicit and positive");
  if (env.instance_group.operator_partition_digest.empty())
    throw error::Exception(error::Code::kInstanceGroupImplicit, "instance_group operator_partition_digest empty");

  env.proposed_binding_generation = field<uint64_t>(j, "proposed_binding_generation");
  env.issued_at_unix_ms = field<int64_t>(j, "issued_at_unix_ms");
  env.expires_at_unix_ms = field<int64_t>(j, "expires_at_unix_ms");
  env.trace_id = field<std::string>(j, "trace_id");
  env.envelope_digest = field<std::string>(j, "envelope_digest");

  if (env.envelope_digest.rfind("sha256:", 0) != 0)
    throw error::Exception(error::Code::kInvalidManifest, "envelope_digest not sha256-prefixed");
  if (now_unix_ms > env.expires_at_unix_ms)
    throw error::Exception(error::Code::kFenced, "envelope expired");

  const std::string computed = compute_envelope_body_digest(env);
  if (!digest_match(computed, env.envelope_digest))
    throw error::Exception(error::Code::kReadbackMismatch, "envelope_digest mismatch");

  return env;
}

}  // namespace masi::inf