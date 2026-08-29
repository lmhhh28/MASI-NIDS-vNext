#pragma once

#include <atomic>
#include <grpcpp/grpcpp.h>

#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include "admission.h"
#include "config.h"
#include "numeric.h"
#include "readback.h"
#include "startup.h"
#include "triton_client.h"

#include "edge/v1/edge.grpc.pb.h"
#include "edge/v1/edge.pb.h"
#include "inference/v1/inference.grpc.pb.h"

namespace masi::inf {

// Public-boundary validators used by the service and exhaustive property tests.
// A non-OK status always uses RESULT_IDENTITY_MISMATCH and names the first
// mismatching fence dimension in its details.
grpc::Status validate_route_fence(const masi::edge::v1::InferenceRoute &route,
                                  const PoolReadback &readback);
grpc::Status validate_record_fence(const masi::edge::v1::InferenceRoute &route,
                                   const masi::edge::v1::InferenceRecord &record);
grpc::Status validate_triton_output_shape(const TritonInferResult &result, size_t record_count,
                                          size_t class_count);

// CentralInference gRPC service implementation.
//
// Contract summary:
//   - No durable queue. Bounded in-flight only: process-wide `max_in_flight`
//     plus exactly one in-flight batch per route target.
//   - No second batch timer and no in-process execution engine. Triton is the
//     only executor and the only delayed batcher; one admitted batch is one
//     ModelInfer call.
//   - mTLS only; the CA chain plus an exact client SAN allowlist are enforced.
//   - Result fence: 29 dimensions. Unknown/cross-generation ->
//     RESULT_IDENTITY_MISMATCH.
//   - Same request_id + same input_digest: recomputed with a new attempt id.
//   - Same request_id + different input_digest: RESULT_DIGEST_CONFLICT.
class CentralInferenceServiceImpl final : public masi::inference::v1::CentralInference::Service {
public:
  CentralInferenceServiceImpl(Config cfg, StartupResult startup,
                              std::shared_ptr<TritonClient> triton);
  ~CentralInferenceServiceImpl() override = default;

  grpc::Status GetBinding(grpc::ServerContext *ctx, const masi::edge::v1::GetBindingRequest *req,
                          masi::edge::v1::BindingReadback *resp) override;

  grpc::Status Infer(grpc::ServerContext *ctx, const masi::edge::v1::InferenceInputBatch *req,
                     masi::edge::v1::InferenceResultBatch *resp) override;

  // Health integration: drain / shutdown hooks.
  void drain_begin();
  void shutdown_complete();
  bool accepting() const noexcept;
  uint64_t in_flight() const noexcept;

private:
  // Bounded in-flight gate. Enforces the frozen profile's
  // maximum_in_flight_batches_per_target = 1 and the process-wide cap.
  class InFlightGate {
  public:
    InFlightGate(CentralInferenceServiceImpl *owner, const std::string &route_key);
    ~InFlightGate();
    bool acquired() const noexcept { return acquired_; }
    const std::string &reason() const noexcept { return reason_; }

  private:
    CentralInferenceServiceImpl *owner_;
    std::string route_key_;
    bool acquired_ = false;
    std::string reason_;
  };

  // Verify the exact binding identity of a route against the loaded binding.
  grpc::Status check_route_fence(const masi::edge::v1::InferenceRoute &route) const;

  // Fill one result record's identity, timing and adapted numeric output.
  void fill_result_record(const masi::edge::v1::InferenceRoute &route,
                          const masi::edge::v1::InferenceRecord &rec, const std::string &trace_id,
                          const std::string &worker_attempt_id,
                          masi::edge::v1::InferenceResultRecord *out) const;

  // Every execution of a request identity gets a fresh attempt id, so a
  // same-generation recompute is distinguishable from the first attempt.
  std::string next_worker_attempt_id();

  Config cfg_;
  StartupResult startup_;
  std::shared_ptr<TritonClient> triton_;
  NumericProfile numeric_;
  WireProfile profile_;
  std::string triton_model_name_;
  std::string triton_model_version_;
  std::string triton_input_name_;

  mutable std::mutex mu_;
  std::atomic<bool> accepting_{true};
  std::atomic<uint64_t> in_flight_{0};
  std::atomic<uint64_t> attempt_counter_{0};
  std::set<std::string> in_flight_routes_;

  struct RequestDigestEntry {
    std::string input_digest;
    int64_t expires_at_unix_ms = 0;
  };
  // Bounded conflict detection for the complete frozen request-deadline
  // horizon. Live entries are never evicted: capacity pressure rejects a new
  // identity instead of forgetting an old identity and accepting a conflict.
  std::unordered_map<std::string, RequestDigestEntry> request_digest_;
  static constexpr size_t kRequestCacheCap = 1024;
};

} // namespace masi::inf
