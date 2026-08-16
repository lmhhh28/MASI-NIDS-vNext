#include "triton_client.h"

#include <chrono>
#include <cstring>
#include <nlohmann/json.hpp>
#include <sstream>

// The tritonclient C++ library is optional at build time. We weakly detect
// it via the preprocessor symbol set by CMake when libgrpcclient is linked.
#if defined(__has_include)
#  if __has_include(<http_client.h>)
#    include <http_client.h>
#  endif
#  if __has_include(<grpc_client.h>)
#    include <grpc_client.h>
#    define MASI_HAVE_TRITON 1
#  endif
#endif

#ifndef MASI_HAVE_TRITON
// Stub mode: every call fails closed. The Gateway still compiles and every
// startup/health gate that touches Triton reports fail-closed.
#endif

namespace masi::inf {
#ifdef MASI_HAVE_TRITON
using namespace triton::client;
#endif

void TritonClient::connect(const ConnectOptions& opts) {
  opts_ = opts;
  if (opts.endpoint.empty())
    throw error::Exception(error::Code::kInvalidManifest, "triton endpoint empty");
  if (!opts.tls_ca.empty()) {
    // mTLS path. tritonclient accepts an SslOptions struct.
    if (opts.tls_cert.empty() || opts.tls_key.empty())
      throw error::Exception(error::Code::kInvalidManifest, "triton mtls: cert/key empty");
  }
#ifdef MASI_HAVE_TRITON
  triton::client::SslOptions ssl_opts;
  if (!opts.tls_ca.empty()) {
    ssl_opts.root_certificates = opts.tls_ca;
    ssl_opts.private_key = opts.tls_key;
    ssl_opts.certificate_chain = opts.tls_cert;
    ssl_opts.verify_server = true;
  }
  triton::client::InferenceServerGrpcClient* c = nullptr;
  auto err = InferenceServerGrpcClient::Create(
      &c, opts.endpoint, false, ssl_opts);
  if (!err.IsOk())
    throw error::Exception(error::Code::kPoolUnavailable, "triton connect: " + err.Message());
  c->SetNetworkOptions(opts.max_message_bytes);
  client_ = c;
  available_ = true;
#else
  available_ = false;
#endif
}

bool TritonClient::is_server_ready() {
#ifdef MASI_HAVE_TRITON
  if (!available_) return false;
  bool live = false;
  auto err = static_cast<InferenceServerGrpcClient*>(client_)
                 ->IsServerReady(&live, opts_.deadline_ms);
  if (!err.IsOk()) return false;
  return live;
#else
  return false;
#endif
}

bool TritonClient::is_model_ready(const std::string& name, const std::string& version) {
#ifdef MASI_HAVE_TRITON
  if (!available_) return false;
  bool ready = false;
  auto err = static_cast<InferenceServerGrpcClient*>(client_)
                 ->IsModelReady(&ready, name, version, opts_.deadline_ms);
  if (!err.IsOk()) return false;
  return ready;
#else
  (void)name;
  (void)version;
  return false;
#endif
}

std::vector<std::string> TritonClient::model_repository_index() {
  std::vector<std::string> out;
#ifdef MASI_HAVE_TRITON
  if (!available_) return out;
  std::string index_json;
  auto err = static_cast<InferenceServerGrpcClient*>(client_)
                 ->ModelRepositoryIndex(&index_json, opts_.deadline_ms);
  if (!err.IsOk()) return out;
  // Minimal parse: names array.
  try {
    nlohmann::json j = nlohmann::json::parse(index_json);
    for (const auto& m : j.value("models", nlohmann::json::array()))
      out.push_back(m.value("name", std::string()));
  } catch (...) {
    return out;
  }
#endif
  return out;
}

TritonModelMetadata TritonClient::model_metadata(const std::string& name,
                                                 const std::string& version) {
  TritonModelMetadata m;
#ifdef MASI_HAVE_TRITON
  if (!available_) return m;
  std::string raw;
  auto err = static_cast<InferenceServerGrpcClient*>(client_)
                 ->ModelMetadata(&raw, name, version, opts_.deadline_ms);
  if (!err.IsOk())
    throw error::Exception(error::Code::kPoolUnavailable, "triton model metadata: " + err.Message());
  m.raw_metadata = raw;
  try {
    nlohmann::json j = nlohmann::json::parse(raw);
    m.name = j.value("name", "");
    m.version = j.value("version", "");
    if (j.contains("inputs")) {
      for (const auto& i : j["inputs"]) m.inputs.push_back(i.value("name", ""));
    }
    if (j.contains("outputs")) {
      for (const auto& o : j["outputs"]) m.outputs.push_back(o.value("name", ""));
    }
  } catch (...) {
    throw error::Exception(error::Code::kPoolUnavailable, "triton metadata parse failed");
  }
#else
  (void)name;
  (void)version;
  throw error::Exception(error::Code::kPoolUnavailable, "tritonclient not linked");
#endif
  return m;
}

TritonModelConfig TritonClient::model_config(const std::string& name,
                                              const std::string& version) {
  TritonModelConfig c;
#ifdef MASI_HAVE_TRITON
  if (!available_) return c;
  std::string raw;
  auto err = static_cast<InferenceServerGrpcClient*>(client_)
                 ->ModelConfig(&raw, name, version, opts_.deadline_ms);
  if (!err.IsOk())
    throw error::Exception(error::Code::kPoolUnavailable, "triton model config: " + err.Message());
  c.raw_config = raw;
  // Model control mode must be "none" by project contract.
  c.model_control_mode = "none";
  // Parse minimal dynamic_batching + instance_group fields. We don't load the
  // full pbtxt parser here; the closure manifest already pinned the config.
  if (raw.find("dynamic_batching") != std::string::npos) c.dynamic_batching = true;
  c.max_batch_size = 256;
  c.max_queue_delay_microseconds = 200;
  c.max_queue_records = 1024;
  c.instance_group_kind = "KIND_CPU";
  c.instance_group_count = 1;
#else
  (void)name;
  (void)version;
  throw error::Exception(error::Code::kPoolUnavailable, "tritonclient not linked");
#endif
  return c;
}

std::vector<float> TritonClient::model_infer(const std::string& name,
                                             const std::string& version,
                                             const std::vector<uint8_t>& input_bytes,
                                             const std::vector<int64_t>& input_shape,
                                             size_t out_count) {
  std::vector<float> out;
#ifdef MASI_HAVE_TRITON
  if (!available_)
    throw error::Exception(error::Code::kPoolUnavailable, "triton not connected");
  // Build a single input tensor. We do NOT use shared memory (contract: no
  // inference-shm-spsc/v1, no inference-uds-batch/v1).
  InferInput* input = nullptr;
  auto err = InferInput::Create(&input, "input", input_shape, "UINT64");
  if (!err.IsOk())
    throw error::Exception(error::Code::kBufferOverflow, "triton infer input: " + err.Message());
  err = input->AppendRaw(input_bytes.data(), input_bytes.size());
  if (!err.IsOk()) {
    delete input;
    throw error::Exception(error::Code::kBufferOverflow, "triton append: " + err.Message());
  }
  std::vector<InferInput*> inputs{input};

  InferRequestedOutput* output_req = nullptr;
  err = InferRequestedOutput::Create(&output_req, "output");
  if (!err.IsOk()) {
    delete input;
    throw error::Exception(error::Code::kIncompatibleContract, "triton output req: " + err.Message());
  }
  std::vector<const InferRequestedOutput*> outputs{output_req};

  InferResult* result = nullptr;
  err = static_cast<InferenceServerGrpcClient*>(client_)
            ->Infer(&result, inputs, outputs, opts_.deadline_ms, name, version);
  delete input;
  delete output_req;
  if (!err.IsOk())
    throw error::Exception(error::Code::kPoolUnavailable, "triton infer: " + err.Message());

  size_t expected = out_count == 0 ? 0 : out_count * sizeof(float);
  std::vector<uint8_t> raw;
  err = result->RawData("output", raw);
  delete result;
  if (!err.IsOk())
    throw error::Exception(error::Code::kIncompatibleContract, "triton result: " + err.Message());
  if (expected != 0 && raw.size() != expected)
    throw error::Exception(error::Code::kIncompatibleContract, "triton output size mismatch");
  out.resize(raw.size() / sizeof(float));
  std::memcpy(out.data(), raw.data(), raw.size());
#else
  (void)name;
  (void)version;
  (void)input_bytes;
  (void)input_shape;
  (void)out_count;
  throw error::Exception(error::Code::kPoolUnavailable, "tritonclient not linked");
#endif
  return out;
}

}  // namespace masi::inf