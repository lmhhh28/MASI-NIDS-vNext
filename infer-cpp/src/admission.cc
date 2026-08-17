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
  // The request batch carries the wire profile id as its schema_version
  // (frozen golden valid-batch-v1.json). Any other value, including an
  // unknown major, is rejected before complete parse.
  if (schema_version != "inference-central-grpc-batch/v1")
    throw error::Exception(error::Code::kIncompatibleContract, "schema_version unsupported: " + schema_version);
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
                        const BatchAdmissionInput& in) {
  AdmissionDecision d;
  d.request_id = in.request_id;
  d.route_shard_id = in.route_shard_id;
  d.route_epoch = in.route_epoch;
  d.pool_generation = in.pool_generation;
  d.binding_generation = in.binding_generation;
  d.model_control_incarnation_id = in.model_control_incarnation_id;

  // 1. schema/profile major/minor.
  try {
    assert_schema_profile(in.schema_version, in.wire_profile);
  } catch (const error::Exception& e) {
    d.verdict = AdmissionVerdict::kFail;
    d.reason_code = e.what();
    return d;
  }

  // 2. size/count/deadline/quota.
  int64_t now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count();
  if (in.deadline_unix_ms <= now_ms) {
    d.verdict = AdmissionVerdict::kFail;
    d.reason_code = "deadline already exceeded";
    return d;
  }
  if (in.request_bytes > static_cast<size_t>(profile.maximum_request_bytes)) {
    d.verdict = AdmissionVerdict::kHold;
    d.reason_code = "INFERENCE_MESSAGE_TOO_LARGE";
    return d;
  }
  const size_t record_count = in.records.size();
  if (record_count < static_cast<size_t>(profile.minimum_records_per_batch) ||
      record_count > static_cast<size_t>(profile.maximum_records_per_batch)) {
    d.verdict = AdmissionVerdict::kHold;
    d.reason_code = "record_count out of range";
    return d;
  }

  // 3. identity fields non-empty.
  if (in.request_id.empty() || in.route_shard_id.empty() ||
      in.model_control_incarnation_id.empty()) {
    d.verdict = AdmissionVerdict::kFail;
    d.reason_code = "identity field empty";
    return d;
  }
  if (in.route_epoch == 0 || in.pool_generation == 0 || in.binding_generation == 0) {
    d.verdict = AdmissionVerdict::kFail;
    d.reason_code = "route/pool/binding generation zero";
    return d;
  }

  // 4. per-record tensor layout, dtype allowlist, NaN/Inf and declared digest.
  // A batch is never collapsed into one aggregate tensor: every record is
  // checked against the per-record shape from the frozen profile.
  std::ostringstream batch_preimage;
  size_t accepted_bytes = 0;
  for (size_t i = 0; i < in.records.size(); ++i) {
    const auto& rec = in.records[i];
    const std::string at = " at record " + std::to_string(i);

    // The wire cannot carry path/pointer/FD/argv/executable content: only the
    // opaque numeric dtypes below are accepted.
    if (rec.dtype != profile.tensor_dtype && rec.dtype != "float32-le" &&
        rec.dtype != "float64-le") {
      d.verdict = AdmissionVerdict::kFail;
      d.reason_code = "feature dtype not allowed on wire" + at;
      return d;
    }
    if (rec.shape.size() != profile.tensor_shape.size()) {
      d.verdict = AdmissionVerdict::kFail;
      d.reason_code = "tensor rank mismatch" + at;
      return d;
    }
    for (size_t k = 0; k < rec.shape.size(); ++k) {
      if (rec.shape[k] != static_cast<uint32_t>(profile.tensor_shape[k])) {
        d.verdict = AdmissionVerdict::kFail;
        d.reason_code = "tensor shape != profile record shape" + at;
        return d;
      }
    }
    auto layout = check_tensor_layout(rec.shape, rec.dtype, in.alignment);
    if (!layout.ok) {
      d.verdict = AdmissionVerdict::kFail;
      d.reason_code = "tensor layout: " + layout.reason + at;
      return d;
    }
    if (rec.bytes == nullptr) {
      d.verdict = AdmissionVerdict::kFail;
      d.reason_code = "feature tensor missing" + at;
      return d;
    }
    if (rec.byte_count != layout.total_bytes) {
      d.verdict = AdmissionVerdict::kFail;
      d.reason_code = "feature tensor bytes mismatch" + at;
      return d;
    }
    if (rec.dtype != "uint64-le") {
      try {
        assert_input_finite(std::vector<uint8_t>(rec.bytes, rec.bytes + rec.byte_count),
                            rec.dtype);
      } catch (const error::Exception& e) {
        d.verdict = AdmissionVerdict::kFail;
        d.reason_code = std::string(e.what()) + at;
        return d;
      }
    }
    // The declared per-record input_digest is a result-fence dimension, so it
    // is recomputed here instead of being copied through to the result.
    if (rec.input_digest.empty()) {
      d.verdict = AdmissionVerdict::kFail;
      d.reason_code = "record input_digest empty" + at;
      return d;
    }
    const std::string computed = sha256_hex(rec.bytes, rec.byte_count);
    if (!digest_match(computed, rec.input_digest)) {
      d.verdict = AdmissionVerdict::kFail;
      d.reason_code = "record input_digest mismatch" + at;
      return d;
    }
    batch_preimage << computed << '\n';
    accepted_bytes += rec.byte_count;
  }

  // 5. bounded buffer admission: do NOT pre-allocate the response here.
  const std::string preimage = batch_preimage.str();
  d.input_digest = sha256_hex(preimage.data(), preimage.size());
  d.accepted_records = record_count;
  d.accepted_bytes = accepted_bytes;
  d.verdict = AdmissionVerdict::kAccept;
  d.reason_code = "accepted";
  (void)cfg;
  return d;
}

}  // namespace masi::inf