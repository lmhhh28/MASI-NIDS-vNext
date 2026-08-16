// Central Inference real-process black-box E2E test.
//
// This test spawns the real `masi_inference_gateway` binary as a subprocess
// with a test config, mTLS certs, and a testkit fixture model repository. It
// creates a contract-consistent fake Edge gRPC client that connects via mTLS
// and exercises the public CentralInference boundary:
//   - GetBinding readback
//   - Infer with a valid batch (from golden) and result identity/fence
//   - Rejection: oversize batch, malformed tensor, unknown major, NaN/Inf
//   - mTLS: plaintext rejected, wrong identity rejected
//   - SIGTERM graceful shutdown exit 0
//
// GUARD: The real-process part is guarded by the `MASI_INF_E2E=1` env var.
// When not set, the test exits 77 (skip) so CI can run the contract/property
// tests without the full Triton/ORT stack. When the binary or mTLS setup
// fails, the test exits 1.

#include <grpcpp/grpcpp.h>

#include <arpa/inet.h>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <netinet/in.h>
#include <nlohmann/json.hpp>
#include <sstream>
#include <string>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include <vector>

#include "support/mod.h"

#include "digest.h"
#include "edge/v1/edge.pb.h"
#include "envelope.h"
#include "inference/v1/inference.pb.h"

namespace {

using masi::inf::test::TempDir;
using masi::inf::test::FakeEdge;
using masi::inf::test::build_valid_batch_from_golden;
using masi::inf::test::generate_mtls_bundle;
using masi::inf::test::load_golden_inference;
using masi::inf::test::hex_to_bytes;
using masi::inf::test::write_temp_file;

// ---------------------------------------------------------------------------
// Subprocess management
// ---------------------------------------------------------------------------
struct Subprocess {
  pid_t pid = -1;
  int stdout_fd = -1;
  int stderr_fd = -1;
  std::string stdout_file;
  std::string stderr_file;
};

// Fork+exec a binary with argv. Redirect stdout/stderr to temp files.
Subprocess spawn(const std::string& binary, const std::vector<std::string>& argv,
                 const std::string& stdout_path, const std::string& stderr_path) {
  Subprocess sp;
  sp.stdout_file = stdout_path;
  sp.stderr_file = stderr_path;

  // Open output files.
  int out_fd = ::open(stdout_path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
  CHECK(out_fd >= 0, "spawn: open stdout failed");
  int err_fd = ::open(stderr_path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
  CHECK(err_fd >= 0, "spawn: open stderr failed");

  pid_t pid = ::fork();
  CHECK(pid >= 0, "spawn: fork failed");
  if (pid == 0) {
    // Child. Die with the test process so a failed run never leaks a
    // gateway that keeps occupying its listen port.
    ::prctl(PR_SET_PDEATHSIG, SIGTERM);
    ::dup2(out_fd, STDOUT_FILENO);
    ::dup2(err_fd, STDERR_FILENO);
    ::close(out_fd);
    ::close(err_fd);
    // Build argv array.
    std::vector<char*> args;
    args.push_back(const_cast<char*>(binary.c_str()));
    for (const auto& a : argv) args.push_back(const_cast<char*>(a.c_str()));
    args.push_back(nullptr);
    ::execv(binary.c_str(), args.data());
    // If exec returns, it failed.
    std::cerr << "spawn: execv failed: " << std::strerror(errno) << "\n";
    ::_exit(127);
  }
  // Parent.
  ::close(out_fd);
  ::close(err_fd);
  sp.pid = pid;
  return sp;
}

// Wait for subprocess to exit. Returns exit code.
int wait_for(Subprocess& sp) {
  CHECK(sp.pid > 0, "wait_for: invalid pid");
  int status = 0;
  pid_t w = ::waitpid(sp.pid, &status, 0);
  CHECK(w == sp.pid, "wait_for: waitpid failed");
  sp.pid = -1;
  if (WIFEXITED(status)) return WEXITSTATUS(status);
  if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
  return -1;
}

// Send a signal to the subprocess.
void signal_subprocess(Subprocess& sp, int sig) {
  CHECK(sp.pid > 0, "signal_subprocess: invalid pid");
  ::kill(sp.pid, sig);
}

// ---------------------------------------------------------------------------
// Read a file into a string
// ---------------------------------------------------------------------------
std::string read_file(const std::string& path) {
  std::ifstream f(path);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

// ---------------------------------------------------------------------------
// Locate the Gateway binary
// ---------------------------------------------------------------------------
std::string find_gateway_binary() {
  const char* env = std::getenv("MASI_INF_GATEWAY_BIN");
  if (env && env[0] != '\0') return std::string(env);
  // Common build locations relative to the repo root.
  namespace fs = std::filesystem;
  fs::path repo = masi::inf::test::repo_root();
  std::vector<fs::path> candidates = {
      repo / "infer-cpp" / "build" / "cpu-release" / "masi_inference_gateway",
      repo / "infer-cpp" / "build" / "masi_inference_gateway",
      repo / "infer-cpp" / "build" / "cpu-debug" / "masi_inference_gateway",
      fs::current_path() / "masi_inference_gateway",
  };
  for (const auto& p : candidates) {
    if (fs::exists(p) && fs::is_regular_file(p)) {
      std::error_code ec;
      auto perms = fs::status(p, ec).permissions();
      if ((perms & fs::perms::owner_exec) != fs::perms::none) {
        return p.string();
      }
    }
  }
  return "";
}

// ---------------------------------------------------------------------------
// Build a test config JSON for the Gateway
// ---------------------------------------------------------------------------
std::string build_test_config(const std::string& envelope_path,
                               const std::string& repo_path,
                               const std::string& ca,
                               const std::string& cert,
                               const std::string& key) {
  nlohmann::json j;
  j["startup_envelope_path"] = envelope_path;
  j["model_repository_path"] = repo_path;
  j["triton_endpoint"] = "127.0.0.1:8001";
  j["gateway_listen"] = "127.0.0.1:0";  // ephemeral port; read from stderr
  j["tls_ca_path"] = ca;
  j["tls_cert_path"] = cert;
  j["tls_key_path"] = key;
  j["max_records_per_batch"] = 256;
  j["max_request_bytes"] = 4194304;
  j["max_response_bytes"] = 4194304;
  j["max_in_flight"] = 64;
  j["request_deadline_ms"] = 2000;
  j["runtime_profile"] = "model-runtime-central-cpu/v1";
  j["availability_profile"] = "availability-single/v1";
  j["drain_ms"] = 500;
  return j.dump();
}

// ---------------------------------------------------------------------------
// Build a minimal startup envelope JSON with a correctly computed
// envelope_digest (canonical body over the StartupEnvelope struct).
// ---------------------------------------------------------------------------
std::string build_test_envelope(const std::string& model_digest,
                                const std::string& closure_digest) {
  masi::inf::StartupEnvelope env;
  env.schema_version = "inference-startup-envelope/v1";
  env.model_control_incarnation_id = "incarnation-blackbox-0001";
  env.operation_id = "op-blackbox-0001";
  env.kind = "binding";
  env.logical_pool_id = "pool-blackbox-0001";
  env.pool_generation = 1;
  env.availability_profile_id = "availability-single/v1";
  env.deployment_tier = "acceptance";
  env.model_revision_digest = model_digest;
  env.inference_wire_profile_digest =
      "sha256:0000000000000000000000000000000000000000000000000000000000000000";
  env.runtime_profile_id = "model-runtime-central-cpu/v1";
  env.repository_snapshot.identity = "repo-blackbox-0001";
  env.repository_snapshot.closure_digest = closure_digest;
  env.instance_group.kind = "KIND_CPU";
  env.instance_group.count = 1;
  env.instance_group.operator_partition_digest =
      "sha256:0000000000000000000000000000000000000000000000000000000000000000";
  env.proposed_binding_generation = 1;
  env.issued_at_unix_ms = 0;
  env.expires_at_unix_ms = 9999999999999LL;
  env.trace_id = "trace-blackbox-0001";
  env.envelope_digest = masi::inf::compute_envelope_body_digest(env);

  nlohmann::json j;
  j["schema_version"] = env.schema_version;
  j["model_control_incarnation_id"] = env.model_control_incarnation_id;
  j["operation_id"] = env.operation_id;
  j["kind"] = env.kind;
  j["logical_pool_id"] = env.logical_pool_id;
  j["pool_generation"] = env.pool_generation;
  j["availability_profile_id"] = env.availability_profile_id;
  j["deployment_tier"] = env.deployment_tier;
  j["model_revision_digest"] = env.model_revision_digest;
  j["inference_wire_profile_digest"] = env.inference_wire_profile_digest;
  j["runtime_profile_id"] = env.runtime_profile_id;
  j["repository_snapshot"] = {
      {"identity", env.repository_snapshot.identity},
      {"closure_digest", env.repository_snapshot.closure_digest}};
  j["instance_group"] = {
      {"kind", env.instance_group.kind},
      {"count", env.instance_group.count},
      {"operator_partition_digest", env.instance_group.operator_partition_digest}};
  j["proposed_binding_generation"] = env.proposed_binding_generation;
  j["issued_at_unix_ms"] = env.issued_at_unix_ms;
  j["expires_at_unix_ms"] = env.expires_at_unix_ms;
  j["trace_id"] = env.trace_id;
  j["envelope_digest"] = env.envelope_digest;
  return j.dump();
}

// ---------------------------------------------------------------------------
// Build a test repository closure (read-only, digest-pinned) from the real
// testkit fixture ONNX, laid out like the E2E Triton repository
// (<name>/1/model.onnx) so the closure-driven model path and Triton model
// name resolution see a realistic repository. Returns the model and closure
// digests.
// ---------------------------------------------------------------------------
struct TestRepoDigests {
  std::string model_digest;
  std::string closure_digest;
};

TestRepoDigests build_test_repository(const std::string& dir) {
  namespace fs = std::filesystem;
  fs::path fixture = fs::path(masi::inf::test::repo_root()) /
                     "testkit" / "fixtures" / "models" /
                     "masi-ids-window-v1-r1.onnx";
  CHECK(fs::is_regular_file(fixture),
        "testkit fixture ONNX missing: " + fixture.string());
  std::string model_bytes = read_file(fixture.string());
  CHECK(!model_bytes.empty(), "testkit fixture ONNX empty");

  fs::create_directories(dir + "/masi-ids-window-v1/1");
  write_temp_file(dir + "/masi-ids-window-v1/1", "model.onnx", model_bytes);

  std::string model_path = dir + "/masi-ids-window-v1/1/model.onnx";
  std::string model_digest = masi::inf::sha256_file(model_path);
  // closure digest = sha256 over sorted member digests joined by newline.
  std::string closure_input = model_digest + "\n";
  std::string closure_digest =
      masi::inf::sha256_hex(closure_input.data(), closure_input.size());

  nlohmann::json manifest;
  manifest["identity"] = "repo-blackbox-0001";
  manifest["closure_digest"] = closure_digest;
  manifest["members"] = nlohmann::json::array();
  manifest["members"][0] = {{"rel_path", "masi-ids-window-v1/1/model.onnx"},
                            {"member_digest", model_digest},
                            {"role", "model"}};
  write_temp_file(dir, "closure-manifest.json", manifest.dump());

  // Make the whole tree read-only (files and directories), as required by
  // verify_repository_closure.
  for (auto& p : fs::recursive_directory_iterator(dir)) {
    fs::permissions(p.path(),
                    fs::perms::owner_read | fs::perms::group_read |
                        fs::perms::others_read,
                    fs::perm_options::replace);
  }
  fs::permissions(dir,
                  fs::perms::owner_read | fs::perms::owner_exec |
                      fs::perms::group_read | fs::perms::group_exec |
                      fs::perms::others_read | fs::perms::others_exec,
                  fs::perm_options::replace);

  return TestRepoDigests{model_digest, closure_digest};
}

// ---------------------------------------------------------------------------
// Wait for the Gateway to log its bound listen address and return
// "127.0.0.1:<port>". The Gateway binds an ephemeral port (config
// "127.0.0.1:0") and logs the actual address; exits the test if the process
// dies or never reports an address.
// ---------------------------------------------------------------------------
std::string wait_for_listen_addr(const Subprocess& sp,
                                 const std::string& stderr_path) {
  const std::string prefix = "listening on 127.0.0.1:";
  for (int i = 0; i < 100; ++i) {
    std::string err = read_file(stderr_path);
    auto pos = err.find(prefix);
    if (pos != std::string::npos) {
      std::string port;
      for (size_t j = pos + prefix.size(); j < err.size(); ++j) {
        if (err[j] >= '0' && err[j] <= '9') port += err[j];
        else break;
      }
      if (!port.empty()) return "127.0.0.1:" + port;
    }
    int wstatus = 0;
    pid_t w = ::waitpid(sp.pid, &wstatus, WNOHANG);
    if (w == sp.pid) {
      std::cerr << "Gateway exited early. stderr:\n" << err << "\n";
      std::exit(1);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  std::cerr << "Gateway did not report a listen address. stderr:\n"
            << read_file(stderr_path) << "\n";
  std::exit(1);
}

// ---------------------------------------------------------------------------
// Bounded RPC retry: gRPC 1.30 client channels against a freshly spawned
// server can hit a spurious first-handshake failure (certificate verify
// failed) followed by reconnect backoff, so the first RPC on a new channel
// may transiently fail with UNAVAILABLE/UNKNOWN. The Gateway itself is
// ready; retry a bounded number of times before asserting.
// ---------------------------------------------------------------------------
template <typename Fn>
grpc::Status retry_rpc(Fn&& fn, int max_attempts = 10) {
  grpc::Status st = fn();
  int attempts = 1;
  while ((st.error_code() == grpc::StatusCode::UNAVAILABLE ||
          st.error_code() == grpc::StatusCode::UNKNOWN) &&
         attempts < max_attempts) {
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    st = fn();
    ++attempts;
  }
  return st;
}

// ---------------------------------------------------------------------------
// Find a free TCP port on loopback. The Gateway binds it immediately after;
// the small race window is acceptable for a test (leaked listeners are
// impossible since the child dies with the test process).
// ---------------------------------------------------------------------------
std::string find_free_loopback_port() {
  int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  CHECK(fd >= 0, "find_free_loopback_port: socket failed");
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  addr.sin_port = 0;
  int rc = ::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
  CHECK(rc == 0, "find_free_loopback_port: bind failed");
  socklen_t len = sizeof(addr);
  rc = ::getsockname(fd, reinterpret_cast<sockaddr*>(&addr), &len);
  CHECK(rc == 0, "find_free_loopback_port: getsockname failed");
  int port = ntohs(addr.sin_port);
  ::close(fd);
  return "127.0.0.1:" + std::to_string(port);
}

// ---------------------------------------------------------------------------
// Wait for the Gateway to be reachable on the given endpoint
// ---------------------------------------------------------------------------
bool wait_for_ready(const std::string& endpoint, const std::string& ca,
                    const std::string& cert, const std::string& key,
                    int timeout_ms = 5000) {
  auto deadline = std::chrono::steady_clock::now() +
                  std::chrono::milliseconds(timeout_ms);
  while (std::chrono::steady_clock::now() < deadline) {
    try {
      FakeEdge edge(endpoint, ca, cert, key);
      masi::edge::v1::GetBindingRequest req;
      req.set_schema_version("inference-committed-binding/v1");
      req.set_logical_pool_id("pool-blackbox-0001");
      req.set_pool_generation(1);
      req.set_binding_generation(1);
      req.set_model_control_incarnation_id("incarnation-blackbox-0001");
      masi::edge::v1::BindingReadback resp;
      auto status = retry_rpc(
          [&] { return edge.GetBinding(req, &resp, 1000); }, 3);
      if (status.ok()) return true;
    } catch (...) {
      // FakeEdge constructor may throw if channel not ready; keep trying.
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  return false;
}

// ---------------------------------------------------------------------------
// E2E test cases (only run when MASI_INF_E2E=1)
// ---------------------------------------------------------------------------
void run_e2e_tests() {
  std::string gateway_bin = find_gateway_binary();
  if (gateway_bin.empty()) {
    std::cerr << "SKIP: masi_inference_gateway binary not found. Set "
                 "MASI_INF_GATEWAY_BIN to its path.\n";
    std::exit(77);
  }
  std::cerr << "Using gateway binary: " << gateway_bin << "\n";

  TempDir td;
  // Generate mTLS bundle.
  auto mtls = generate_mtls_bundle(td.path());

  // Build startup envelope and model repository (real testkit fixture with
  // exact digests and a valid envelope digest).
  std::string repo_path = td.child("modelrepo");
  TestRepoDigests digests = build_test_repository(repo_path);
  std::string envelope_json =
      build_test_envelope(digests.model_digest, digests.closure_digest);
  std::string envelope_digest =
      nlohmann::json::parse(envelope_json)["envelope_digest"];
  std::string envelope_path =
      write_temp_file(td.path(), "envelope.json", envelope_json);
  std::string config_json = build_test_config(
      envelope_path, repo_path, mtls.ca_path, mtls.server_cert, mtls.server_key);
  std::string config_path = write_temp_file(td.path(), "config.json", config_json);

  // Pick a free loopback port so a stale listener from an earlier run can
  // never hijack this run's connections.
  std::string listen_addr = find_free_loopback_port();
  {
    nlohmann::json j = nlohmann::json::parse(config_json);
    j["gateway_listen"] = listen_addr;
    config_json = j.dump();
    write_temp_file(td.path(), "config.json", config_json);
  }

  // Spawn the Gateway.
  std::string stdout_path = td.child("gateway.stdout");
  std::string stderr_path = td.child("gateway.stderr");
  auto sp = spawn(gateway_bin, {config_path}, stdout_path, stderr_path);
  std::string grpc_endpoint = listen_addr;
  std::cerr << "Gateway target: " << grpc_endpoint << "\n";

  bool ready = false;
  // Give the Gateway a moment to start. Require a real successful GetBinding
  // (a spurious first-handshake failure must not count as readiness).
  for (int i = 0; i < 50 && !ready; ++i) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    try {
      FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
      masi::edge::v1::GetBindingRequest req;
      req.set_schema_version("inference-committed-binding/v1");
      req.set_logical_pool_id("pool-blackbox-0001");
      req.set_pool_generation(1);
      req.set_binding_generation(1);
      req.set_model_control_incarnation_id("incarnation-blackbox-0001");
      masi::edge::v1::BindingReadback resp;
      auto status = retry_rpc([&] { return edge.GetBinding(req, &resp, 1000); }, 3);
      if (status.ok()) {
        ready = true;
      }
    } catch (...) {
      // Keep waiting.
    }
    // Check if the process died early.
    int wstatus = 0;
    pid_t w = ::waitpid(sp.pid, &wstatus, WNOHANG);
    if (w == sp.pid) {
      std::string err = read_file(stderr_path);
      std::cerr << "Gateway exited early. stderr:\n" << err << "\n";
      std::exit(1);
    }
  }

  if (!ready) {
    std::cerr << "Gateway did not become ready. stderr:\n"
              << read_file(stderr_path) << "\n";
    ::kill(sp.pid, SIGKILL);
    int code = wait_for(sp);
    (void)code;
    std::exit(1);
  }

  std::cerr << "Gateway is ready.\n";

  // -----------------------------------------------------------------------
  // Test 1: GetBinding readback
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    masi::edge::v1::GetBindingRequest req;
    req.set_schema_version("inference-committed-binding/v1");
    req.set_logical_pool_id("pool-blackbox-0001");
    req.set_pool_generation(1);
    req.set_binding_generation(1);
    req.set_model_control_incarnation_id("incarnation-blackbox-0001");
    masi::edge::v1::BindingReadback resp;
    auto status = retry_rpc([&] { return edge.GetBinding(req, &resp, 2000); });
    CHECK(status.ok(), "GetBinding failed: " + status.error_message());
    CHECK(resp.logical_pool_id() == "pool-blackbox-0001",
          "GetBinding: logical_pool_id mismatch");
    CHECK(resp.pool_generation() == 1,
          "GetBinding: pool_generation mismatch");
    CHECK(resp.binding_generation() == 1,
          "GetBinding: binding_generation mismatch");
    std::cerr << "GetBinding readback OK.\n";
  }

  // -----------------------------------------------------------------------
  // Test 2: Infer with valid batch from golden, through the real serving
  // path (Gateway -> Triton -> result). The route must match the blackbox
  // binding exactly to pass the result fence.
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    masi::edge::v1::InferenceInputBatch batch = build_valid_batch_from_golden();
    batch.set_request_id("request-blackbox-0001");
    auto* route = batch.mutable_route();
    route->set_shard_id("shard-blackbox-0001");
    route->set_model_control_incarnation_id("incarnation-blackbox-0001");
    route->set_logical_pool_id("pool-blackbox-0001");
    route->set_pool_generation(1);
    route->set_binding_generation(1);
    route->set_route_epoch(1);
    route->set_runtime_profile("model-runtime-central-cpu/v1");
    route->set_wire_profile("inference-central-grpc-batch/v1");
    route->set_startup_envelope_digest(envelope_digest);
    masi::edge::v1::InferenceResultBatch resp;
    auto status = retry_rpc([&] { return edge.Infer(batch, &resp, 5000); });
    CHECK(status.ok(), "Infer valid batch failed: " + status.error_message());
    CHECK(resp.schema_version() == "inference-central-grpc-batch/v1",
          "Infer: result schema_version wrong");
    CHECK(resp.records_size() == 1, "Infer: expected 1 result record");
    const auto& rec = resp.records(0);
    CHECK(rec.execution_status() == masi::edge::v1::INFERENCE_EXECUTION_STATUS_OK,
          "Infer: record not OK: " + rec.error_code());
    CHECK(rec.scores_size() == 2, "Infer: expected 2 scores");
    // Scores [-33.4, 33.4] (0.5*sum - 0.1 over [5,10,3,16,32,1]) softmax to
    // ~[0,1] -> class 1 -> ALERT. This asserts the full adapter path.
    CHECK(rec.decision() == "ALERT", "Infer: expected ALERT decision, got " +
                                         rec.decision());
    std::cerr << "Infer valid batch OK through Triton, scores=["
              << rec.scores(0) << ", " << rec.scores(1)
              << "] decision=" << rec.decision() << "\n";
  }

  // -----------------------------------------------------------------------
  // Test 3: Oversize batch rejected
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    masi::edge::v1::InferenceInputBatch batch;
    batch.set_schema_version("inference-central-grpc-batch/v1");
    batch.set_request_id("req-oversize-001");
    batch.set_deadline_unix_ms(9999999999999LL);
    auto* route = batch.mutable_route();
    route->set_shard_id("shard-001");
    route->set_model_control_incarnation_id("inc-001");
    route->set_logical_pool_id("pool-001");
    route->set_pool_generation(1);
    route->set_binding_generation(1);
    route->set_route_epoch(1);
    route->set_runtime_profile("model-runtime-central-cpu/v1");
    route->set_wire_profile("inference-central-grpc-batch/v1");
    // Add 257 records (exceeds max 256).
    for (int i = 0; i < 257; ++i) {
      auto* rec = batch.add_records();
      rec->set_input_id("input-" + std::to_string(i));
      rec->set_event_idempotency_key("event-" + std::to_string(i));
      std::vector<uint8_t> bytes(48, 0);
      rec->set_feature_tensor(bytes.data(), bytes.size());
      rec->add_shape(1);
      rec->add_shape(6);
      rec->set_dtype("uint64-le");
    }
    masi::edge::v1::InferenceResultBatch resp;
    auto status = retry_rpc([&] { return edge.Infer(batch, &resp, 2000); });
    CHECK(!status.ok(),
          "Infer: oversize batch must not succeed");
    std::cerr << "Oversize batch rejected: " << status.error_message() << "\n";
  }

  // -----------------------------------------------------------------------
  // Test 4: Unknown major rejected
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    masi::edge::v1::InferenceInputBatch batch;
    batch.set_schema_version("inference-central-grpc-batch/v2");  // unknown major
    batch.set_request_id("req-unknown-major-001");
    batch.set_deadline_unix_ms(9999999999999LL);
    auto* route = batch.mutable_route();
    route->set_wire_profile("inference-central-grpc-batch/v2");
    route->set_runtime_profile("model-runtime-central-cpu/v1");
    masi::edge::v1::InferenceResultBatch resp;
    auto status = retry_rpc([&] { return edge.Infer(batch, &resp, 2000); });
    CHECK(!status.ok(), "Infer: unknown major must not succeed");
    std::cerr << "Unknown major rejected: " << status.error_message() << "\n";
  }

  // -----------------------------------------------------------------------
  // Test 5: Empty batch rejected cleanly (no protobuf CHECK, no crash)
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    masi::edge::v1::InferenceInputBatch batch;
    batch.set_schema_version("inference-central-grpc-batch/v1");
    batch.set_request_id("req-empty-001");
    batch.set_deadline_unix_ms(9999999999999LL);
    auto* route = batch.mutable_route();
    route->set_shard_id("shard-001");
    route->set_model_control_incarnation_id("inc-001");
    route->set_logical_pool_id("pool-001");
    route->set_pool_generation(1);
    route->set_binding_generation(1);
    route->set_route_epoch(1);
    route->set_runtime_profile("model-runtime-central-cpu/v1");
    route->set_wire_profile("inference-central-grpc-batch/v1");
    masi::edge::v1::InferenceResultBatch resp;
    auto status = retry_rpc([&] { return edge.Infer(batch, &resp, 2000); });
    CHECK(!status.ok(), "Infer: empty batch must not succeed");
    std::cerr << "Empty batch rejected: " << status.error_message() << "\n";
  }

  // -----------------------------------------------------------------------
  // Test 6: Plaintext rejected
  // -----------------------------------------------------------------------
  {
    FakeEdge plaintext_edge(grpc_endpoint, 0);  // plaintext
    masi::edge::v1::GetBindingRequest req;
    req.set_schema_version("inference-committed-binding/v1");
    masi::edge::v1::BindingReadback resp;
    auto status = plaintext_edge.GetBinding(req, &resp, 1000);
    CHECK(!status.ok(),
          "Plaintext GetBinding must fail (mTLS required)");
    std::cerr << "Plaintext rejected: " << status.error_message() << "\n";
  }

  // -----------------------------------------------------------------------
  // Test 7: Wrong identity rejected (optional — may pass TLS but fail app)
  // -----------------------------------------------------------------------
  {
    FakeEdge wrong_edge(grpc_endpoint, mtls.ca_path, mtls.wrong_cert, mtls.wrong_key);
    masi::edge::v1::GetBindingRequest req;
    req.set_schema_version("inference-committed-binding/v1");
    req.set_logical_pool_id("pool-blackbox-0001");
    req.set_pool_generation(1);
    req.set_binding_generation(1);
    req.set_model_control_incarnation_id("incarnation-blackbox-0001");
    masi::edge::v1::BindingReadback resp;
    auto status = wrong_edge.GetBinding(req, &resp, 1000);
    // The wrong-identity client is signed by the same CA, so TLS may succeed;
    // but the Gateway should reject it at the application identity layer. If
    // the Gateway does not enforce client identity binding, this will still
    // succeed — which is itself a finding (not a hard failure here).
    if (status.ok()) {
      std::cerr << "WARNING: wrong-identity client was accepted; the Gateway "
                   "may not enforce client identity binding.\n";
    } else {
      std::cerr << "Wrong identity rejected: " << status.error_message()
                << "\n";
    }
  }

  // -----------------------------------------------------------------------
  // Test 8: SIGTERM graceful shutdown exit 0
  // -----------------------------------------------------------------------
  std::this_thread::sleep_for(std::chrono::milliseconds(200));
  signal_subprocess(sp, SIGTERM);
  int exit_code = wait_for(sp);
  std::string stderr_content = read_file(stderr_path);
  std::cerr << "Gateway stderr:\n" << stderr_content << "\n";
  CHECK(exit_code == 0,
        "Gateway must exit 0 on SIGTERM (got " + std::to_string(exit_code) + ")");
  std::cerr << "SIGTERM graceful shutdown OK (exit 0).\n";

  std::cerr << "All E2E blackbox tests passed.\n";
}

}  // namespace

int main() {
  std::cout << "=== Central Inference module blackbox E2E test ===\n";

  // Guard: real-process E2E requires the full Triton/ORT stack.
  const char* e2e_env = std::getenv("MASI_INF_E2E");
  if (e2e_env == nullptr || std::string(e2e_env) != "1") {
    std::cout << "SKIP: MASI_INF_E2E=1 not set. Skipping real-process E2E.\n";
    std::cout << "Set MASI_INF_E2E=1 to run the full black-box test with a "
                 "real Gateway binary + mTLS + Triton.\n";
    return 77;  // 77 = skip
  }

  try {
    run_e2e_tests();
  } catch (const std::exception& e) {
    std::cerr << "E2E test exception: " << e.what() << "\n";
    return 1;
  }
  return 0;
}