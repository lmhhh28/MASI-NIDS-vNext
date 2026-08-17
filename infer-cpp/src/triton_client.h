#pragma once

#include <grpcpp/grpcpp.h>

#include <cstdint>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "error.h"
// Stubs generated from the vendored upstream Triton protocol definition
// (proto/vendor/triton, provenance registered in
// contracts/supply-chain/v1/central-inference-vendored-sources.json).
#include "grpc_service.pb.h"
#include "grpc_service.grpc.pb.h"

namespace masi::inf {

struct TritonServerMetadata {
  std::string name;
  std::string version;
  std::vector<std::string> extensions;
};

struct TritonModelMetadata {
  std::string raw_metadata;      // canonical projection, not the raw protobuf
  std::string name;
  std::string version;
  std::vector<std::string> inputs;
  std::vector<std::string> outputs;
  std::string input_datatype;
  std::string output_datatype;
  std::vector<int64_t> input_shape;
  std::vector<int64_t> output_shape;
};

// Exactly the ModelConfig fields the startup gate compares against the frozen
// wire profile and the pinned on-disk config.pbtxt.
struct TritonModelConfigProjection {
  std::string name;
  std::string platform;
  std::string backend;
  int32_t max_batch_size = 0;
  bool dynamic_batching = false;
  std::vector<int32_t> preferred_batch_size;
  int64_t max_queue_delay_microseconds = 0;
  int32_t max_queue_size = 0;
  // instance_group entries as (kind, count) in declaration order.
  std::vector<std::pair<std::string, int32_t>> instance_groups;
  std::string input_name;
  std::string input_datatype;
  std::vector<int64_t> input_dims;
  std::string output_name;
  std::string output_datatype;
  std::vector<int64_t> output_dims;
  // Stable newline-delimited projection used as readback evidence digest input.
  std::string canonical_projection;
};

struct TritonModelStatistics {
  bool observed = false;
  uint64_t inference_count = 0;
  uint64_t execution_count = 0;
  uint64_t success_count = 0;
};

struct TritonInferResult {
  std::vector<float> values;
  std::vector<int64_t> shape;
  std::string datatype;
};

// Gateway-to-Triton client restricted to the read-only control methods plus
// ModelInfer. Model-control and shared-memory RPCs are never called; see the
// method allowlist in the vendored-source registry.
class TritonClient {
 public:
  TritonClient() = default;
  ~TritonClient() = default;

  TritonClient(const TritonClient&) = delete;
  TritonClient& operator=(const TritonClient&) = delete;

  struct ConnectOptions {
    std::string endpoint;
    // Optional mTLS material for the Gateway-to-Triton channel. When empty,
    // the endpoint must resolve to loopback (enforced by connect()).
    std::string tls_ca;
    std::string tls_cert;
    std::string tls_key;
    std::string tls_target_name;   // exact SAN expected from Triton
    int32_t deadline_ms = 2000;
    int32_t max_message_bytes = 16777216;
  };

  bool available() const noexcept { return available_; }
  const std::string& endpoint() const noexcept { return opts_.endpoint; }
  bool tls_enabled() const noexcept { return !opts_.tls_ca.empty(); }

  // Fails closed when a plaintext channel is requested for a non-loopback
  // endpoint: "same host" is never treated as an identity.
  void connect(const ConnectOptions& opts);

  bool is_server_ready(int32_t deadline_ms = 0);
  bool is_model_ready(const std::string& name, const std::string& version,
                      int32_t deadline_ms = 0);

  TritonServerMetadata server_metadata(int32_t deadline_ms = 0);
  TritonModelMetadata model_metadata(const std::string& name,
                                     const std::string& version,
                                     int32_t deadline_ms = 0);
  TritonModelConfigProjection model_config(const std::string& name,
                                           const std::string& version,
                                           int32_t deadline_ms = 0);
  TritonModelStatistics model_statistics(const std::string& name,
                                         const std::string& version,
                                         int32_t deadline_ms = 0);

  // One batched inference call for a whole admitted batch. `input_shape` is the
  // full request shape (batch dimension first). `deadline_ms` is the remaining
  // budget of the originating Edge request; `parent` propagates the Edge
  // deadline and cancellation when provided.
  TritonInferResult model_infer(const std::string& name,
                                const std::string& version,
                                const std::string& input_name,
                                const std::string& input_datatype,
                                const std::vector<uint8_t>& input_bytes,
                                const std::vector<int64_t>& input_shape,
                                int32_t deadline_ms,
                                const grpc::ServerContext* parent = nullptr);

 private:
  bool available_ = false;
  ConnectOptions opts_;
  std::shared_ptr<grpc::Channel> channel_;
  std::unique_ptr<::inference::GRPCInferenceService::Stub> stub_;
};

}  // namespace masi::inf
