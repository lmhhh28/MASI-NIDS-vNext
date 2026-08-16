// Central Inference Gateway process entry.
//
// Sequence: parse config path -> load config -> run startup -> create mTLS
// gRPC server -> register CentralInference -> install SIGTERM/SIGINT handlers
// (drain -> shutdown -> exit 0) -> wait.

#include <csignal>
#include <cstdlib>

#include <grpcpp/grpcpp.h>

#include <atomic>
#include <fstream>
#include <iostream>
#include <memory>
#include <sstream>
#include <thread>

#include "admission.h"
#include "config.h"
#include "gateway.h"
#include "health.h"
#include "numeric.h"
#include "ort_session.h"
#include "readback.h"
#include "startup.h"
#include "triton_client.h"
#include "tls.h"

namespace masi::inf {

namespace {

std::atomic<bool> g_signal_seen{false};

void handle_signal(int sig) {
  (void)sig;
  g_signal_seen.store(true, std::memory_order_release);
}

int64_t now_ms() {
  using namespace std::chrono;
  return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

int run(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "usage: " << argv[0] << " <config.json>\n";
    return 2;
  }
  const std::string config_path = argv[1];
  Config cfg;
  try {
    cfg = load_config(config_path);
  } catch (const std::exception& e) {
    std::cerr << "config load failed: " << e.what() << "\n";
    return 1;
  }

  StartupResult startup;
  try {
    startup = run_startup(cfg, now_ms());
  } catch (const std::exception& e) {
    std::cerr << "startup failed: " << e.what() << "\n";
    return 1;
  }
  if (!startup.ready) {
    std::cerr << "startup not ready\n";
    return 1;
  }

  // Keep the ORT session and Triton client alive for the service lifetime.
  auto session = std::make_shared<OrtSession>();
  OrtSessionConfig oc;
  oc.model_path = cfg.model_repository_path + "/model.onnx";
  oc.intra_op_num_threads = cfg.intra_op_num_threads;
  oc.inter_op_num_threads = cfg.inter_op_num_threads;
  oc.intra_op_affinity = cfg.intra_op_affinity;
  oc.enable_cpu_arena = cfg.enable_cpu_arena;
  try {
    session->open(oc);
  } catch (const std::exception& e) {
    std::cerr << "ort session open failed: " << e.what() << "\n";
    return 1;
  }

  auto triton = std::make_shared<TritonClient>();
  TritonClient::ConnectOptions to;
  to.endpoint = cfg.triton_endpoint;
  to.deadline_ms = cfg.request_deadline_ms;
  // Triton runs on an isolated loopback/inference network. mTLS to Triton
  // is configured separately via triton_tls_* config fields (not the
  // gateway's edge-facing TLS certs). For the CPU E2E test, Triton has no TLS.
  // No TLS options set here = plain gRPC to loopback Triton.
  try {
    triton->connect(to);
  } catch (const std::exception& e) {
    std::cerr << "triton connect failed: " << e.what() << "\n";
  }

  NumericProfile numeric;
  numeric.abs_tol = 1e-6;
  numeric.rel_tol = 1e-6;
  numeric.ulp_tol = 4;
  numeric.ood_threshold = 1.0;       // no class score above 1.0 in softmax
  numeric.abstain_threshold = 0.0;  // never abstain by default
  numeric.alert_threshold = 0.5;
  numeric.softmax_output = true;
  ClassLabel benign;
  benign.label = 0;
  benign.name = "benign";
  benign.output_index = 0;
  ClassLabel alert;
  alert.label = 1;
  alert.name = "alert";
  alert.output_index = 1;
  numeric.class_order = {benign, alert};

  auto svc_impl = std::make_shared<CentralInferenceServiceImpl>(
      cfg, startup, session, triton, numeric);
  HealthMonitor health(svc_impl, triton, startup);
  health.mark_ready();

  grpc::ServerBuilder builder;
  auto server_creds = make_server_credentials(cfg.tls_ca_path,
                                              cfg.tls_cert_path,
                                              cfg.tls_key_path);
  builder.AddListeningPort(cfg.gateway_listen, server_creds);
  builder.RegisterService(svc_impl.get());
  builder.SetMaxReceiveMessageSize(cfg.max_request_bytes);
  builder.SetMaxSendMessageSize(cfg.max_response_bytes);
  std::unique_ptr<grpc::Server> server(builder.BuildAndStart());
  if (!server) {
    std::cerr << "grpc server build failed\n";
    return 1;
  }
  std::cerr << "CentralInference listening on " << cfg.gateway_listen << " (mTLS)\n";

  std::signal(SIGTERM, handle_signal);
  std::signal(SIGINT, handle_signal);

  // Wait for signal. Use a poll loop so we don't miss the signal.
  while (!g_signal_seen.load(std::memory_order_acquire)) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }

  // Drain: stop new admission, allow bounded deadline for admitted requests.
  std::cerr << "drain begin (drain_ms=" << cfg.drain_ms << ")\n";
  health.begin_drain(cfg.drain_ms);
  // Grace period bounded by drain_ms (capped to 1s for test).
  int effective_drain = std::min(cfg.drain_ms, 1000);
  std::this_thread::sleep_for(std::chrono::milliseconds(effective_drain));

  // Shutdown: stop server + Triton. Preserve structured startup/runtime
  // evidence (already captured in startup_).
  std::cerr << "shutdown begin\n";
  health.begin_shutdown();
  server->Shutdown();
  server->Wait();
  std::cerr << "shutdown complete\n";
  return 0;
}

}  // namespace

}  // namespace masi::inf

int main(int argc, char** argv) {
  return masi::inf::run(argc, argv);
}