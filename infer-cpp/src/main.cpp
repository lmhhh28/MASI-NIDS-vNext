// Central Inference Gateway process entry.
//
// Sequence: parse config path -> load config -> connect the Triton channel ->
// run startup (fail closed) -> create the mTLS gRPC server -> register
// CentralInference -> install SIGTERM/SIGINT handlers (drain -> shutdown ->
// exit 0) -> wait.
//
// The Gateway holds no execution engine of its own: Triton is the only executor
// and the only delayed batcher. There is no local inference path, so a missing
// or unhealthy Triton is a startup failure rather than a silent degradation.

#include <csignal>
#include <cstdlib>

#include <grpcpp/grpcpp.h>

#include <atomic>
#include <chrono>
#include <iostream>
#include <memory>
#include <thread>

#include "admission.h"
#include "config.h"
#include "gateway.h"
#include "health.h"
#include "numeric.h"
#include "readback.h"
#include "startup.h"
#include "tls.h"
#include "triton_client.h"

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

  // Triton channel. Plaintext is only accepted for a loopback endpoint; the
  // triton_tls_* material is required for anything else.
  auto triton = std::make_shared<TritonClient>();
  TritonClient::ConnectOptions to;
  to.endpoint = cfg.triton_endpoint;
  to.deadline_ms = cfg.request_deadline_ms;
  try {
    if (!cfg.triton_tls_ca_path.empty()) {
      to.tls_ca = read_pem_file(cfg.triton_tls_ca_path, false);
      to.tls_cert = read_pem_file(cfg.triton_tls_cert_path, false);
      to.tls_key = read_pem_file(cfg.triton_tls_key_path, true);
      to.tls_target_name = cfg.triton_tls_server_name;
    }
    triton->connect(to);
  } catch (const std::exception& e) {
    // No local execution plane exists, so this is fatal by construction.
    std::cerr << "triton connect failed: " << e.what() << "\n";
    return 1;
  }

  StartupResult startup;
  try {
    startup = run_startup(cfg, *triton, now_ms());
  } catch (const std::exception& e) {
    std::cerr << "startup failed: " << e.what() << "\n";
    return 1;
  }
  if (!startup.ready) {
    std::cerr << "startup not ready\n";
    return 1;
  }

  auto svc_impl = std::make_shared<CentralInferenceServiceImpl>(cfg, startup, triton);
  HealthMonitor health(svc_impl, triton, startup);

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
  health.mark_ready();
  std::cerr << "CentralInference listening on " << cfg.gateway_listen << " (mTLS)\n";

  std::signal(SIGTERM, handle_signal);
  std::signal(SIGINT, handle_signal);

  // Wait for signal. Use a poll loop so we don't miss the signal.
  while (!g_signal_seen.load(std::memory_order_acquire)) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }

  // Drain: stop new admission, then wait for admitted work up to the
  // configured bound. The bound comes from config; it is never silently capped.
  std::cerr << "drain begin (drain_ms=" << cfg.drain_ms << ")\n";
  health.begin_drain(cfg.drain_ms);
  const int64_t drain_deadline = now_ms() + cfg.drain_ms;
  while (svc_impl->in_flight() > 0 && now_ms() < drain_deadline) {
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  const uint64_t unfinished = svc_impl->in_flight();
  if (unfinished > 0) {
    health.report_incomplete_attempt();
    std::cerr << "drain incomplete: in_flight=" << unfinished << "\n";
  }

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
