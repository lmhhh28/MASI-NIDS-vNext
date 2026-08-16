#pragma once

#include <atomic>
#include <grpcpp/grpcpp.h>

#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>

#include "admission.h"
#include "config.h"
#include "numeric.h"
#include "ort_session.h"
#include "readback.h"
#include "startup.h"
#include "triton_client.h"

#include "edge/v1/edge.grpc.pb.h"
#include "edge/v1/edge.pb.h"
#include "inference/v1/inference.grpc.pb.h"

namespace masi::inf {

// CentralInference gRPC service implementation.
//
// Contract summary:
//   - No durable queue. Bounded in-flight only.
//   - No second batch timer. Triton is the only delayed batcher.
//   - mTLS only; peer verification is done by gRPC.
//   - Result fence: 29 dimensions. Unknown/cross-generation -> RESULT_IDENTITY_MISMATCH.
//   - Same request_id + same input_digest: idempotent.
//   - Same request_id + different input_digest: RESULT_DIGEST_CONFLICT.
class CentralInferenceServiceImpl final
    : public masi::inference::v1::CentralInference::Service {
 public:
  CentralInferenceServiceImpl(Config cfg,
                              StartupResult startup,
                              std::shared_ptr<OrtSession> session,
                              std::shared_ptr<TritonClient> triton,
                              NumericProfile numeric);
  ~CentralInferenceServiceImpl() override = default;

  grpc::Status GetBinding(grpc::ServerContext* ctx,
                          const masi::edge::v1::GetBindingRequest* req,
                          masi::edge::v1::BindingReadback* resp) override;

  grpc::Status Infer(grpc::ServerContext* ctx,
                     const masi::edge::v1::InferenceInputBatch* req,
                     masi::edge::v1::InferenceResultBatch* resp) override;

  // Health integration: drain / shutdown hooks.
  void drain_begin();
  void shutdown_complete();
  bool accepting() const noexcept;

  // Bounded idempotency cache: request_id -> (input_digest, output_digest).
  struct IdempEntry { std::string input_digest; std::string output_digest; };

 private:
  grpc::Status infer_one(const masi::edge::v1::InferenceRoute& route,
                         const masi::edge::v1::InferenceRecord& rec,
                         int64_t deadline_unix_ms,
                         const std::string& trace_id,
                         masi::edge::v1::InferenceResultRecord* out);

  Config cfg_;
  StartupResult startup_;
  std::shared_ptr<OrtSession> session_;
  std::shared_ptr<TritonClient> triton_;
  NumericProfile numeric_;
  WireProfile profile_;

  mutable std::mutex mu_;
  std::atomic<bool> accepting_{true};

  std::unordered_map<std::string, IdempEntry> idemp_;
  static constexpr size_t kIdempCap = 1024;
};

}  // namespace masi::inf