#pragma once

#include <grpcpp/grpcpp.h>

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "error.h"
#include "triton_grpc.pb.h"
#include "triton_grpc.grpc.pb.h"

namespace masi::inf {

struct TritonModelIdentity {
  std::string name;
  std::string version;
  std::string repository_digest;
  std::string config_digest;
};

struct TritonModelConfig {
  std::string raw_config;
  std::string model_control_mode;
  int32_t max_batch_size = 0;
  std::vector<int32_t> preferred_batch_sizes;
  int32_t max_queue_delay_microseconds = 0;
  int32_t max_queue_records = 0;
  bool dynamic_batching = false;
  std::string instance_group_kind;
  int32_t instance_group_count = 0;
};

struct TritonModelMetadata {
  std::string raw_metadata;
  std::string name;
  std::string version;
  std::vector<std::string> inputs;
  std::vector<std::string> outputs;
};

class TritonClient {
 public:
  TritonClient() = default;
  ~TritonClient() = default;

  TritonClient(const TritonClient&) = delete;
  TritonClient& operator=(const TritonClient&) = delete;

  struct ConnectOptions {
    std::string endpoint;
    std::string tls_ca;
    std::string tls_cert;
    std::string tls_key;
    int32_t deadline_ms = 2000;
    int32_t max_message_bytes = 16777216;
  };

  bool available() const noexcept { return available_; }

  void connect(const ConnectOptions& opts);
  bool is_server_ready();
  bool is_model_ready(const std::string& name, const std::string& version);

  std::vector<std::string> model_repository_index();

  TritonModelMetadata model_metadata(const std::string& name,
                                     const std::string& version);
  TritonModelConfig model_config(const std::string& name,
                                 const std::string& version);

  std::vector<float> model_infer(const std::string& name,
                                 const std::string& version,
                                 const std::vector<uint8_t>& input_bytes,
                                 const std::vector<int64_t>& input_shape,
                                 size_t out_count);

 private:
  bool available_ = false;
  ConnectOptions opts_;
  std::shared_ptr<grpc::Channel> channel_;
  std::unique_ptr<inference::GRPCInferenceService::Stub> stub_;
};

}  // namespace masi::inf