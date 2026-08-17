#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "config.h"
#include "envelope.h"
#include "numeric.h"
#include "readback.h"
#include "repository_closure.h"
#include "triton_client.h"

namespace masi::inf {

struct StartupResult {
  bool ready = false;
  PoolReadback readback;
  std::vector<StageResult> stages;
  std::string failure_reason;
  int64_t started_at_unix_ms = 0;
  int64_t completed_at_unix_ms = 0;

  // Verified data configuration and the observation it was checked against.
  BundleManifest bundle;
  TritonObservation observed;
  // Output adapter parameters derived from `bundle` (never hardcoded).
  NumericProfile numeric;
  // Exact Triton model identity resolved from the digest-pinned closure.
  std::string triton_model_name;
  std::string triton_model_version;
};

// Expectations the frozen wire profile places on the serving runtime.
// contracts/inference/v1/profile.json#triton_execution_expectation
struct TritonExecutionExpectation {
  int32_t max_batch_size = 256;
  std::vector<int32_t> preferred_batch_size{32, 64, 128, 256};
  int64_t max_queue_delay_microseconds = 200;
  int32_t max_queue_size = 1024;
  bool dynamic_batching = true;
  std::string backend = "onnxruntime";
};

// Fixed startup sequence (fail closed on any mismatch):
//   1. read-profile
//   2. verify-envelope
//   3. verify-repository            (digest-pinned read-only closure)
//   4. verify-bundle-manifest       (adapter data + envelope digest cross-check)
//   5. triton-none-load             (ServerReady/ModelReady/ServerMetadata/
//                                    ModelMetadata/ModelConfig three-way check
//                                    against the frozen profile and the pinned
//                                    config.pbtxt)
//   6. warmup-numeric-self-test     (deterministic repeat + batch equivalence
//                                    executed through Triton, the only
//                                    execution plane)
//   7. gateway-readback             (observation-derived, no envelope echo)
//   8. readiness
StartupResult run_startup(const Config& cfg, TritonClient& triton, int64_t now_unix_ms);

// Build the deterministic adapter parameters from the verified bundle manifest.
NumericProfile make_numeric_profile(const BundleManifest& bundle);

// Parse the pinned Triton `config.pbtxt` text into the same projection shape the
// live ModelConfig RPC produces, so the two can be compared field by field.
TritonModelConfigProjection parse_pinned_config_text(const std::string& text);

// Compare the pinned config, the live Triton config and the frozen profile.
// Throws on any mismatch.
void assert_triton_config_matches(const TritonModelConfigProjection& live,
                                  const TritonModelConfigProjection& pinned,
                                  const TritonExecutionExpectation& expected,
                                  const BundleManifest& bundle,
                                  const StartupEnvelope& env);

}  // namespace masi::inf
