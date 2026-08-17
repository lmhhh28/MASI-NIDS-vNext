#include "triton_client.h"

#include <grpcpp/grpcpp.h>

#include <algorithm>
#include <chrono>
#include <cstring>
#include <sstream>

namespace masi::inf {

namespace {

// Endpoint host part of "host:port" (IPv6 literals use "[addr]:port").
std::string host_of(const std::string& endpoint) {
  if (!endpoint.empty() && endpoint[0] == '[') {
    const auto close = endpoint.find(']');
    if (close == std::string::npos) return "";
    return endpoint.substr(1, close - 1);
  }
  const auto colon = endpoint.rfind(':');
  if (colon == std::string::npos) return endpoint;
  return endpoint.substr(0, colon);
}

bool is_loopback_host(const std::string& host) {
  if (host == "localhost" || host == "::1") return true;
  // 127.0.0.0/8
  unsigned a = 0, b = 0, c = 0, d = 0;
  if (std::sscanf(host.c_str(), "%u.%u.%u.%u", &a, &b, &c, &d) == 4)
    return a == 127 && b < 256 && c < 256 && d < 256;
  return false;
}

void set_deadline(grpc::ClientContext& ctx, int32_t deadline_ms, int32_t fallback_ms) {
  const int32_t ms = deadline_ms > 0 ? deadline_ms : fallback_ms;
  ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(ms > 0 ? ms : 2000));
}

std::string join_i64(const std::vector<int64_t>& v) {
  std::ostringstream oss;
  for (size_t i = 0; i < v.size(); ++i) {
    if (i) oss << ',';
    oss << v[i];
  }
  return oss.str();
}

std::string join_i32(const std::vector<int32_t>& v) {
  std::ostringstream oss;
  for (size_t i = 0; i < v.size(); ++i) {
    if (i) oss << ',';
    oss << v[i];
  }
  return oss.str();
}

}  // namespace

void TritonClient::connect(const ConnectOptions& opts) {
  opts_ = opts;
  if (opts.endpoint.empty())
    throw error::Exception(error::Code::kInvalidManifest, "triton endpoint empty");

  const bool tls = !opts.tls_ca.empty();
  if (!tls && !is_loopback_host(host_of(opts.endpoint)))
    throw error::Exception(error::Code::kIncompatibleContract,
                           "plaintext Triton channel requires a loopback endpoint; "
                           "configure triton_tls_* for " + opts.endpoint);
  if (tls && (opts.tls_cert.empty() || opts.tls_key.empty()))
    throw error::Exception(error::Code::kInvalidManifest,
                           "triton mTLS requires both client cert and key");
  if (tls && opts.tls_target_name.empty())
    throw error::Exception(error::Code::kInvalidManifest,
                           "triton mTLS requires an exact expected SAN");

  grpc::ChannelArguments args;
  args.SetInt(GRPC_ARG_MAX_RECEIVE_MESSAGE_LENGTH, opts.max_message_bytes);
  args.SetInt(GRPC_ARG_MAX_SEND_MESSAGE_LENGTH, opts.max_message_bytes);

  std::shared_ptr<grpc::ChannelCredentials> creds;
  if (tls) {
    grpc::SslCredentialsOptions ssl_opts;
    ssl_opts.pem_root_certs = opts.tls_ca;
    ssl_opts.pem_private_key = opts.tls_key;
    ssl_opts.pem_cert_chain = opts.tls_cert;
    creds = grpc::SslCredentials(ssl_opts);
    args.SetSslTargetNameOverride(opts.tls_target_name);
  } else {
    creds = grpc::InsecureChannelCredentials();
  }

  channel_ = grpc::CreateCustomChannel(opts.endpoint, creds, args);
  if (!channel_)
    throw error::Exception(error::Code::kPoolUnavailable, "triton channel create failed");
  stub_ = ::inference::GRPCInferenceService::NewStub(channel_);
  if (!stub_)
    throw error::Exception(error::Code::kPoolUnavailable, "triton stub create failed");
  available_ = true;
}

bool TritonClient::is_server_ready(int32_t deadline_ms) {
  if (!available_) return false;
  grpc::ClientContext ctx;
  set_deadline(ctx, deadline_ms, opts_.deadline_ms);
  ::inference::ServerReadyRequest req;
  ::inference::ServerReadyResponse resp;
  auto st = stub_->ServerReady(&ctx, req, &resp);
  if (!st.ok()) return false;
  return resp.ready();
}

bool TritonClient::is_model_ready(const std::string& name,
                                  const std::string& version,
                                  int32_t deadline_ms) {
  if (!available_) return false;
  grpc::ClientContext ctx;
  set_deadline(ctx, deadline_ms, opts_.deadline_ms);
  ::inference::ModelReadyRequest req;
  req.set_name(name);
  req.set_version(version);
  ::inference::ModelReadyResponse resp;
  auto st = stub_->ModelReady(&ctx, req, &resp);
  if (!st.ok()) return false;
  return resp.ready();
}

TritonServerMetadata TritonClient::server_metadata(int32_t deadline_ms) {
  TritonServerMetadata m;
  if (!available_)
    throw error::Exception(error::Code::kPoolUnavailable, "triton client not connected");
  grpc::ClientContext ctx;
  set_deadline(ctx, deadline_ms, opts_.deadline_ms);
  ::inference::ServerMetadataRequest req;
  ::inference::ServerMetadataResponse resp;
  auto st = stub_->ServerMetadata(&ctx, req, &resp);
  if (!st.ok())
    throw error::Exception(error::Code::kPoolUnavailable,
                           "triton ServerMetadata failed: " + st.error_message());
  m.name = resp.name();
  m.version = resp.version();
  for (const auto& e : resp.extensions()) m.extensions.push_back(e);
  std::sort(m.extensions.begin(), m.extensions.end());
  return m;
}

TritonModelMetadata TritonClient::model_metadata(const std::string& name,
                                                 const std::string& version,
                                                 int32_t deadline_ms) {
  TritonModelMetadata m;
  if (!available_)
    throw error::Exception(error::Code::kPoolUnavailable, "triton client not connected");
  grpc::ClientContext ctx;
  set_deadline(ctx, deadline_ms, opts_.deadline_ms);
  ::inference::ModelMetadataRequest req;
  req.set_name(name);
  req.set_version(version);
  ::inference::ModelMetadataResponse resp;
  auto st = stub_->ModelMetadata(&ctx, req, &resp);
  if (!st.ok())
    throw error::Exception(error::Code::kPoolUnavailable,
                           "triton ModelMetadata failed: " + st.error_message());
  m.name = resp.name();
  if (resp.versions_size() > 0) m.version = resp.versions(0);
  for (const auto& in : resp.inputs()) {
    m.inputs.push_back(in.name());
    if (m.input_datatype.empty()) {
      m.input_datatype = in.datatype();
      for (auto d : in.shape()) m.input_shape.push_back(d);
    }
  }
  for (const auto& out : resp.outputs()) {
    m.outputs.push_back(out.name());
    if (m.output_datatype.empty()) {
      m.output_datatype = out.datatype();
      for (auto d : out.shape()) m.output_shape.push_back(d);
    }
  }
  // Stable canonical projection instead of the raw serialized protobuf, so the
  // evidence digest does not drift with protobuf field ordering or unrelated
  // upstream field additions.
  std::ostringstream oss;
  oss << "name=" << m.name << '\n'
      << "version=" << m.version << '\n'
      << "platform=" << resp.platform() << '\n';
  for (size_t i = 0; i < m.inputs.size(); ++i) oss << "input=" << m.inputs[i] << '\n';
  oss << "input_datatype=" << m.input_datatype << '\n'
      << "input_shape=" << join_i64(m.input_shape) << '\n';
  for (size_t i = 0; i < m.outputs.size(); ++i) oss << "output=" << m.outputs[i] << '\n';
  oss << "output_datatype=" << m.output_datatype << '\n'
      << "output_shape=" << join_i64(m.output_shape) << '\n';
  m.raw_metadata = oss.str();
  return m;
}

TritonModelConfigProjection TritonClient::model_config(const std::string& name,
                                                       const std::string& version,
                                                       int32_t deadline_ms) {
  TritonModelConfigProjection c;
  if (!available_)
    throw error::Exception(error::Code::kPoolUnavailable, "triton client not connected");
  grpc::ClientContext ctx;
  set_deadline(ctx, deadline_ms, opts_.deadline_ms);
  ::inference::ModelConfigRequest req;
  req.set_name(name);
  req.set_version(version);
  ::inference::ModelConfigResponse resp;
  auto st = stub_->ModelConfig(&ctx, req, &resp);
  if (!st.ok())
    throw error::Exception(error::Code::kPoolUnavailable,
                           "triton ModelConfig failed: " + st.error_message());

  const auto& cfg = resp.config();
  c.name = cfg.name();
  c.platform = cfg.platform();
  c.backend = cfg.backend();
  c.max_batch_size = cfg.max_batch_size();
  if (cfg.has_dynamic_batching()) {
    c.dynamic_batching = true;
    for (auto p : cfg.dynamic_batching().preferred_batch_size())
      c.preferred_batch_size.push_back(p);
    c.max_queue_delay_microseconds =
        static_cast<int64_t>(cfg.dynamic_batching().max_queue_delay_microseconds());
    if (cfg.dynamic_batching().has_default_queue_policy())
      c.max_queue_size = cfg.dynamic_batching().default_queue_policy().max_queue_size();
  }
  for (const auto& ig : cfg.instance_group()) {
    c.instance_groups.emplace_back(::inference::ModelInstanceGroup_Kind_Name(ig.kind()),
                                   ig.count());
  }
  if (cfg.input_size() > 0) {
    c.input_name = cfg.input(0).name();
    c.input_datatype = ::inference::DataType_Name(cfg.input(0).data_type());
    for (auto d : cfg.input(0).dims()) c.input_dims.push_back(d);
  }
  if (cfg.output_size() > 0) {
    c.output_name = cfg.output(0).name();
    c.output_datatype = ::inference::DataType_Name(cfg.output(0).data_type());
    for (auto d : cfg.output(0).dims()) c.output_dims.push_back(d);
  }

  std::ostringstream oss;
  oss << "name=" << c.name << '\n'
      << "platform=" << c.platform << '\n'
      << "backend=" << c.backend << '\n'
      << "max_batch_size=" << c.max_batch_size << '\n'
      << "dynamic_batching=" << (c.dynamic_batching ? "1" : "0") << '\n'
      << "preferred_batch_size=" << join_i32(c.preferred_batch_size) << '\n'
      << "max_queue_delay_microseconds=" << c.max_queue_delay_microseconds << '\n'
      << "max_queue_size=" << c.max_queue_size << '\n';
  for (const auto& ig : c.instance_groups)
    oss << "instance_group=" << ig.first << ':' << ig.second << '\n';
  oss << "input=" << c.input_name << ':' << c.input_datatype << ':' << join_i64(c.input_dims) << '\n'
      << "output=" << c.output_name << ':' << c.output_datatype << ':' << join_i64(c.output_dims) << '\n';
  c.canonical_projection = oss.str();
  return c;
}

TritonModelStatistics TritonClient::model_statistics(const std::string& name,
                                                     const std::string& version,
                                                     int32_t deadline_ms) {
  TritonModelStatistics s;
  if (!available_)
    throw error::Exception(error::Code::kPoolUnavailable, "triton client not connected");
  grpc::ClientContext ctx;
  set_deadline(ctx, deadline_ms, opts_.deadline_ms);
  ::inference::ModelStatisticsRequest req;
  req.set_name(name);
  req.set_version(version);
  ::inference::ModelStatisticsResponse resp;
  auto st = stub_->ModelStatistics(&ctx, req, &resp);
  if (!st.ok())
    throw error::Exception(error::Code::kPoolUnavailable,
                           "triton ModelStatistics failed: " + st.error_message());
  for (const auto& ms : resp.model_stats()) {
    if (ms.name() != name) continue;
    s.observed = true;
    s.inference_count = ms.inference_count();
    s.execution_count = ms.execution_count();
    s.success_count = ms.inference_stats().success().count();
    break;
  }
  return s;
}

TritonInferResult TritonClient::model_infer(const std::string& name,
                                            const std::string& version,
                                            const std::string& input_name,
                                            const std::string& input_datatype,
                                            const std::vector<uint8_t>& input_bytes,
                                            const std::vector<int64_t>& input_shape,
                                            int32_t deadline_ms,
                                            const grpc::ServerContext* parent) {
  TritonInferResult result;
  if (!available_)
    throw error::Exception(error::Code::kPoolUnavailable, "triton client not connected");

  // Propagate the Edge request deadline and cancellation when the call runs
  // inside a server handler; otherwise use the supplied remaining budget.
  std::unique_ptr<grpc::ClientContext> ctx;
  if (parent != nullptr) {
    ctx = grpc::ClientContext::FromServerContext(*parent, grpc::PropagationOptions());
  } else {
    ctx = std::make_unique<grpc::ClientContext>();
  }
  if (!ctx)
    throw error::Exception(error::Code::kPoolUnavailable, "triton client context create failed");
  set_deadline(*ctx, deadline_ms, opts_.deadline_ms);

  ::inference::ModelInferRequest req;
  req.set_model_name(name);
  req.set_model_version(version);

  auto* inp = req.add_inputs();
  inp->set_name(input_name);
  inp->set_datatype(input_datatype);
  for (auto d : input_shape) inp->add_shape(d);

  req.add_raw_input_contents(
      std::string(reinterpret_cast<const char*>(input_bytes.data()), input_bytes.size()));

  ::inference::ModelInferResponse resp;
  auto st = stub_->ModelInfer(ctx.get(), req, &resp);
  if (!st.ok()) {
    if (st.error_code() == grpc::StatusCode::DEADLINE_EXCEEDED)
      throw error::Exception(error::Code::kDeadlineExceeded,
                             "triton ModelInfer deadline exceeded");
    if (st.error_code() == grpc::StatusCode::CANCELLED)
      throw error::Exception(error::Code::kAborted, "triton ModelInfer cancelled");
    throw error::Exception(error::Code::kPoolUnavailable,
                           "triton ModelInfer failed: " + st.error_message());
  }

  if (resp.outputs_size() == 0)
    throw error::Exception(error::Code::kIncompatibleContract, "triton returned no output tensor");
  const auto& out = resp.outputs(0);
  result.datatype = out.datatype();
  for (auto d : out.shape()) result.shape.push_back(d);
  if (result.datatype != "FP32")
    throw error::Exception(error::Code::kIncompatibleContract,
                           "triton output datatype not FP32: " + result.datatype);

  if (resp.raw_output_contents_size() > 0) {
    const auto& raw = resp.raw_output_contents(0);
    if (raw.size() % sizeof(float) != 0)
      throw error::Exception(error::Code::kBufferOverflow,
                             "triton raw output length not a multiple of float32");
    const size_t count = raw.size() / sizeof(float);
    result.values.resize(count);
    std::memcpy(result.values.data(), raw.data(), raw.size());
  } else if (out.contents().fp32_contents_size() > 0) {
    result.values.assign(out.contents().fp32_contents().begin(),
                         out.contents().fp32_contents().end());
  } else {
    throw error::Exception(error::Code::kIncompatibleContract, "triton output carries no data");
  }
  return result;
}

}  // namespace masi::inf
