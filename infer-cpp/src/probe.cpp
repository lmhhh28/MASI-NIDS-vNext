// mTLS readiness probe binary for OCI smoke tests.
//
// Creates an mTLS channel to the Gateway, calls GetBinding, verifies the
// readback identity matches the expected startup envelope digest, and exits
// 0 on success / 1 on failure.

#include <grpcpp/grpcpp.h>

#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "edge.grpc.pb.h"
#include "edge.pb.h"
#include "inference.grpc.pb.h"

namespace {

struct ProbeArgs {
  std::string endpoint;
  std::string ca_path;
  std::string cert_path;
  std::string key_path;
  std::string logical_pool_id;
  uint64_t pool_generation = 0;
  uint64_t binding_generation = 0;
  std::string model_control_incarnation_id;
  std::string expected_startup_envelope_digest;
};

ProbeArgs parse_args(int argc, char** argv) {
  ProbeArgs a;
  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    auto eq = arg.find('=');
    if (eq == std::string::npos) continue;
    std::string k = arg.substr(0, eq);
    std::string v = arg.substr(eq + 1);
    if (k == "--endpoint") a.endpoint = v;
    else if (k == "--tls-ca") a.ca_path = v;
    else if (k == "--tls-cert") a.cert_path = v;
    else if (k == "--tls-key") a.key_path = v;
    else if (k == "--logical-pool-id") a.logical_pool_id = v;
    else if (k == "--pool-generation") a.pool_generation = std::stoull(v);
    else if (k == "--binding-generation") a.binding_generation = std::stoull(v);
    else if (k == "--model-control-incarnation-id") a.model_control_incarnation_id = v;
    else if (k == "--expected-startup-envelope-digest") a.expected_startup_envelope_digest = v;
  }
  return a;
}

}  // namespace

int main(int argc, char** argv) {
  auto args = parse_args(argc, argv);
  if (args.endpoint.empty() || args.ca_path.empty() || args.cert_path.empty() ||
      args.key_path.empty()) {
    std::cerr << "probe: missing required mTLS args\n";
    return 1;
  }

  grpc::SslCredentialsOptions opts;
  {
    std::ifstream caf(args.ca_path);
    std::stringstream ss; ss << caf.rdbuf();
    opts.pem_root_certs = ss.str();
  }
  {
    std::ifstream cef(args.cert_path);
    std::stringstream ss; ss << cef.rdbuf();
    opts.pem_cert_chain = ss.str();
  }
  {
    std::ifstream kf(args.key_path);
    std::stringstream ss; ss << kf.rdbuf();
    opts.pem_private_key = ss.str();
  }
  if (opts.pem_root_certs.empty() || opts.pem_cert_chain.empty() ||
      opts.pem_private_key.empty()) {
    std::cerr << "probe: empty PEM\n";
    return 1;
  }

  auto channel = grpc::SslCredentials(opts);
  auto stub = masi::inference::v1::CentralInference::NewStub(
      grpc::CreateChannel(args.endpoint, channel));
  if (!stub) {
    std::cerr << "probe: stub creation failed\n";
    return 1;
  }

  grpc::ClientContext ctx;
  ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(2000));
  masi::edge::v1::GetBindingRequest req;
  req.set_logical_pool_id(args.logical_pool_id);
  req.set_pool_generation(args.pool_generation);
  req.set_binding_generation(args.binding_generation);
  req.set_model_control_incarnation_id(args.model_control_incarnation_id);
  req.set_schema_version("inference-committed-binding/v1");

  masi::edge::v1::BindingReadback resp;
  auto status = stub->GetBinding(&ctx, req, &resp);
  if (!status.ok()) {
    std::cerr << "probe: GetBinding failed: " << status.error_message() << "\n";
    return 1;
  }
  if (!args.expected_startup_envelope_digest.empty() &&
      resp.startup_envelope_digest() != args.expected_startup_envelope_digest) {
    std::cerr << "probe: startup envelope digest mismatch\n";
    return 1;
  }
  if (resp.logical_pool_id() != args.logical_pool_id ||
      resp.pool_generation() != args.pool_generation ||
      resp.binding_generation() != args.binding_generation) {
    std::cerr << "probe: readback identity mismatch\n";
    return 1;
  }
  std::cout << "probe: OK\n";
  return 0;
}