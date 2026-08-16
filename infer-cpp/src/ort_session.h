#pragma once

#include <onnxruntime_cxx_api.h>

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "error.h"

namespace masi::inf {

struct OrtSessionConfig {
  std::string model_path;          // path to .onnx file (no symlink)
  int32_t intra_op_num_threads = 0;   // 0 => ORT default
  int32_t inter_op_num_threads = 0;
  std::string intra_op_affinity;  // comma-separated per-thread affinity
  bool enable_cpu_arena = true;
};

class OrtSession {
 public:
  OrtSession() = default;
  ~OrtSession() = default;

  OrtSession(const OrtSession&) = delete;
  OrtSession& operator=(const OrtSession&) = delete;

  // Initialize the Env, SessionOptions, CPU EP, and load the model. Asserts
  // GetAvailableProviders() returns exactly {"CPUExecutionProvider"}.
  void open(const OrtSessionConfig& cfg);

  bool is_open() const noexcept { return session_ != nullptr; }

  // Run the model against a pre-validated input buffer. `input_bytes` is the
  // raw tensor bytes for the single model input. `out_count` is the expected
  // number of float output values.
  std::vector<float> run(const std::vector<uint8_t>& input_bytes,
                          size_t out_count);

  // Stable identity for readback: provider set + session options digest.
  std::string provider_identity() const;
  std::string session_options_identity() const;

  const std::vector<std::string>& input_names() const noexcept { return input_names_; }
  const std::vector<std::string>& output_names() const noexcept { return output_names_; }

 private:
  Ort::Env env_{nullptr};
  Ort::Session session_{nullptr};
  Ort::SessionOptions session_options_{nullptr};
  std::vector<std::string> input_names_;
  std::vector<std::string> output_names_;
  OrtSessionConfig cfg_;
};

// Assert exactly {"CPUExecutionProvider"} available. Fail closed otherwise.
void assert_cpu_ep_only();

}  // namespace masi::inf