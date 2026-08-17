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

#include <grpc/grpc.h>
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

// Hold one process-level gRPC initialization reference until every application
// owned Server, Service and Channel has been destroyed. The last ordinary
// grpc_shutdown() is allowed to delegate cleanup to an unjoined EventEngine
// thread; releasing the final reference synchronously on the main thread avoids
// racing process-exit/OpenSSL cleanup with that worker.
class GrpcRuntimeGuard {
public:
  GrpcRuntimeGuard() { grpc_init(); }
  ~GrpcRuntimeGuard() { grpc_shutdown_blocking(); }

  GrpcRuntimeGuard(const GrpcRuntimeGuard &) = delete;
  GrpcRuntimeGuard &operator=(const GrpcRuntimeGuard &) = delete;
};

void handle_signal(int sig) {
  (void)sig;
  g_signal_seen.store(true, std::memory_order_release);
}

int64_t now_ms() {
  using namespace std::chrono;
  return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

int run(int argc, char **argv) {
  GrpcRuntimeGuard grpc_runtime;
  if (argc < 2) {
    std::cerr << "usage: " << argv[0] << " <config.json>\n";
    return 2;
  }
  const std::string config_path = argv[1];
  Config cfg;
  try {
    cfg = load_config(config_path);
  } catch (const std::exception &e) {
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
  } catch (const std::exception &e) {
    // No local execution plane exists, so this is fatal by construction.
    std::cerr << "triton connect failed: " << e.what() << "\n";
    return 1;
  }

  StartupResult startup;
  try {
    startup = run_startup(cfg, *triton, now_ms());
  } catch (const std::exception &e) {
    std::cerr << "startup failed: " << e.what() << "\n";
    return 1;
  }
  if (!startup.ready) {
    std::cerr << "startup not ready\n";
    return 1;
  }

  auto svc_impl = std::make_shared<CentralInferenceServiceImpl>(cfg, startup, triton);
  auto health = std::make_unique<HealthMonitor>(svc_impl, triton, startup);

  std::unique_ptr<grpc::Server> server;
  {
    grpc::ServerBuilder builder;
    auto server_creds =
        make_server_credentials(cfg.tls_ca_path, cfg.tls_cert_path, cfg.tls_key_path);
    builder.AddListeningPort(cfg.gateway_listen, server_creds);
    builder.RegisterService(svc_impl.get());
    builder.SetMaxReceiveMessageSize(cfg.max_request_bytes);
    builder.SetMaxSendMessageSize(cfg.max_response_bytes);
    server = builder.BuildAndStart();
  }
  if (!server) {
    std::cerr << "grpc server build failed\n";
    return 1;
  }
  health->mark_ready();
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
  health->begin_drain(cfg.drain_ms);
  const int64_t drain_deadline = now_ms() + cfg.drain_ms;
  while (svc_impl->in_flight() > 0 && now_ms() < drain_deadline) {
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  const uint64_t unfinished = svc_impl->in_flight();
  if (unfinished > 0) {
    health->report_incomplete_attempt();
    std::cerr << "drain incomplete: in_flight=" << unfinished << "\n";
  }

  std::cerr << "shutdown begin\n";
  health->begin_shutdown();
  server->Shutdown();
  server->Wait();
  // Tear the dependency graph down explicitly while all ownership edges are
  // still visible. This avoids racing process-exit static cleanup with gRPC's
  // event-engine threads and makes a shutdown allocator failure observable at
  // a precise boundary.
  server.reset();
  health.reset();
  svc_impl.reset();
  triton.reset();
  std::cerr << "shutdown complete\n";
  std::cerr.flush();
  // The pinned static gRPC/OpenSSL stack has an intermittent process-global
  // teardown race after failed TLS handshakes: after every application-owned
  // object above is synchronously drained and destroyed, its final global
  // cleanup can still double-free or SIGSEGV an EventEngine worker. This
  // stateless process has no remaining data to commit. Exit without running
  // third-party/static destructors only after the complete graceful boundary
  // above; early startup/runtime failures continue to return normally.
  std::_Exit(EXIT_SUCCESS);
}

} // namespace

} // namespace masi::inf

int main(int argc, char **argv) { return masi::inf::run(argc, argv); }
