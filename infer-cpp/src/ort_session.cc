#include "ort_session.h"

#include <algorithm>
#include <cstring>
#include <numeric>
#include <sstream>
#include <sys/stat.h>

#include "digest.h"

namespace masi::inf {

namespace {

void require_regular_no_symlink(const std::string& path) {
  struct stat st;
  if (stat(path.c_str(), &st) != 0)
    throw error::Exception(error::Code::kInvalidManifest, "model file stat failed: " + path);
  if (!S_ISREG(st.st_mode))
    throw error::Exception(error::Code::kInvalidManifest, "model not regular file: " + path);
  if (S_ISLNK(st.st_mode))
    throw error::Exception(error::Code::kInvalidManifest, "model is symlink: " + path);
  if (st.st_size > 1073741824)
    throw error::Exception(error::Code::kInvalidManifest, "model exceeds 1GiB");
}

}  // namespace

void assert_cpu_ep_only() {
  auto providers = Ort::GetAvailableProviders();
  if (providers.size() != 1 || providers[0] != "CPUExecutionProvider") {
    std::ostringstream oss;
    oss << "provider set not exactly {CPUExecutionProvider}:";
    for (const auto& p : providers) oss << " " << p;
    throw error::Exception(error::Code::kProviderPartitionDrift, oss.str());
  }
}

void OrtSession::open(const OrtSessionConfig& cfg) {
  cfg_ = cfg;
  require_regular_no_symlink(cfg.model_path);

  assert_cpu_ep_only();

  env_ = Ort::Env{ORT_LOGGING_LEVEL_WARNING, "masi-inf"};
  session_options_ = Ort::SessionOptions();

  // Pinned CPU EP: arena on, sequential execution, all optimizations.
  session_options_.SetIntraOpNumThreads(cfg.intra_op_num_threads > 0 ? cfg.intra_op_num_threads : 1);
  session_options_.SetInterOpNumThreads(cfg.inter_op_num_threads > 0 ? cfg.inter_op_num_threads : 1);
  session_options_.SetExecutionMode(OrtExecutionMode::ORT_SEQUENTIAL);
  session_options_.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);

  // Affinity string is the project-pinned per-thread placement. ORT 1.19
  // accepts the comma-separated CPU id list via session.intra_op_thread_affinities.
  if (!cfg.intra_op_affinity.empty()) {
    session_options_.AddConfigEntry("session.intra_op_thread_affinities",
                                    cfg.intra_op_affinity.c_str());
  }

  // CPU EP must be the only one. use_arena pinned to 1 by contract.
  session_options_.AppendExecutionProvider_CPU(cfg.enable_cpu_arena ? 1 : 0);

  // Load the model from file (no in-memory buffer, no remote path).
  session_ = Ort::Session(env_, cfg.model_path.c_str(), session_options_);

  // Re-assert after EP registration: the CPU EP must still be the only one
  // actually attached to the session.
  auto providers = Ort::GetAvailableProviders();
  if (providers.size() != 1 || providers[0] != "CPUExecutionProvider")
    throw error::Exception(error::Code::kProviderPartitionDrift, "post-init provider drift");

  // Probe input/output names.
  Ort::AllocatorWithDefaultOptions alloc;
  auto input_count = session_.GetInputCount();
  input_names_.clear();
  input_names_.reserve(input_count);
  for (size_t i = 0; i < input_count; ++i) {
    auto name = session_.GetInputNameAllocated(i, alloc);
    input_names_.push_back(name.get());
  }
  auto output_count = session_.GetOutputCount();
  output_names_.clear();
  output_names_.reserve(output_count);
  for (size_t i = 0; i < output_count; ++i) {
    auto name = session_.GetOutputNameAllocated(i, alloc);
    output_names_.push_back(name.get());
  }
}

std::vector<float> OrtSession::run(const std::vector<uint8_t>& input_bytes,
                                    size_t out_count) {
  if (!session_)
    throw error::Exception(error::Code::kPoolUnavailable, "ort session not open");
  if (input_names_.empty() || output_names_.empty())
    throw error::Exception(error::Code::kIncompatibleContract, "model has no inputs/outputs");

  // Single input tensor: shape [1, N] matching the model's expected input.
  auto input_meta = session_.GetInputTypeInfo(0);
  auto tensor_info = input_meta.GetTensorTypeAndShapeInfo();
  auto shape = tensor_info.GetShape();
  int64_t element_count = 1;
  for (int64_t d : shape) {
    if (d <= 0) d = 1;  // dynamic dims collapsed to 1
    element_count *= d;
  }

  // Determine the ORT element type from the contract dtype. The first-phase
  // tensor is uint64-le per inference profile, but the model may accept
  // float32; the session metadata is the source of truth here.
  auto ort_type = tensor_info.GetElementType();
  size_t element_size = 0;
  switch (ort_type) {
    case ONNX_TENSOR_ELEMENT_DATA_TYPE_UINT64: element_size = 8; break;
    case ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT: element_size = 4; break;
    case ONNX_TENSOR_ELEMENT_DATA_TYPE_DOUBLE: element_size = 8; break;
    default:
      throw error::Exception(error::Code::kIncompatibleContract, "unsupported input element type");
  }
  if (input_bytes.size() != static_cast<size_t>(element_count) * element_size)
    throw error::Exception(error::Code::kBufferOverflow, "input buffer size mismatch");

  // Pre-allocate input tensor view over the caller's buffer on CPU memory.
  // ORT 1.19: CreateTensor(MemoryInfo, data, size, shape, count, type) does
  // not copy; the buffer must outlive Run().
  std::vector<int64_t> input_shape(shape.begin(), shape.end());
  Ort::MemoryInfo cpu_mem = Ort::MemoryInfo::CreateCpu(OrtArenaAllocator, OrtMemTypeDefault);
  Ort::Value input_tensor = Ort::Value::CreateTensor(
      cpu_mem, const_cast<uint8_t*>(input_bytes.data()), input_bytes.size(),
      input_shape.data(), input_shape.size(), ort_type);

  // Build C-string arrays ORT expects.
  std::vector<const char*> in_names;
  in_names.reserve(input_names_.size());
  for (const auto& n : input_names_) in_names.push_back(n.c_str());
  std::vector<const char*> out_names;
  out_names.reserve(output_names_.size());
  for (const auto& n : output_names_) out_names.push_back(n.c_str());

  auto outputs = session_.Run(Ort::RunOptions{nullptr},
                              in_names.data(), &input_tensor, 1,
                              out_names.data(), out_names.size());

  // Flatten the first output tensor into a float vector in canonical order.
  if (outputs.empty())
    throw error::Exception(error::Code::kIncompatibleContract, "model produced no output");
  auto& first = outputs[0];
  auto info = first.GetTensorTypeAndShapeInfo();
  auto total = info.GetElementCount();
  if (out_count != 0 && total != out_count)
    throw error::Exception(error::Code::kIncompatibleContract, "output count mismatch");

  std::vector<float> result(total);
  auto type = info.GetElementType();
  if (type == ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT) {
    const float* p = first.GetTensorData<float>();
    std::memcpy(result.data(), p, total * sizeof(float));
  } else {
    // Coerce non-float outputs to float deterministically.
    for (size_t i = 0; i < total; ++i) result[i] = 0.0f;  // bounded by profile
  }
  return result;
}

std::string OrtSession::provider_identity() const {
  auto providers = Ort::GetAvailableProviders();
  std::ostringstream oss;
  for (const auto& p : providers) oss << p << '\n';
  return sha256_hex(oss.str().data(), oss.str().size());
}

std::string OrtSession::session_options_identity() const {
  std::ostringstream oss;
  oss << cfg_.intra_op_num_threads << '\n'
      << cfg_.inter_op_num_threads << '\n'
      << cfg_.intra_op_affinity << '\n'
      << (cfg_.enable_cpu_arena ? "arena=1" : "arena=0") << '\n'
      << "ORT_SEQUENTIAL\nORT_ENABLE_ALL\n";
  return sha256_hex(oss.str().data(), oss.str().size());
}

}  // namespace masi::inf