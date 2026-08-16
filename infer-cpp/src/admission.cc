#include "admission.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <limits>
#include <sstream>

#include "digest.h"
#include "numeric.h"

namespace masi::inf {

namespace {

const std::vector<std::string>& fence_dims_ref() {
  static const std::vector<std::string> dims = {
    "request_id",
    "input_id",
    "event_idempotency_key",
    "input_digest",
    "model_control_incarnation_id",
    "operation_id",
    "scope",
    "shard_id",
    "route_epoch",
    "logical_pool_id",
    "pool_generation",
    "binding_generation",
    "startup_envelope_digest",
    "pool_observation_digest",
    "binding_digest",
    "model_revision_digest",
    "model_bundle_digest",
    "feature_contract_digest",
    "label_contract_digest",
    "output_adapter_digest",
    "wire_profile_digest",
    "runtime_profile_digest",
    "optimization_profile_digest",
    "worker_id",
    "worker_digest",
    "worker_attempt_id",
    "source_input_result_WAL_sequence",
    "source_window_identity",
    "trace_id"
  };
  return dims;
}

}  // namespace

const std::vector<std::string>& result_fence_dimensions() {
  return fence_dims_ref();
}

void assert_schema_profile(const std::string& schema_version,
                            const std::string& wire_profile) {
  if (schema_version != "inference-central-grpc-batch-profile/v1")
    throw error::Exception(error::Code::kIncompatibleContract, "schema_version unsupported: " + schema_version);
  // profile_id major must be 1; minor is closed.
  if (wire_profile != "inference-central-grpc-batch/v1")
    throw error::Exception(error::Code::kIncompatibleContract, "wire_profile unsupported: " + wire_profile);
}

TensorLayoutCheck check_tensor_layout(const std::vector<uint32_t>& shape,
                                      const std::string& dtype,
                                      uint32_t alignment) {
  TensorLayoutCheck r;
  size_t element_size = 0;
  if (dtype == "uint64-le") element_size = 8;
  else if (dtype == "float32-le") element_size = 4;
  else if (dtype == "float64-le") element_size = 8;
  else { r.reason = "unknown dtype"; return r; }

  if (shape.empty()) { r.reason = "empty shape"; return r; }
  // Checked product of shape dimensions with integer-overflow guard.
  size_t element_count = 1;
  for (uint32_t d : shape) {
    if (d == 0) { r.reason = "zero dim"; return r; }
    if (element_count > SIZE_MAX / d) { r.reason = "shape overflow"; return r; }
    element_count *= d;
  }
  if (element_size > 0 && element_count > SIZE_MAX / element_size) {
    r.reason = "element count overflow"; return r;
  }
  size_t total = element_count * element_size;
  if (alignment > 0 && (total % alignment) != 0) {
    r.reason = "alignment"; return r;
  }
  r.ok = true;
  r.total_bytes = total;
  return r;
}

AdmissionDecision admit(const Config& cfg,
                        const WireProfile& profile,
                        const std::string& request_id,
                        const std::string& schema_version,
                        const std::string& wire_profile,
                        int64_t deadline_unix_ms,
                        size_t request_bytes,
                        size_t record_count,
                        const std::string& route_shard_id,
                        uint64_t route_epoch,
                        uint64_t pool_generation,
                        uint64_t binding_generation,
                        const std::string& model_control_incarnation_id,
                        const std::vector<uint8_t>& feature_tensor_bytes,
                        const std::vector<uint32_t>& feature_shape,
                        const std::string& feature_dtype,
                        uint32_t feature_alignment) {
  AdmissionDecision d;
  d.request_id = request_id;
  d.route_shard_id = route_shard_id;
  d.route_epoch = route_epoch;
  d.pool_generation = pool_generation;
  d.binding_generation = binding_generation;
  d.model_control_incarnation_id = model_control_incarnation_id;

  // 1. schema/profile major/minor.
  try {
    assert_schema_profile(schema_version, wire_profile);
  } catch (const error::Exception& e) {
    d.verdict = AdmissionVerdict::kFail;
    d.reason_code = e.what();
    return d;
  }

  // 2. size/count/deadline/quota.
  int64_t now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count();
  if (deadline_unix_ms <= now_ms) {
    d.verdict = AdmissionVerdict::kFail;
    d.reason_code = "deadline already exceeded";
    return d;
  }
  if (request_bytes > static_cast<size_t>(profile.maximum_request_bytes)) {
    d.verdict = AdmissionVerdict::kHold;
    d.reason_code = "INFERENCE_MESSAGE_TOO_LARGE";
    return d;
  }
  if (record_count < static_cast<size_t>(profile.minimum_records_per_batch) ||
      record_count > static_cast<size_t>(profile.maximum_records_per_batch)) {
    d.verdict = AdmissionVerdict::kHold;
    d.reason_code = "record_count out of range";
    return d;
  }

  // 3. identity fields non-empty.
  if (request_id.empty() || route_shard_id.empty() ||
      model_control_incarnation_id.empty()) {
    d.verdict = AdmissionVerdict::kFail;
    d.reason_code = "identity field empty";
    return d;
  }
  if (route_epoch == 0 || pool_generation == 0 || binding_generation == 0) {
    d.verdict = AdmissionVerdict::kFail;
    d.reason_code = "route/pool/binding generation zero";
    return d;
  }

  // 4. tensor layout: name/ID/dtype/rank/shape/offset/length/alignment + integer overflow.
  auto layout = check_tensor_layout(feature_shape, feature_dtype, feature_alignment);
  if (!layout.ok) {
    d.verdict = AdmissionVerdict::kFail;
    d.reason_code = "tensor layout: " + layout.reason;
    return d;
  }
  if (feature_tensor_bytes.size() != layout.total_bytes) {
    d.verdict = AdmissionVerdict::kFail;
    d.reason_code = "feature tensor bytes mismatch";
    return d;
  }
  // reject-before-RPC: unknown dtype/shape/length already covered; NaN/Inf
  // for float dtypes.
  if (feature_dtype != "uint64-le") {
    try {
      assert_input_finite(feature_tensor_bytes, feature_dtype);
    } catch (const error::Exception& e) {
      d.verdict = AdmissionVerdict::kFail;
      d.reason_code = e.what();
      return d;
    }
  }

  // 5. Wire cannot carry path/pointer/FD/argv/executable content. We only
  // accept opaque uint64/float tensors; reject any other dtype at admission.
  if (feature_dtype != profile.tensor_dtype && feature_dtype != "float32-le" &&
      feature_dtype != "float64-le") {
    d.verdict = AdmissionVerdict::kFail;
    d.reason_code = "feature dtype not allowed on wire";
    return d;
  }

  // 6. bounded buffer admission: do NOT pre-allocate the response here.
  d.input_digest = sha256_hex(feature_tensor_bytes.data(), feature_tensor_bytes.size());
  d.accepted_records = record_count;
  d.accepted_bytes = feature_tensor_bytes.size();
  d.verdict = AdmissionVerdict::kAccept;
  d.reason_code = "accepted";
  (void)cfg;
  return d;
}

}  // namespace masi::inf