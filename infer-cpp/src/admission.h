#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "config.h"
#include "error.h"

namespace masi::inf {

// Wire-profile constants from contracts/inference/v1/profile.json. The
// admission pipeline rejects anything outside these bounds before any large
// allocation or model execution.
struct WireProfile {
  std::string schema_version = "inference-central-grpc-batch-profile/v1";
  std::string profile_id = "inference-central-grpc-batch/v1";
  int32_t minimum_records_per_batch = 1;
  int32_t maximum_records_per_batch = 256;
  int32_t maximum_request_bytes = 4194304;
  int32_t maximum_response_bytes = 4194304;
  int32_t maximum_in_flight_batches_per_target = 1;
  int32_t maximum_eligible_workers_per_pool_readback = 64;
  int32_t request_deadline_ms = 2000;
  std::string tensor_dtype = "uint64-le";
  std::vector<int32_t> tensor_shape = {1, 6};
  int32_t bytes_per_record = 48;
};

// Result of the admission pipeline. HOLD means the request is not currently
// admissible; FAIL means the request itself is malformed and must not be
// retried with the same identity.
enum class AdmissionVerdict { kAccept, kHold, kFail };

struct AdmissionDecision {
  AdmissionVerdict verdict = AdmissionVerdict::kHold;
  std::string reason_code;
  std::string request_id;
  std::string route_shard_id;
  uint64_t route_epoch = 0;
  uint64_t pool_generation = 0;
  uint64_t binding_generation = 0;
  std::string model_control_incarnation_id;
  std::string input_digest;          // sha256: over accepted input bytes
  size_t accepted_records = 0;
  size_t accepted_bytes = 0;
};

// The 29 result-fence dimensions, in exact order from profile.json.
const std::vector<std::string>& result_fence_dimensions();

// Verify schema/profile major/minor compatibility. The wire profile is
// `inference-central-grpc-batch/v1` (major 1). Unknown major rejected.
void assert_schema_profile(const std::string& schema_version,
                            const std::string& wire_profile);

// Bounded checked-arithmetic tensor layout check: name/ID/dtype/rank/shape/
// offset/length/alignment, with integer-overflow protection.
struct TensorLayoutCheck {
  bool ok = false;
  size_t total_bytes = 0;          // checked-sum, 0 if overflow
  std::string reason;
};
TensorLayoutCheck check_tensor_layout(const std::vector<uint32_t>& shape,
                                      const std::string& dtype,
                                      uint32_t alignment);

// Run the full admission pipeline against an InferenceInputBatch. Rejects
// before any large allocation or model execution. The wire cannot carry
// path/pointer/FD/argv/executable content; `bytes` is the raw feature tensor
// and is only inspected for size/dtype/NaN where applicable.
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
                        uint32_t feature_alignment);

}  // namespace masi::inf