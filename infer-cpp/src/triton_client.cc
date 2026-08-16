#include "triton_client.h"

#include <grpcpp/grpcpp.h>

#include <chrono>
#include <cstring>
#include <nlohmann/json.hpp>
#include <sstream>

namespace masi::inf {

void TritonClient::connect(const ConnectOptions& opts) {
  opts_ = opts;
  if (opts.endpoint.empty())
    throw error::Exception(error::Code::kInvalidManifest, "triton endpoint empty");

  grpc::ChannelArguments args;
  args.SetInt(GRPC_ARG_MAX_RECEIVE_MESSAGE_LENGTH, opts.max_message_bytes);
  args.SetInt(GRPC_ARG_MAX_SEND_MESSAGE_LENGTH, opts.max_message_bytes);

  std::shared_ptr<grpc::ChannelCredentials> creds;
  if (!opts.tls_ca.empty()) {
    grpc::SslCredentialsOptions ssl_opts;
    ssl_opts.pem_root_certs = opts.tls_ca;
    ssl_opts.pem_private_key = opts.tls_key;
    ssl_opts.pem_cert_chain = opts.tls_cert;
    creds = grpc::SslCredentials(ssl_opts);
  } else {
    creds = grpc::InsecureChannelCredentials();
  }

  channel_ = grpc::CreateCustomChannel(opts.endpoint, creds, args);
  if (!channel_)
    throw error::Exception(error::Code::kPoolUnavailable, "triton channel create failed");
  stub_ = inference::GRPCInferenceService::NewStub(channel_);
  if (!stub_)
    throw error::Exception(error::Code::kPoolUnavailable, "triton stub create failed");
  available_ = true;
}

static void set_deadline(grpc::ClientContext& ctx, int32_t deadline_ms) {
  auto deadline = std::chrono::system_clock::now() +
                  std::chrono::milliseconds(deadline_ms > 0 ? deadline_ms : 2000);
  ctx.set_deadline(deadline);
}

bool TritonClient::is_server_ready() {
  if (!available_) return false;
  grpc::ClientContext ctx;
  set_deadline(ctx, opts_.deadline_ms);
  inference::ServerReadyRequest req;
  inference::ServerReadyResponse resp;
  auto st = stub_->ServerReady(&ctx, req, &resp);
  if (!st.ok()) return false;
  return resp.ready();
}

bool TritonClient::is_model_ready(const std::string& name,
                                  const std::string& version) {
  if (!available_) return false;
  grpc::ClientContext ctx;
  set_deadline(ctx, opts_.deadline_ms);
  inference::ModelReadyRequest req;
  req.set_name(name);
  req.set_version(version);
  inference::ModelReadyResponse resp;
  auto st = stub_->ModelReady(&ctx, req, &resp);
  if (!st.ok()) return false;
  return resp.ready();
}

TritonModelMetadata TritonClient::model_metadata(const std::string& name,
                                                 const std::string& version) {
  TritonModelMetadata m;
  if (!available_) return m;
  grpc::ClientContext ctx;
  set_deadline(ctx, opts_.deadline_ms);
  inference::ModelMetadataRequest req;
  req.set_name(name);
  req.set_version(version);
  inference::ModelMetadataResponse resp;
  auto st = stub_->ModelMetadata(&ctx, req, &resp);
  if (!st.ok()) return m;
  m.name = resp.name();
  if (resp.versions_size() > 0) m.version = resp.versions(0);
  for (const auto& in : resp.inputs()) m.inputs.push_back(in.name());
  for (const auto& out : resp.outputs()) m.outputs.push_back(out.name());
  return m;
}

TritonModelConfig TritonClient::model_config(const std::string& name,
                                             const std::string& version) {
  TritonModelConfig c;
  if (!available_) return c;
  c.model_control_mode = "none";
  return c;
}

std::vector<std::string> TritonClient::model_repository_index() {
  std::vector<std::string> idx;
  if (!available_) return idx;
  return idx;
}

std::vector<float> TritonClient::model_infer(
    const std::string& name, const std::string& version,
    const std::vector<uint8_t>& input_bytes,
    const std::vector<int64_t>& input_shape, size_t out_count) {
  std::vector<float> result;
  if (!available_) return result;

  grpc::ClientContext ctx;
  set_deadline(ctx, opts_.deadline_ms);
  inference::ModelInferRequest req;
  req.set_model_name(name);
  req.set_model_version(version);

  auto* inp = req.add_inputs();
  inp->set_name("features");
  inp->set_datatype("UINT64");
  for (auto d : input_shape) inp->add_shape(d);

  req.add_raw_input_contents(
      std::string(reinterpret_cast<const char*>(input_bytes.data()),
                  input_bytes.size()));

  inference::ModelInferResponse resp;
  auto st = stub_->ModelInfer(&ctx, req, &resp);
  if (!st.ok())
    throw error::Exception(error::Code::kPoolUnavailable,
                           "triton ModelInfer failed: " + st.error_message());

  if (resp.raw_output_contents_size() > 0) {
    const auto& raw = resp.raw_output_contents(0);
    const float* fptr = reinterpret_cast<const float*>(raw.data());
    size_t count = raw.size() / sizeof(float);
    result.assign(fptr, fptr + count);
  } else if (resp.outputs_size() > 0 && resp.outputs(0).contents().fp32_contents_size() > 0) {
    const auto& fp = resp.outputs(0).contents().fp32_contents();
    result.assign(fp.begin(), fp.end());
  }

  return result;
}

}  // namespace masi::inf