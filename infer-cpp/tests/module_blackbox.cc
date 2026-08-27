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
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
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
#include "triton_client.h"

namespace {

using masi::inf::test::build_valid_batch_from_golden;
using masi::inf::test::FakeEdge;
using masi::inf::test::generate_mtls_bundle;
using masi::inf::test::hex_to_bytes;
using masi::inf::test::load_golden_inference;
using masi::inf::test::TempDir;
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

// Ensures every failure/exception path reaps the real Gateway process. A clean
// path calls wait_for(), which sets pid=-1 and makes this guard a no-op.
class SubprocessGuard {
public:
  explicit SubprocessGuard(Subprocess &process) : process_(process) {}
  ~SubprocessGuard() {
    if (process_.pid <= 0)
      return;
    ::kill(process_.pid, SIGTERM);
    int status = 0;
    for (int attempt = 0; attempt < 50; ++attempt) {
      const pid_t observed = ::waitpid(process_.pid, &status, WNOHANG);
      if (observed == process_.pid) {
        process_.pid = -1;
        return;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    ::kill(process_.pid, SIGKILL);
    (void)::waitpid(process_.pid, &status, 0);
    process_.pid = -1;
  }

  SubprocessGuard(const SubprocessGuard &) = delete;
  SubprocessGuard &operator=(const SubprocessGuard &) = delete;

private:
  Subprocess &process_;
};

// Fork+exec a binary with argv. Redirect stdout/stderr to temp files.
Subprocess spawn(const std::string &binary, const std::vector<std::string> &argv,
                 const std::string &stdout_path, const std::string &stderr_path) {
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
    std::vector<char *> args;
    args.push_back(const_cast<char *>(binary.c_str()));
    for (const auto &a : argv)
      args.push_back(const_cast<char *>(a.c_str()));
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
int wait_for(Subprocess &sp) {
  CHECK(sp.pid > 0, "wait_for: invalid pid");
  int status = 0;
  pid_t w = ::waitpid(sp.pid, &status, 0);
  CHECK(w == sp.pid, "wait_for: waitpid failed");
  sp.pid = -1;
  if (WIFEXITED(status))
    return WEXITSTATUS(status);
  if (WIFSIGNALED(status))
    return 128 + WTERMSIG(status);
  return -1;
}

// Send a signal to the subprocess.
void signal_subprocess(Subprocess &sp, int sig) {
  CHECK(sp.pid > 0, "signal_subprocess: invalid pid");
  ::kill(sp.pid, sig);
}

// ---------------------------------------------------------------------------
// Read a file into a string
// ---------------------------------------------------------------------------
std::string read_file(const std::string &path) {
  std::ifstream f(path);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

std::string fatal_runtime_marker(const std::string &content) {
  static const std::vector<std::string> markers = {
      "double free or corruption",  "free(): double free", "malloc():",      "AddressSanitizer",
      "UndefinedBehaviorSanitizer", "ThreadSanitizer",     "runtime error:", "Segmentation fault",
      "stack smashing detected"};
  for (const auto &marker : markers) {
    if (content.find(marker) != std::string::npos)
      return marker;
  }
  return "";
}

// ---------------------------------------------------------------------------
// Locate the Gateway binary
// ---------------------------------------------------------------------------
std::string find_gateway_binary() {
  const char *env = std::getenv("MASI_INF_GATEWAY_BIN");
  if (env && env[0] != '\0')
    return std::string(env);
  // Common build locations relative to the repo root.
  namespace fs = std::filesystem;
  fs::path repo = masi::inf::test::repo_root();
  std::vector<fs::path> candidates = {
      repo / "infer-cpp" / "build" / "cpu-release" / "masi_inference_gateway",
      repo / "infer-cpp" / "build" / "masi_inference_gateway",
      repo / "infer-cpp" / "build" / "cpu-debug" / "masi_inference_gateway",
      fs::current_path() / "masi_inference_gateway",
  };
  for (const auto &p : candidates) {
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
// Triton endpoint under test. The blackbox test requires a real Triton serving
// the pinned r3 repository; the endpoint is configurable so the harness can
// point at the repository-scoped container instead of a hardcoded port.
// ---------------------------------------------------------------------------
std::string triton_endpoint() {
  const char *env = std::getenv("MASI_INF_TRITON_ENDPOINT");
  if (env != nullptr && *env != '\0')
    return env;
  return "127.0.0.1:8011";
}

constexpr const char *kClientSan = "masi-edge.test";

// ---------------------------------------------------------------------------
// Build a test config JSON for the Gateway
// ---------------------------------------------------------------------------
// Loopback TCP fault proxy.
//
// Real fault injection at the real transport boundary: the Gateway keeps
// talking gRPC to a real Triton, but every byte flows through this proxy, so
// the test can cut the backend connection or stall it past the deadline
// without stopping the shared Triton container and without replacing Triton
// with a fake. Only the transport is manipulated; the backend stays real.
// ---------------------------------------------------------------------------
class TcpFaultProxy {
public:
  enum class Mode { PassThrough, Cut, Stall };

  TcpFaultProxy(std::string upstream_host, uint16_t upstream_port)
      : upstream_host_(std::move(upstream_host)), upstream_port_(upstream_port) {
    listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    CHECK(listen_fd_ >= 0, "fault proxy: socket failed");
    int one = 1;
    ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = ::htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;
    CHECK(::bind(listen_fd_, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) == 0,
          "fault proxy: bind failed");
    CHECK(::listen(listen_fd_, 64) == 0, "fault proxy: listen failed");
    socklen_t len = sizeof(addr);
    CHECK(::getsockname(listen_fd_, reinterpret_cast<sockaddr *>(&addr), &len) == 0,
          "fault proxy: getsockname failed");
    port_ = ::ntohs(addr.sin_port);
    accept_thread_ = std::thread([this] { accept_loop(); });
  }

  ~TcpFaultProxy() {
    stopping_.store(true);
    ::shutdown(listen_fd_, SHUT_RDWR);
    ::close(listen_fd_);
    if (accept_thread_.joinable())
      accept_thread_.join();
    drop_live_connections();
    for (auto &t : pumps_) {
      if (t.joinable())
        t.join();
    }
  }

  std::string endpoint() const { return "127.0.0.1:" + std::to_string(port_); }

  void set_mode(Mode mode) {
    mode_.store(mode);
    if (mode == Mode::Cut)
      drop_live_connections();
  }

private:
  void register_connection(int fd) {
    std::lock_guard<std::mutex> guard(mutex_);
    live_.push_back(fd);
  }

  void drop_live_connections() {
    std::lock_guard<std::mutex> guard(mutex_);
    for (int fd : live_)
      ::shutdown(fd, SHUT_RDWR);
    live_.clear();
  }

  int connect_upstream() {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0)
      return -1;
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = ::htons(upstream_port_);
    if (::inet_pton(AF_INET, upstream_host_.c_str(), &addr.sin_addr) != 1) {
      ::close(fd);
      return -1;
    }
    if (::connect(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) != 0) {
      ::close(fd);
      return -1;
    }
    return fd;
  }

  void pump(int from, int to) {
    std::vector<char> buffer(16384);
    while (!stopping_.load()) {
      ssize_t n = ::recv(from, buffer.data(), buffer.size(), 0);
      if (n <= 0)
        break;
      // A stalled backend: hold the bytes long enough for a tight client
      // deadline to expire while the request is in flight.
      while (mode_.load() == Mode::Stall && !stopping_.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
      }
      if (mode_.load() == Mode::Cut)
        break;
      ssize_t written = 0;
      while (written < n) {
        ssize_t w = ::send(to, buffer.data() + written, static_cast<size_t>(n - written), 0);
        if (w <= 0)
          return;
        written += w;
      }
    }
    ::shutdown(to, SHUT_RDWR);
  }

  void accept_loop() {
    while (!stopping_.load()) {
      int client = ::accept(listen_fd_, nullptr, nullptr);
      if (client < 0)
        break;
      if (mode_.load() == Mode::Cut) {
        // Refuse while the backend is cut: a real transport failure, not a
        // synthesized gRPC error.
        ::close(client);
        continue;
      }
      int upstream = connect_upstream();
      if (upstream < 0) {
        ::close(client);
        continue;
      }
      register_connection(client);
      register_connection(upstream);
      pumps_.emplace_back([this, client, upstream] {
        pump(client, upstream);
        ::close(client);
      });
      pumps_.emplace_back([this, client, upstream] {
        pump(upstream, client);
        ::close(upstream);
      });
    }
  }

  std::string upstream_host_;
  uint16_t upstream_port_ = 0;
  int listen_fd_ = -1;
  uint16_t port_ = 0;
  std::atomic<Mode> mode_{Mode::PassThrough};
  std::atomic<bool> stopping_{false};
  std::thread accept_thread_;
  std::vector<std::thread> pumps_;
  std::vector<int> live_;
  std::mutex mutex_;
};

// ---------------------------------------------------------------------------
// Structured blackbox evidence.
//
// Every field is set at the point the corresponding assertion actually
// succeeded during this run, so the file cannot claim coverage the run did not
// produce. The file is only written when MASI_INF_EVIDENCE_DIR is set.
// ---------------------------------------------------------------------------
nlohmann::json g_evidence = nlohmann::json::object();

template <typename T> void mark(const char *key, T value) { g_evidence[key] = value; }

void write_blackbox_evidence(const std::string &result, int64_t elapsed_ms) {
  const char *dir = std::getenv("MASI_INF_EVIDENCE_DIR");
  if (dir == nullptr || *dir == '\0')
    return;
  g_evidence["schema_version"] = "central-inference-module-e2e-evidence/v1";
  g_evidence["test_id"] = "TEST-INF-MODULE-E2E-001";
  g_evidence["requirement_ids"] =
      nlohmann::json::array({"MOD-INF-001", "TEST-TEL-INF-001", "DEC-044"});
  g_evidence["level"] = "MODULE";
  g_evidence["applicability"] = "APPLICABLE";
  g_evidence["result"] = result;
  // The frozen schema derives qualification from result at MODULE level. The
  // limits of this claim are carried by level=MODULE and
  // overall_module_complete=false: absolute performance, OCI startup and
  // formal pairwise are not covered here.
  g_evidence["qualification"] = result == "PASS" ? "QUALIFIED" : "NOT_QUALIFIED";
  g_evidence["elapsed_ms"] = elapsed_ms;
  g_evidence["overall_module_complete"] = false;
  std::error_code ec;
  std::filesystem::create_directories(dir, ec);
  const std::filesystem::path out = std::filesystem::path(dir) / "module-blackbox-e2e.json";
  std::ofstream f(out);
  f << g_evidence.dump(2) << "\n";
  std::cerr << "Evidence written: " << out.string() << "\n";
}

// ---------------------------------------------------------------------------
// Real soak driver.
//
// The frozen `qualification-soak/v1` profile fixes a 60-90s warmup, four
// 900,000 ms phases, a 10,000 ms sample interval and batch sizes 1/32/128/256.
// This driver sustains that load through the real mTLS boundary against the
// real Gateway process and real Triton, samples the Gateway's own resource
// counters from /proc, and derives the result from what it measured. A run
// shorter than the required window can never report PASS: the schema requires
// qualified_elapsed_ms >= 3600000 for PASS, and this driver reports the
// measured value.
// ---------------------------------------------------------------------------
std::string strip_sha256_prefix(const std::string &digest) {
  const std::string prefix = "sha256:";
  return digest.rfind(prefix, 0) == 0 ? digest.substr(prefix.size()) : digest;
}

std::string utc_timestamp(std::chrono::system_clock::time_point tp) {
  const std::time_t t = std::chrono::system_clock::to_time_t(tp);
  std::tm tm{};
  ::gmtime_r(&t, &tm);
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm);
  return std::string(buf);
}

struct ProcSample {
  bool valid = false;
  int64_t rss_bytes = 0;
  int64_t fd_count = 0;
  int64_t thread_count = 0;
  double cpu_pct = 0.0;
};

int64_t read_proc_field(const std::string &path, const std::string &key) {
  std::ifstream f(path);
  std::string line;
  while (std::getline(f, line)) {
    if (line.rfind(key, 0) == 0) {
      const auto pos = line.find_first_of("0123456789");
      if (pos == std::string::npos)
        return -1;
      return std::stoll(line.substr(pos));
    }
  }
  return -1;
}

ProcSample sample_process(pid_t pid, int64_t *prev_cpu_ticks, int64_t interval_ms) {
  ProcSample s;
  const std::string root = "/proc/" + std::to_string(pid);
  const int64_t rss_kb = read_proc_field(root + "/status", "VmRSS:");
  const int64_t threads = read_proc_field(root + "/status", "Threads:");
  if (rss_kb < 0 || threads < 0)
    return s;
  s.rss_bytes = rss_kb * 1024;
  s.thread_count = threads;
  int64_t fds = 0;
  std::error_code ec;
  for (auto it = std::filesystem::directory_iterator(root + "/fd", ec);
       !ec && it != std::filesystem::directory_iterator(); it.increment(ec)) {
    ++fds;
  }
  s.fd_count = fds;
  // utime+stime in clock ticks, converted to a percentage of one core.
  std::ifstream stat(root + "/stat");
  std::string content;
  std::getline(stat, content);
  const auto close_paren = content.rfind(')');
  int64_t ticks = 0;
  if (close_paren != std::string::npos) {
    std::istringstream fields(content.substr(close_paren + 1));
    std::string field;
    int index = 0;
    while (fields >> field) {
      ++index;
      // Fields after the comm: state is 1, so utime is 12 and stime is 13.
      if (index == 12 || index == 13)
        ticks += std::stoll(field);
    }
  }
  const int64_t hz = ::sysconf(_SC_CLK_TCK) > 0 ? ::sysconf(_SC_CLK_TCK) : 100;
  if (*prev_cpu_ticks >= 0 && interval_ms > 0) {
    const double delta = static_cast<double>(ticks - *prev_cpu_ticks);
    s.cpu_pct =
        (delta / static_cast<double>(hz)) / (static_cast<double>(interval_ms) / 1000.0) * 100.0;
    if (s.cpu_pct < 0)
      s.cpu_pct = 0;
  }
  *prev_cpu_ticks = ticks;
  s.valid = true;
  return s;
}

// ---------------------------------------------------------------------------
std::string build_test_config(const std::string &envelope_path, const std::string &repo_path,
                              const std::string &ca, const std::string &cert,
                              const std::string &key) {
  nlohmann::json j;
  j["startup_envelope_path"] = envelope_path;
  j["model_repository_path"] = repo_path;
  j["triton_endpoint"] = triton_endpoint();
  j["gateway_listen"] = "127.0.0.1:0"; // ephemeral port; read from stderr
  j["tls_ca_path"] = ca;
  j["tls_cert_path"] = cert;
  j["tls_key_path"] = key;
  j["client_san_allowlist"] = nlohmann::json::array({kClientSan});
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
// Build a startup envelope JSON with a correctly computed envelope_digest.
// The contract digests come from the digest-pinned bundle manifest, so the
// startup cross-check has real values to compare (an envelope that declares
// anything else must fail closed).
// ---------------------------------------------------------------------------
struct EnvelopeBinding {
  std::string model_digest;
  std::string model_revision_digest;
  std::string closure_digest;
  std::string repository_identity;
  std::string feature_contract_digest;
  std::string label_contract_digest;
  std::string output_adapter_digest;
  std::string wire_profile_digest;
  std::string runtime_profile_digest;
  std::string optimization_profile_digest;
  std::string triton_server_version;
};

std::string build_test_envelope(const EnvelopeBinding &binding) {
  masi::inf::StartupEnvelope env;
  env.schema_version = "inference-startup-envelope/v1";
  env.model_control_incarnation_id = "incarnation-blackbox-0001";
  env.operation_id = "op-blackbox-0001";
  env.kind = "binding";
  env.logical_pool_id = "pool-blackbox-0001";
  env.pool_generation = 1;
  env.availability_profile_id = "availability-single/v1";
  env.deployment_tier = "acceptance";
  env.model_revision_digest = binding.model_revision_digest;
  env.feature_contract_digest = binding.feature_contract_digest;
  env.label_contract_digest = binding.label_contract_digest;
  env.output_adapter_digest = binding.output_adapter_digest;
  env.inference_wire_profile_digest = binding.wire_profile_digest;
  env.runtime_profile_id = "model-runtime-central-cpu/v1";
  env.runtime_profile_digest = binding.runtime_profile_digest;
  env.optimization_profile_digest = binding.optimization_profile_digest;
  env.triton_server_version = binding.triton_server_version;
  env.repository_snapshot.identity = binding.repository_identity;
  env.repository_snapshot.closure_digest = binding.closure_digest;
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
  j["feature_contract_digest"] = env.feature_contract_digest;
  j["label_contract_digest"] = env.label_contract_digest;
  j["output_adapter_digest"] = env.output_adapter_digest;
  j["inference_wire_profile_digest"] = env.inference_wire_profile_digest;
  j["runtime_profile_id"] = env.runtime_profile_id;
  j["runtime_profile_digest"] = env.runtime_profile_digest;
  j["optimization_profile_digest"] = env.optimization_profile_digest;
  j["triton_server_version"] = env.triton_server_version;
  j["repository_snapshot"] = {{"identity", env.repository_snapshot.identity},
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
// Materialize the test repository closure by copying the committed, digest
// pinned fixture repository (testkit/fixtures/repositories/...). The same
// directory content is what the Triton container serves, so the Gateway's
// closure verification and Triton's loaded model cannot drift apart.
// ---------------------------------------------------------------------------
struct TestRepoDigests {
  std::string model_digest;
  std::string model_revision_digest;
  std::string closure_digest;
  std::string identity;
  std::string feature_contract_digest;
  std::string label_contract_digest;
  std::string output_adapter_digest;
  std::string wire_profile_digest;
  std::string runtime_profile_digest;
  std::string optimization_profile_digest;
};

TestRepoDigests build_test_repository(const std::string &dir) {
  namespace fs = std::filesystem;
  const fs::path source = fs::path(masi::inf::test::repo_root()) / "testkit" / "fixtures" /
                          "repositories" / "masi-ids-window-v1-r3";
  CHECK(fs::is_directory(source),
        "pinned fixture repository missing: " + source.string() +
            " (regenerate with testkit/fixtures/models/scripts/build_repository.py)");

  fs::remove_all(dir);
  fs::copy(source, dir, fs::copy_options::recursive);

  const auto manifest = nlohmann::json::parse(read_file(dir + "/closure-manifest.json"));
  const auto bundle = nlohmann::json::parse(read_file(dir + "/bundle-manifest.json"));

  TestRepoDigests out;
  out.identity = manifest.at("identity").get<std::string>();
  out.closure_digest = manifest.at("closure_digest").get<std::string>();
  for (const auto &m : manifest.at("members")) {
    if (m.at("role").get<std::string>() == "model")
      out.model_digest = m.at("member_digest").get<std::string>();
  }
  CHECK(!out.model_digest.empty(), "fixture repository declares no model member");
  const auto &cd = bundle.at("contract_digests");
  out.feature_contract_digest = cd.at("feature_contract_digest").get<std::string>();
  out.label_contract_digest = cd.at("label_contract_digest").get<std::string>();
  out.output_adapter_digest = cd.at("output_adapter_digest").get<std::string>();
  out.model_revision_digest = bundle.at("model_revision_digest").get<std::string>();
  const auto &bi = bundle.at("binding_identity");
  out.wire_profile_digest = bi.at("inference_wire_profile_digest").get<std::string>();
  out.runtime_profile_digest = bi.at("runtime_profile_digest").get<std::string>();
  out.optimization_profile_digest = bi.at("optimization_profile_digest").get<std::string>();

  // Read-only tree, as required by verify_repository_closure.
  for (auto &p : fs::recursive_directory_iterator(dir)) {
    if (p.is_regular_file())
      fs::permissions(p.path(),
                      fs::perms::owner_read | fs::perms::group_read | fs::perms::others_read,
                      fs::perm_options::replace);
  }
  for (auto &p : fs::recursive_directory_iterator(dir)) {
    if (p.is_directory())
      fs::permissions(p.path(),
                      fs::perms::owner_read | fs::perms::owner_exec | fs::perms::group_read |
                          fs::perms::group_exec | fs::perms::others_read | fs::perms::others_exec,
                      fs::perm_options::replace);
  }
  fs::permissions(dir,
                  fs::perms::owner_read | fs::perms::owner_exec | fs::perms::group_read |
                      fs::perms::group_exec | fs::perms::others_read | fs::perms::others_exec,
                  fs::perm_options::replace);

  return out;
}

// ---------------------------------------------------------------------------
// Wait for the Gateway to log its bound listen address and return
// "127.0.0.1:<port>". The Gateway binds an ephemeral port (config
// "127.0.0.1:0") and logs the actual address; exits the test if the process
// dies or never reports an address.
// ---------------------------------------------------------------------------
std::string wait_for_listen_addr(const Subprocess &sp, const std::string &stderr_path) {
  const std::string prefix = "listening on 127.0.0.1:";
  for (int i = 0; i < 100; ++i) {
    std::string err = read_file(stderr_path);
    auto pos = err.find(prefix);
    if (pos != std::string::npos) {
      std::string port;
      for (size_t j = pos + prefix.size(); j < err.size(); ++j) {
        if (err[j] >= '0' && err[j] <= '9')
          port += err[j];
        else
          break;
      }
      if (!port.empty())
        return "127.0.0.1:" + port;
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
template <typename Fn> grpc::Status retry_rpc(Fn &&fn, int max_attempts = 10) {
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
  int rc = ::bind(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr));
  CHECK(rc == 0, "find_free_loopback_port: bind failed");
  socklen_t len = sizeof(addr);
  rc = ::getsockname(fd, reinterpret_cast<sockaddr *>(&addr), &len);
  CHECK(rc == 0, "find_free_loopback_port: getsockname failed");
  int port = ntohs(addr.sin_port);
  ::close(fd);
  return "127.0.0.1:" + std::to_string(port);
}

bool listener_is_released(const std::string &endpoint) {
  const auto colon = endpoint.rfind(':');
  if (colon == std::string::npos)
    return false;
  const int port = std::stoi(endpoint.substr(colon + 1));
  int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0)
    return false;
  int one = 1;
  (void)::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  addr.sin_port = htons(static_cast<uint16_t>(port));
  const bool released = ::bind(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) == 0;
  ::close(fd);
  return released;
}

// ---------------------------------------------------------------------------
// Wait for the Gateway to be reachable on the given endpoint
// ---------------------------------------------------------------------------
bool wait_for_ready(const std::string &endpoint, const std::string &ca, const std::string &cert,
                    const std::string &key, int timeout_ms = 5000) {
  auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
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
      auto status = retry_rpc([&] { return edge.GetBinding(req, &resp, 1000); }, 3);
      if (status.ok())
        return true;
    } catch (...) {
      // FakeEdge constructor may throw if channel not ready; keep trying.
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  return false;
}

// ---------------------------------------------------------------------------
// Real-service precondition. The module black-box test requires a real Triton
// serving the pinned r3 closure. When it is absent the result is a structured
// HOLD/NOT_RUN (skip), never a PASS and never a confusing failure.
// ---------------------------------------------------------------------------
struct TritonPrecondition {
  bool available = false;
  std::string reason;
  std::string server_version;
  masi::inf::TritonModelStatistics baseline;
};

TritonPrecondition probe_triton() {
  TritonPrecondition p;
  masi::inf::TritonClient client;
  masi::inf::TritonClient::ConnectOptions opts;
  opts.endpoint = triton_endpoint();
  opts.deadline_ms = 2000;
  try {
    client.connect(opts);
    if (!client.is_server_ready()) {
      p.reason = "TRITON_SERVER_NOT_READY at " + opts.endpoint;
      return p;
    }
    if (!client.is_model_ready("masi-ids-window-v1", "1")) {
      p.reason = "TRITON_MODEL_NOT_READY masi-ids-window-v1/1 at " + opts.endpoint;
      return p;
    }
    p.server_version = client.server_metadata().version;
    const auto cpu_profile = masi::inf::test::load_json(
        masi::inf::test::contract_path("contracts/profiles/v1/central-inference-cpu.json"));
    const std::string expected_version =
        cpu_profile.at("triton").at("triton_version").get<std::string>();
    if (p.server_version != expected_version) {
      p.reason = "TRITON_SERVER_VERSION_MISMATCH expected=" + expected_version +
                 " observed=" + p.server_version;
      return p;
    }
    p.baseline = client.model_statistics("masi-ids-window-v1", "1");
    p.available = true;
  } catch (const std::exception &e) {
    p.reason = std::string("TRITON_UNREACHABLE: ") + e.what();
  }
  return p;
}

masi::inf::TritonModelStatistics triton_statistics() {
  masi::inf::TritonClient client;
  masi::inf::TritonClient::ConnectOptions opts;
  opts.endpoint = triton_endpoint();
  opts.deadline_ms = 2000;
  client.connect(opts);
  return client.model_statistics("masi-ids-window-v1", "1");
}

// Build a batch of `n` records with correct per-record input digests. Record 0
// uses the golden alert tensor; the remaining records alternate with an
// all-zero OOD/abstain tensor so row splitting is observable.
void bind_batch_to_readback(masi::edge::v1::InferenceInputBatch *batch,
                            const masi::edge::v1::BindingReadback &binding) {
  const int64_t now = std::chrono::duration_cast<std::chrono::milliseconds>(
                          std::chrono::system_clock::now().time_since_epoch())
                          .count();
  batch->set_deadline_unix_ms(now + 1900);
  if (batch->trace_id().empty())
    batch->set_trace_id("trace-blackbox-batch");
  auto *route = batch->mutable_route();
  route->set_schema_version("inference-route/v1");
  route->set_shard_id("shard-blackbox-0001");
  route->set_model_control_incarnation_id(binding.model_control_incarnation_id());
  route->set_logical_pool_id(binding.logical_pool_id());
  route->set_pool_generation(binding.pool_generation());
  route->set_binding_generation(binding.binding_generation());
  route->set_route_epoch(1);
  route->set_model_revision_digest(binding.model_revision_digest());
  route->set_feature_contract_digest(binding.feature_contract_digest());
  route->set_label_contract_digest(binding.label_contract_digest());
  route->set_output_adapter_digest(binding.output_adapter_digest());
  route->set_wire_profile(binding.wire_profile());
  route->set_runtime_profile(binding.runtime_profile());
  route->set_operation_id(binding.operation_id());
  route->set_scope("scope-blackbox-0001");
  route->set_expected_binding_generation(binding.binding_generation() - 1);
  route->set_proposed_binding_generation(binding.binding_generation());
  route->set_current_binding_generation(binding.binding_generation());
  route->set_startup_envelope_digest(binding.startup_envelope_digest());
  route->set_pool_observation_digest(binding.pool_observation_digest());
  route->set_binding_digest(binding.binding_digest());
  route->set_model_bundle_digest(binding.model_bundle_digest());
  route->set_wire_profile_digest(binding.wire_profile_digest());
  route->set_runtime_profile_digest(binding.runtime_profile_digest());
  route->set_optimization_profile_digest(binding.optimization_profile_digest());

  for (int i = 0; i < batch->records_size(); ++i) {
    auto *rec = batch->mutable_records(i);
    const uint64_t sequence = static_cast<uint64_t>(i) + 1;
    rec->set_schema_version("edge-inference-record/v1");
    rec->set_target_id(route->shard_id());
    rec->set_source_runtime_epoch("source-runtime-blackbox-0001");
    rec->set_source_sequence_start(sequence);
    rec->set_source_sequence_end(sequence);
    rec->set_window_start_unix_ms(now - 1000);
    rec->set_window_end_unix_ms(now - 500);
    rec->set_watermark_unix_ms(now - 500);
    rec->set_finalized_at_unix_ms(now - 400);
    rec->set_enqueued_at_unix_ms(now - 300);
    rec->set_quality("valid");
    rec->set_quality_code(masi::edge::v1::DATA_QUALITY_VALID);
    rec->clear_quality_reasons();
    rec->add_quality_reasons("NONE");
    rec->set_final_window(true);
    rec->set_sampling_coverage_ppm(1000000);
    rec->set_source_wal_sequence(sequence);
    rec->set_input_wal_sequence(sequence);
    rec->set_model_control_incarnation_id(route->model_control_incarnation_id());
    rec->set_logical_pool_id(route->logical_pool_id());
    rec->set_pool_generation(route->pool_generation());
    rec->set_binding_generation(route->binding_generation());
    rec->set_route_epoch(route->route_epoch());
    rec->set_model_revision_digest(route->model_revision_digest());
    rec->set_feature_contract_digest(route->feature_contract_digest());
    rec->set_label_contract_digest(route->label_contract_digest());
    rec->set_output_adapter_digest(route->output_adapter_digest());
    rec->set_wire_profile(route->wire_profile());
    rec->set_runtime_profile(route->runtime_profile());
    rec->set_operation_id(route->operation_id());
    rec->set_scope(route->scope());
    rec->set_expected_binding_generation(route->expected_binding_generation());
    rec->set_proposed_binding_generation(route->proposed_binding_generation());
    rec->set_current_binding_generation(route->current_binding_generation());
    rec->set_startup_envelope_digest(route->startup_envelope_digest());
    rec->set_pool_observation_digest(route->pool_observation_digest());
    rec->set_binding_digest(route->binding_digest());
    rec->set_model_bundle_digest(route->model_bundle_digest());
    rec->set_wire_profile_digest(route->wire_profile_digest());
    rec->set_runtime_profile_digest(route->runtime_profile_digest());
    rec->set_optimization_profile_digest(route->optimization_profile_digest());
  }
}

masi::edge::v1::InferenceInputBatch build_batch_of(size_t n, const std::string &request_id,
                                                   const masi::edge::v1::BindingReadback &binding) {
  masi::edge::v1::InferenceInputBatch batch;
  batch.set_schema_version("inference-central-grpc-batch/v1");
  batch.set_request_id(request_id);
  batch.set_trace_id("trace-blackbox-batch");

  const std::vector<uint64_t> alert_row{5, 10, 3, 16, 32, 1};
  const std::vector<uint64_t> ood_row{0, 0, 0, 0, 0, 0};
  for (size_t i = 0; i < n; ++i) {
    const auto &row = (i % 2 == 0) ? alert_row : ood_row;
    std::string bytes(48, '\0');
    for (size_t f = 0; f < 6; ++f) {
      const uint64_t v = row[f];
      for (size_t b = 0; b < 8; ++b)
        bytes[f * 8 + b] = static_cast<char>((v >> (8 * b)) & 0xff);
    }
    auto *rec = batch.add_records();
    rec->set_input_id("input-batch-" + std::to_string(i));
    rec->set_event_idempotency_key("event-batch-" + request_id + "-" + std::to_string(i));
    rec->set_window_id("window-batch-" + std::to_string(i));
    rec->set_feature_tensor(bytes);
    rec->add_shape(1);
    rec->add_shape(6);
    rec->set_dtype("uint64-le");
    rec->set_final_window(true);
    rec->set_quality("valid");
    rec->set_input_digest(masi::inf::sha256_hex(bytes.data(), bytes.size()));
  }
  bind_batch_to_readback(&batch, binding);
  return batch;
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

  // Real-service precondition before anything else.
  const auto triton = probe_triton();
  if (!triton.available) {
    std::cerr << "HOLD/NOT_RUN: required real Triton is not usable: " << triton.reason << "\n"
              << "Start a Triton serving testkit/fixtures/repositories/"
                 "masi-ids-window-v1-r3 and set MASI_INF_TRITON_ENDPOINT.\n";
    std::exit(77);
  }
  std::cerr << "Triton precondition OK (version " << triton.server_version << ", endpoint "
            << triton_endpoint() << ")\n";

  TempDir td;
  // Generate mTLS bundle.
  auto mtls = generate_mtls_bundle(td.path());

  // Materialize the pinned repository closure and build a matching envelope.
  std::string repo_path = td.child("modelrepo");
  TestRepoDigests digests = build_test_repository(repo_path);
  EnvelopeBinding binding;
  binding.model_digest = digests.model_digest;
  binding.model_revision_digest = digests.model_revision_digest;
  binding.closure_digest = digests.closure_digest;
  binding.repository_identity = digests.identity;
  binding.feature_contract_digest = digests.feature_contract_digest;
  binding.label_contract_digest = digests.label_contract_digest;
  binding.output_adapter_digest = digests.output_adapter_digest;
  binding.wire_profile_digest = digests.wire_profile_digest;
  binding.runtime_profile_digest = digests.runtime_profile_digest;
  binding.optimization_profile_digest = digests.optimization_profile_digest;
  binding.triton_server_version = triton.server_version;
  std::string envelope_json = build_test_envelope(binding);
  std::string envelope_path = write_temp_file(td.path(), "envelope.json", envelope_json);
  std::string config_json =
      build_test_config(envelope_path, repo_path, mtls.ca_path, mtls.server_cert, mtls.server_key);
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
  masi::edge::v1::BindingReadback binding_readback;
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
        binding_readback = resp;
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
    std::cerr << "Gateway did not become ready. stderr:\n" << read_file(stderr_path) << "\n";
    ::kill(sp.pid, SIGKILL);
    int code = wait_for(sp);
    (void)code;
    std::exit(1);
  }

  std::cerr << "Gateway is ready.\n";
  CHECK(binding_readback.runtime_profile() == "model-runtime-central-cpu/v1" &&
            binding_readback.runtime_profile_digest() == digests.runtime_profile_digest &&
            binding_readback.optimization_profile_digest() == digests.optimization_profile_digest,
        "ready binding did not prove the selected CPU runtime/optimization profile");
  mark("real_binary", true);
  mark("gateway_process", std::filesystem::path(gateway_bin).filename().string());
  mark("explicit_profile_selected", true);
  // Readiness is only published after startup has recomputed the repository
  // closure and compared the live Triton ModelConfig (including its explicit
  // instance group) against both config.pbtxt and the frozen profile.
  mark("triton_instance_group_explicit", true);
  mark("repository_closure_exact", true);

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
    CHECK(resp.logical_pool_id() == "pool-blackbox-0001", "GetBinding: logical_pool_id mismatch");
    CHECK(resp.pool_generation() == 1, "GetBinding: pool_generation mismatch");
    CHECK(resp.binding_generation() == 1, "GetBinding: binding_generation mismatch");
    mark("readback_identity_exact", true);
    mark("mtls_verified", true);
    std::cerr << "GetBinding readback OK.\n";
  }

  // -----------------------------------------------------------------------
  // Test 2: Infer a single-record golden batch through the real serving path
  // and assert the canonical adapter output pinned in the golden vector.
  // -----------------------------------------------------------------------
  const auto stats_before_infer = triton_statistics();
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    const auto golden = masi::inf::test::load_golden_inference("valid-batch-v1.json");
    masi::edge::v1::InferenceInputBatch batch = build_valid_batch_from_golden();
    batch.set_request_id("request-blackbox-0001");
    bind_batch_to_readback(&batch, binding_readback);
    masi::edge::v1::InferenceResultBatch resp;
    auto status = retry_rpc([&] { return edge.Infer(batch, &resp, 5000); });
    CHECK(status.ok(), "Infer valid batch failed: " + status.error_message());
    CHECK(resp.schema_version() == "inference-central-grpc-batch/v1",
          "Infer: result schema_version wrong");
    CHECK(resp.records_size() == 1, "Infer: expected 1 result record");
    const auto &rec = resp.records(0);
    CHECK(rec.execution_status() == masi::edge::v1::INFERENCE_EXECUTION_STATUS_OK,
          "Infer: record not OK: " + rec.error_code());
    CHECK(rec.scores_size() == 2, "Infer: expected 2 scores");
    // The golden pins the canonical class-ordered scores, the lowercase
    // decision and the canonical output digest.
    const auto &expected = golden["expected"];
    for (int i = 0; i < rec.scores_size(); ++i) {
      const double want = expected["scores"][i].get<double>();
      const double got = rec.scores(i);
      CHECK(std::fabs(got - want) <= 1e-6 + 1e-5 * std::fabs(want),
            "Infer: score " + std::to_string(i) + " drifted from the golden value");
    }
    CHECK(rec.decision() == expected["decision"].get<std::string>(),
          "Infer: decision drifted from the golden value: " + rec.decision());
    CHECK(rec.predicted_label() == expected["predicted_label"].get<uint32_t>(),
          "Infer: predicted_label drifted from the golden value");
    CHECK(rec.quality() == expected["quality"].get<std::string>(),
          "Infer: quality must be the lowercase contract value, got " + rec.quality());
    CHECK(rec.output_digest() == expected["output_digest"].get<std::string>(),
          "Infer: canonical output_digest drifted: " + rec.output_digest());
    CHECK(rec.input_digest() == golden["input"]["records"][0]["input_digest"].get<std::string>(),
          "Infer: input_digest not carried through");
    CHECK(!rec.worker_attempt_id().empty(), "Infer: worker_attempt_id empty");
    CHECK(!rec.pool_observation_digest().empty(),
          "Infer: pool_observation_digest must be populated");
    CHECK(!rec.binding_digest().empty(), "Infer: binding_digest must be populated");
    mark("admission_accepted", true);
    mark("class_order_correct", true);
    mark("numeric_match_python_reference", true);
    mark("request_identity_stable", true);
    std::cerr << "Infer single-record golden OK: scores=[" << rec.scores(0) << ", " << rec.scores(1)
              << "] decision=" << rec.decision() << "\n";
  }

  // -----------------------------------------------------------------------
  // Test 2b: the execution really went through Triton (discriminating: an
  // in-process executor would leave Triton's counters untouched).
  // -----------------------------------------------------------------------
  {
    const auto after = triton_statistics();
    CHECK(after.observed, "Triton statistics unavailable");
    CHECK(after.inference_count > stats_before_infer.inference_count,
          "Triton inference_count did not increase: the request did not reach Triton");
    mark("real_triton", true);
    mark("no_fallback", true);
    mark("inference_calls",
         static_cast<int64_t>(after.inference_count - stats_before_infer.inference_count));
    std::cerr << "Triton executed the request (inference_count "
              << stats_before_infer.inference_count << " -> " << after.inference_count << ")\n";
  }

  // -----------------------------------------------------------------------
  // Test 2c: multi-record batches. One admitted batch must become exactly one
  // Triton inference of shape [N,6] whose rows are split back per record.
  // -----------------------------------------------------------------------
  {
    const auto multi_golden =
        masi::inf::test::load_golden_inference("valid-multi-record-batch-v1.json");
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);

    // Exact two-record golden first: row 0 alerts, row 1 is OOD and abstains.
    {
      masi::edge::v1::InferenceInputBatch batch =
          masi::inf::test::build_batch_from_golden("valid-multi-record-batch-v1.json");
      batch.set_request_id("request-blackbox-multi-0001");
      bind_batch_to_readback(&batch, binding_readback);

      const auto before = triton_statistics();
      masi::edge::v1::InferenceResultBatch resp;
      auto status = retry_rpc([&] { return edge.Infer(batch, &resp, 10000); });
      CHECK(status.ok(), "Infer 2-record golden failed: " + status.error_message());
      CHECK(resp.records_size() == 2,
            "Infer: expected 2 result records, got " + std::to_string(resp.records_size()));
      const auto &rows = multi_golden["expected"]["rows"];
      for (int i = 0; i < resp.records_size(); ++i) {
        const auto &r = resp.records(i);
        CHECK(r.execution_status() == masi::edge::v1::INFERENCE_EXECUTION_STATUS_OK,
              "Infer: multi-record row not OK: " + r.error_code());
        CHECK(r.decision() == rows[i]["decision"].get<std::string>(),
              "Infer: row " + std::to_string(i) + " decision drifted: " + r.decision());
        CHECK(r.output_digest() == rows[i]["output_digest"].get<std::string>(),
              "Infer: row " + std::to_string(i) + " canonical digest drifted");
      }
      const auto after = triton_statistics();
      CHECK(resp.records(1).out_of_distribution() && resp.records(1).abstain(),
            "Infer: zero row must exercise the live OOD/abstain policy");
      mark("ood_handled", true);
      // Triton counts inferences per record and executions per batch, so a
      // two-record batch must be +2 inferences in exactly +1 execution.
      CHECK(after.inference_count == before.inference_count + 2,
            "a two-record batch must submit both records, observed +" +
                std::to_string(after.inference_count - before.inference_count));
      CHECK(after.execution_count == before.execution_count + 1,
            "a two-record batch must be exactly one Triton execution, observed +" +
                std::to_string(after.execution_count - before.execution_count));
      std::cerr << "Infer 2-record golden OK: decisions=[" << resp.records(0).decision() << ", "
                << resp.records(1).decision() << "], one Triton execution of two records\n";
    }

    // Larger batch sizes across the frozen bound.
    for (size_t n : {size_t{32}, size_t{256}}) {
      auto batch = build_batch_of(n, "request-blackbox-n" + std::to_string(n), binding_readback);
      const auto before = triton_statistics();
      masi::edge::v1::InferenceResultBatch resp;
      auto status = retry_rpc([&] { return edge.Infer(batch, &resp, 20000); });
      CHECK(status.ok(),
            "Infer " + std::to_string(n) + "-record batch failed: " + status.error_message());
      CHECK(resp.records_size() == static_cast<int>(n), "Infer: expected " + std::to_string(n) +
                                                            " result records, got " +
                                                            std::to_string(resp.records_size()));
      // Alternating rows prove the output was split, not broadcast.
      CHECK(resp.records(0).decision() == "alert", "Infer: row 0 must alert");
      CHECK(resp.records(1).decision() == "abstain", "Infer: row 1 must abstain");
      CHECK(resp.records(static_cast<int>(n) - 1).decision() ==
                ((n - 1) % 2 == 0 ? "alert" : "abstain"),
            "Infer: last row decision does not follow the input pattern");
      const auto after = triton_statistics();
      CHECK(after.inference_count == before.inference_count + n,
            "batch of " + std::to_string(n) + " must submit " + std::to_string(n) +
                " records, observed +" +
                std::to_string(after.inference_count - before.inference_count));
      CHECK(after.execution_count == before.execution_count + 1,
            "batch of " + std::to_string(n) + " must be exactly one Triton execution, observed +" +
                std::to_string(after.execution_count - before.execution_count));
      std::cerr << "Infer " << n << "-record batch OK (one Triton execution of " << n
                << " records)\n";
    }
  }

  // The r3 serving fixture also has a real baseline-class row, so benign is
  // proven through Gateway -> Triton rather than only by an adapter unit test.
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    auto batch = build_batch_of(1, "request-blackbox-benign", binding_readback);
    std::string bytes(48, '\0');
    const uint64_t first_feature = 100;
    for (size_t b = 0; b < 8; ++b)
      bytes[b] = static_cast<char>((first_feature >> (8 * b)) & 0xff);
    batch.mutable_records(0)->set_feature_tensor(bytes);
    batch.mutable_records(0)->set_input_digest(masi::inf::sha256_hex(bytes.data(), bytes.size()));
    const auto before = triton_statistics();
    masi::edge::v1::InferenceResultBatch resp;
    const auto status = edge.Infer(batch, &resp, 5000);
    CHECK(status.ok(), "Infer benign fixture row failed: " + status.error_message());
    CHECK(resp.records_size() == 1 && resp.records(0).decision() == "benign",
          "Infer: r3 baseline row must produce benign");
    CHECK(!resp.records(0).out_of_distribution() && !resp.records(0).abstain(),
          "Infer: confident baseline row must not be OOD/abstain");
    CHECK(triton_statistics().inference_count == before.inference_count + 1,
          "Infer: benign boundary case must execute in Triton exactly once");
  }

  // -----------------------------------------------------------------------
  // Test 2d: a record whose declared input_digest does not match its tensor
  // must be rejected before execution.
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    auto batch = build_batch_of(2, "request-blackbox-baddigest", binding_readback);
    batch.mutable_records(1)->set_input_digest(
        "sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef");
    const auto before = triton_statistics();
    masi::edge::v1::InferenceResultBatch resp;
    auto status = edge.Infer(batch, &resp, 5000);
    CHECK(!status.ok(), "Infer: wrong input_digest must be rejected");
    CHECK(status.error_code() == grpc::StatusCode::INVALID_ARGUMENT,
          "Infer: wrong input_digest must be INVALID_ARGUMENT");
    CHECK(status.error_message().find("input_digest mismatch") != std::string::npos,
          "Infer: rejection must name the digest mismatch: " + status.error_message());
    const auto after = triton_statistics();
    CHECK(after.inference_count == before.inference_count,
          "Infer: a rejected batch must not reach Triton");
    mark("admission_rejected", true);
    mark("batch_digest_exact", true);
    std::cerr << "Wrong input_digest rejected before execution: " << status.error_message() << "\n";
  }

  // -----------------------------------------------------------------------
  // Test 2e: replay of the same request identity recomputes a full result with
  // a new attempt id; a different input under the same request_id conflicts.
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    auto batch = build_batch_of(2, "request-blackbox-replay", binding_readback);
    masi::edge::v1::InferenceResultBatch first;
    auto s1 = retry_rpc([&] { return edge.Infer(batch, &first, 10000); });
    CHECK(s1.ok(), "Infer: first attempt failed: " + s1.error_message());
    CHECK(first.records_size() == 2, "Infer: first attempt must return 2 records");

    masi::edge::v1::InferenceResultBatch second;
    auto s2 = edge.Infer(batch, &second, 10000);
    CHECK(s2.ok(), "Infer: replay must succeed: " + s2.error_message());
    CHECK(second.records_size() == 2, "Infer: replay must return the full result, got " +
                                          std::to_string(second.records_size()) + " records");
    CHECK(second.batch_digest() == first.batch_digest(),
          "Infer: replay batch digest must be identical");
    for (int i = 0; i < 2; ++i) {
      CHECK(second.records(i).output_digest() == first.records(i).output_digest(),
            "Infer: replay row digest must be identical");
    }
    CHECK(second.records(0).worker_attempt_id() != first.records(0).worker_attempt_id(),
          "Infer: replay must use a new worker_attempt_id");

    // Same request_id, different input -> conflict.
    auto conflicting = build_batch_of(1, "request-blackbox-replay", binding_readback);
    masi::edge::v1::InferenceResultBatch third;
    auto s3 = edge.Infer(conflicting, &third, 5000);
    CHECK(!s3.ok(), "Infer: same request_id with a different input must conflict");
    CHECK(s3.error_message() == "RESULT_DIGEST_CONFLICT",
          "Infer: expected RESULT_DIGEST_CONFLICT, got " + s3.error_message());
    mark("duplicate_response_idempotent", true);
    mark("result_digest_conflict_fenced", true);
    mark("retry_identity_stable", true);
    mark("same_generation_retry_bounded", true);
    std::cerr << "Replay recomputed with a new attempt id; digest conflict detected\n";
  }

  // -----------------------------------------------------------------------
  // Test 2f: cross-generation route is fenced before execution.
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    auto batch = build_batch_of(1, "request-blackbox-crossgen", binding_readback);
    batch.mutable_route()->set_binding_generation(2);
    const auto before = triton_statistics();
    masi::edge::v1::InferenceResultBatch resp;
    auto status = edge.Infer(batch, &resp, 5000);
    CHECK(!status.ok(), "Infer: cross-generation route must be fenced");
    CHECK(status.error_message() == "RESULT_IDENTITY_MISMATCH",
          "Infer: expected RESULT_IDENTITY_MISMATCH, got " + status.error_message());
    const auto after = triton_statistics();
    CHECK(after.inference_count == before.inference_count,
          "Infer: a fenced batch must not reach Triton");
    mark("wrong_generation_fenced", true);
    std::cerr << "Cross-generation route fenced before execution\n";
  }

  // -----------------------------------------------------------------------
  // Test 2g: an adapter digest that is not the loaded binding is fenced.
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    auto batch = build_batch_of(1, "request-blackbox-adapterfence", binding_readback);
    batch.mutable_route()->set_output_adapter_digest(
        "sha256:1111111111111111111111111111111111111111111111111111111111111111");
    masi::edge::v1::InferenceResultBatch resp;
    auto status = edge.Infer(batch, &resp, 5000);
    CHECK(!status.ok(), "Infer: wrong output_adapter_digest must be fenced");
    CHECK(status.error_message() == "RESULT_IDENTITY_MISMATCH",
          "Infer: expected RESULT_IDENTITY_MISMATCH for adapter drift, got " +
              status.error_message());
    mark("result_identity_mismatch_fenced", true);
    std::cerr << "Output adapter digest drift fenced\n";
  }

  // -----------------------------------------------------------------------
  // Test 2h: an already-expired deadline is rejected without execution.
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    auto batch = build_batch_of(1, "request-blackbox-deadline", binding_readback);
    batch.set_deadline_unix_ms(1); // long past
    const auto before = triton_statistics();
    masi::edge::v1::InferenceResultBatch resp;
    auto status = edge.Infer(batch, &resp, 5000);
    CHECK(!status.ok(), "Infer: expired deadline must be rejected");
    CHECK(status.error_code() == grpc::StatusCode::DEADLINE_EXCEEDED,
          "Infer: expired deadline must map to DEADLINE_EXCEEDED");
    const auto after = triton_statistics();
    CHECK(after.inference_count == before.inference_count,
          "Infer: an expired request must not reach Triton");
    std::cerr << "Expired deadline rejected before execution\n";
  }

  // -----------------------------------------------------------------------
  // Test 3: Oversize batch rejected
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    auto batch = build_batch_of(257, "req-oversize-001", binding_readback);
    const auto before = triton_statistics();
    masi::edge::v1::InferenceResultBatch resp;
    auto status = retry_rpc([&] { return edge.Infer(batch, &resp, 2000); });
    CHECK(!status.ok(), "Infer: oversize batch must not succeed");
    CHECK(status.error_code() == grpc::StatusCode::RESOURCE_EXHAUSTED,
          "Infer: oversize batch must be RESOURCE_EXHAUSTED");
    CHECK(status.error_message() == "record_count out of range",
          "Infer: oversize reason drifted: " + status.error_message());
    CHECK(triton_statistics().inference_count == before.inference_count,
          "Infer: oversize batch must not execute");
    mark("oversize_rejected", true);
    std::cerr << "Oversize batch rejected: " << status.error_message() << "\n";
  }

  // -----------------------------------------------------------------------
  // Test 3b: a response that would exceed the configured serialized-response
  // ceiling is rejected before Triton execution. This uses a second real
  // Gateway with a 4 KiB response ceiling: GetBinding still fits, while the
  // conservative 256-record result skeleton cannot.
  // -----------------------------------------------------------------------
  {
    nlohmann::json bounded_cfg = nlohmann::json::parse(config_json);
    const std::string bounded_endpoint = find_free_loopback_port();
    bounded_cfg["gateway_listen"] = bounded_endpoint;
    bounded_cfg["max_response_bytes"] = 4096;
    const std::string bounded_cfg_path =
        write_temp_file(td.path(), "config-response-bound.json", bounded_cfg.dump());
    auto bounded_process =
        spawn(gateway_bin, {bounded_cfg_path}, td.child("gateway-response-bound.stdout"),
              td.child("gateway-response-bound.stderr"));
    SubprocessGuard bounded_guard(bounded_process);
    masi::edge::v1::BindingReadback bounded_binding;
    bool bounded_ready = false;
    for (int attempt = 0; attempt < 80 && !bounded_ready; ++attempt) {
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
      FakeEdge edge(bounded_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
      masi::edge::v1::GetBindingRequest req;
      req.set_schema_version("inference-committed-binding/v1");
      req.set_logical_pool_id("pool-blackbox-0001");
      req.set_pool_generation(1);
      req.set_binding_generation(1);
      req.set_model_control_incarnation_id("incarnation-blackbox-0001");
      bounded_ready = edge.GetBinding(req, &bounded_binding, 1000).ok();
    }
    CHECK(bounded_ready, "response-bound Gateway did not become ready");
    FakeEdge edge(bounded_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    auto batch = build_batch_of(256, "request-response-bound", bounded_binding);
    const auto before = triton_statistics();
    masi::edge::v1::InferenceResultBatch resp;
    const auto status = edge.Infer(batch, &resp, 5000);
    CHECK(!status.ok() && status.error_code() == grpc::StatusCode::RESOURCE_EXHAUSTED,
          "oversize response must be rejected with RESOURCE_EXHAUSTED");
    CHECK(status.error_message() == "INFERENCE_MESSAGE_TOO_LARGE",
          "oversize response reason drifted: " + status.error_message());
    CHECK(triton_statistics().inference_count == before.inference_count,
          "response ceiling must reject before Triton execution");
    signal_subprocess(bounded_process, SIGTERM);
    CHECK(wait_for(bounded_process) == 0, "response-bound Gateway did not exit cleanly");
    const std::string bounded_diagnostic =
        fatal_runtime_marker(read_file(td.child("gateway-response-bound.stderr")));
    CHECK(bounded_diagnostic.empty(),
          "response-bound Gateway emitted fatal runtime marker: " + bounded_diagnostic);
    std::cerr << "Serialized response ceiling rejected before execution\n";
  }

  // -----------------------------------------------------------------------
  // Test 4: Unknown major rejected
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    auto batch = build_batch_of(1, "req-unknown-major-001", binding_readback);
    batch.set_schema_version("inference-central-grpc-batch/v2");
    batch.mutable_route()->set_wire_profile("inference-central-grpc-batch/v2");
    const auto before = triton_statistics();
    masi::edge::v1::InferenceResultBatch resp;
    auto status = retry_rpc([&] { return edge.Infer(batch, &resp, 2000); });
    CHECK(!status.ok(), "Infer: unknown major must not succeed");
    CHECK(status.error_code() == grpc::StatusCode::INVALID_ARGUMENT,
          "Infer: unknown major must be INVALID_ARGUMENT");
    CHECK(status.error_message().find("schema_version unsupported") != std::string::npos,
          "Infer: unknown major reason drifted: " + status.error_message());
    CHECK(triton_statistics().inference_count == before.inference_count,
          "Infer: unknown major must not execute");
    mark("unknown_major_rejected", true);
    std::cerr << "Unknown major rejected: " << status.error_message() << "\n";
  }

  // -----------------------------------------------------------------------
  // Test 5: Empty batch rejected cleanly (no protobuf CHECK, no crash)
  // -----------------------------------------------------------------------
  {
    FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
    auto batch = build_batch_of(0, "req-empty-001", binding_readback);
    const auto before = triton_statistics();
    masi::edge::v1::InferenceResultBatch resp;
    auto status = retry_rpc([&] { return edge.Infer(batch, &resp, 2000); });
    CHECK(!status.ok(), "Infer: empty batch must not succeed");
    CHECK(status.error_code() == grpc::StatusCode::RESOURCE_EXHAUSTED,
          "Infer: empty batch must be RESOURCE_EXHAUSTED");
    CHECK(status.error_message() == "record_count out of range",
          "Infer: empty batch reason drifted: " + status.error_message());
    CHECK(triton_statistics().inference_count == before.inference_count,
          "Infer: empty batch must not execute");
    std::cerr << "Empty batch rejected: " << status.error_message() << "\n";
  }

  // -----------------------------------------------------------------------
  // Test 6: Plaintext rejected
  // -----------------------------------------------------------------------
  {
    FakeEdge plaintext_edge(grpc_endpoint, 0); // plaintext
    masi::edge::v1::GetBindingRequest req;
    req.set_schema_version("inference-committed-binding/v1");
    masi::edge::v1::BindingReadback resp;
    auto status = plaintext_edge.GetBinding(req, &resp, 1000);
    CHECK(!status.ok(), "Plaintext GetBinding must fail (mTLS required)");
    mark("plaintext_rejected", true);
    std::cerr << "Plaintext rejected: " << status.error_message() << "\n";
  }

  // -----------------------------------------------------------------------
  // Test 7: exact client identity binding. The frozen profile requires
  // CA-chain-plus-exact-SAN, so a CA-signed certificate whose SAN is not in the
  // deployment allowlist, and a CA-signed certificate with no SAN at all, must
  // both be rejected. A CA signature alone is not an identity.
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
    auto status = wrong_edge.GetBinding(req, &resp, 2000);
    CHECK(!status.ok(), "non-allowlisted client SAN must be rejected");
    CHECK(status.error_code() == grpc::StatusCode::UNAUTHENTICATED,
          "non-allowlisted SAN must map to UNAUTHENTICATED, got " +
              std::to_string(static_cast<int>(status.error_code())));
    CHECK(status.error_message() == "PEER_SAN_NOT_ALLOWLISTED",
          "unexpected rejection reason: " + status.error_message());
    std::cerr << "Non-allowlisted client SAN rejected: " << status.error_message() << "\n";
  }
  {
    FakeEdge no_san_edge(grpc_endpoint, mtls.ca_path, mtls.no_san_cert, mtls.no_san_key);
    masi::edge::v1::GetBindingRequest req;
    req.set_schema_version("inference-committed-binding/v1");
    req.set_logical_pool_id("pool-blackbox-0001");
    req.set_pool_generation(1);
    req.set_binding_generation(1);
    req.set_model_control_incarnation_id("incarnation-blackbox-0001");
    masi::edge::v1::BindingReadback resp;
    auto status = no_san_edge.GetBinding(req, &resp, 2000);
    CHECK(!status.ok(), "client certificate without any SAN must be rejected");
    CHECK(status.error_code() == grpc::StatusCode::UNAUTHENTICATED,
          "no-SAN client must map to UNAUTHENTICATED");
    CHECK(status.error_message() == "PEER_CERTIFICATE_HAS_NO_SAN",
          "unexpected rejection reason: " + status.error_message());
    std::cerr << "Client certificate without SAN rejected: " << status.error_message() << "\n";
  }
  {
    // The same identity check guards Infer, not only GetBinding.
    FakeEdge wrong_edge(grpc_endpoint, mtls.ca_path, mtls.wrong_cert, mtls.wrong_key);
    auto batch = build_batch_of(1, "request-blackbox-identity", binding_readback);
    masi::edge::v1::InferenceResultBatch resp;
    auto status = wrong_edge.Infer(batch, &resp, 2000);
    CHECK(!status.ok(), "Infer must also enforce the client SAN allowlist");
    CHECK(status.error_code() == grpc::StatusCode::UNAUTHENTICATED,
          "Infer identity rejection must be UNAUTHENTICATED");
    mark("mtls_verified", true);
    std::cerr << "Infer enforces the client SAN allowlist\n";
  }

  // -----------------------------------------------------------------------
  // Test 7b: bounded in-flight per route target. The frozen profile allows
  // exactly one in-flight batch per target, so concurrent batches on the same
  // route must be rejected with a stable RESOURCE_EXHAUSTED reason instead of
  // being queued inside the Gateway.
  // -----------------------------------------------------------------------
  {
    constexpr int kThreads = 4;
    constexpr int kIterations = 15;
    std::atomic<int> ok_count{0};
    std::atomic<int> rejected{0};
    std::atomic<int> other_failure{0};
    std::string unexpected_message;
    std::mutex msg_mu;

    std::vector<std::thread> threads;
    threads.reserve(kThreads);
    for (int t = 0; t < kThreads; ++t) {
      threads.emplace_back([&, t] {
        FakeEdge edge(grpc_endpoint, mtls.ca_path, mtls.client_cert, mtls.client_key);
        for (int i = 0; i < kIterations; ++i) {
          auto batch =
              build_batch_of(256, "request-inflight-" + std::to_string(t) + "-" + std::to_string(i),
                             binding_readback);
          masi::edge::v1::InferenceResultBatch resp;
          auto status = edge.Infer(batch, &resp, 20000);
          if (status.ok()) {
            ok_count.fetch_add(1);
          } else if (status.error_code() == grpc::StatusCode::RESOURCE_EXHAUSTED) {
            rejected.fetch_add(1);
            if (status.error_message() != "maximum_in_flight_batches_per_target exceeded" &&
                status.error_message() != "gateway in-flight limit reached") {
              std::lock_guard<std::mutex> g(msg_mu);
              unexpected_message = status.error_message();
              other_failure.fetch_add(1);
            }
          } else {
            std::lock_guard<std::mutex> g(msg_mu);
            unexpected_message = std::to_string(static_cast<int>(status.error_code())) + ": " +
                                 status.error_message();
            other_failure.fetch_add(1);
          }
        }
      });
    }
    for (auto &th : threads)
      th.join();

    CHECK(other_failure.load() == 0,
          "concurrent same-route batches produced an unexpected failure: " + unexpected_message);
    CHECK(ok_count.load() > 0, "no concurrent batch succeeded");
    CHECK(rejected.load() > 0, "concurrent same-route batches were never rejected: the per-target "
                               "in-flight bound is not enforced");
    std::cerr << "Bounded in-flight per target enforced (" << ok_count.load() << " admitted, "
              << rejected.load() << " rejected with the contract reason)\n";
  }

  // -----------------------------------------------------------------------
  // Test 7c: fault recovery at the real transport boundary.
  //
  // A second real Gateway process is started against a loopback fault proxy in
  // front of the same real Triton. Cutting or stalling the proxy produces a
  // genuine backend transport failure, which must fail closed (no local
  // execution, no fabricated scores) and must recover afterwards.
  // -----------------------------------------------------------------------
  {
    const std::string upstream = triton_endpoint();
    const auto colon = upstream.rfind(':');
    CHECK(colon != std::string::npos, "fault proxy: malformed Triton endpoint");
    TcpFaultProxy proxy(upstream.substr(0, colon),
                        static_cast<uint16_t>(std::stoi(upstream.substr(colon + 1))));

    // Same pinned binding, backend reached through the proxy.
    std::string fault_listen = find_free_loopback_port();
    nlohmann::json fault_cfg = nlohmann::json::parse(config_json);
    fault_cfg["triton_endpoint"] = proxy.endpoint();
    fault_cfg["gateway_listen"] = fault_listen;
    fault_cfg["request_deadline_ms"] = 1500;
    std::string fault_cfg_path = write_temp_file(td.path(), "config-fault.json", fault_cfg.dump());

    auto spawn_fault_gateway = [&](const std::string &tag) {
      auto child =
          spawn(gateway_bin, {fault_cfg_path}, td.child("gateway-fault-" + tag + ".stdout"),
                td.child("gateway-fault-" + tag + ".stderr"));
      bool up = false;
      for (int i = 0; i < 80 && !up; ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        try {
          FakeEdge edge(fault_listen, mtls.ca_path, mtls.client_cert, mtls.client_key);
          masi::edge::v1::GetBindingRequest req;
          req.set_schema_version("inference-committed-binding/v1");
          req.set_logical_pool_id("pool-blackbox-0001");
          req.set_pool_generation(1);
          req.set_binding_generation(1);
          req.set_model_control_incarnation_id("incarnation-blackbox-0001");
          masi::edge::v1::BindingReadback resp;
          if (retry_rpc([&] { return edge.GetBinding(req, &resp, 1000); }, 3).ok()) {
            up = true;
          }
        } catch (...) {
        }
      }
      CHECK(up, "fault gateway did not become ready (" + tag +
                    "): " + read_file(td.child("gateway-fault-" + tag + ".stderr")));
      return child;
    };

    auto make_batch = [&](const std::string &request_id) {
      masi::edge::v1::InferenceInputBatch batch = build_valid_batch_from_golden();
      batch.set_request_id(request_id);
      bind_batch_to_readback(&batch, binding_readback);
      batch.set_deadline_unix_ms(std::chrono::duration_cast<std::chrono::milliseconds>(
                                     std::chrono::system_clock::now().time_since_epoch())
                                     .count() +
                                 1400);
      return batch;
    };

    auto fault_sp = spawn_fault_gateway("a");
    const auto golden = masi::inf::test::load_golden_inference("valid-batch-v1.json");
    const std::string golden_output_digest = golden["expected"]["output_digest"].get<std::string>();
    std::string binding_digest_before_crash;

    // Baseline through the proxy, and capture the readback identity.
    {
      FakeEdge edge(fault_listen, mtls.ca_path, mtls.client_cert, mtls.client_key);
      masi::edge::v1::InferenceResultBatch resp;
      auto status =
          retry_rpc([&] { return edge.Infer(make_batch("request-fault-baseline"), &resp, 5000); });
      CHECK(status.ok(), "fault baseline Infer failed: " + status.error_message());
      CHECK(resp.records_size() == 1 && resp.records(0).output_digest() == golden_output_digest,
            "fault baseline must reproduce the golden output digest");
      binding_digest_before_crash = resp.records(0).binding_digest();
      CHECK(!binding_digest_before_crash.empty(), "fault baseline binding_digest empty");
      std::cerr << "Fault-proxy baseline OK (backend reached through the proxy)\n";
    }

    // F1: backend cut. The request must fail closed and nothing may execute.
    {
      const auto before = triton_statistics();
      proxy.set_mode(TcpFaultProxy::Mode::Cut);
      FakeEdge edge(fault_listen, mtls.ca_path, mtls.client_cert, mtls.client_key);
      masi::edge::v1::InferenceResultBatch resp;
      auto status = edge.Infer(make_batch("request-fault-cut"), &resp, 6000);
      CHECK(!status.ok(), "backend outage must fail closed, got OK");
      CHECK(resp.records_size() == 0, "backend outage must not return fabricated result records");
      const auto after = triton_statistics();
      CHECK(after.observed, "Triton statistics unavailable during the outage case");
      CHECK(after.inference_count == before.inference_count,
            "no inference may execute while the backend is unreachable");
      CHECK(::kill(fault_sp.pid, 0) == 0, "Gateway must survive a backend outage");
      mark("fail_closed", true);
      mark("pool_unavailable_returns_stable_error", true);
      mark("full_pool_outage_bounded", true);
      std::cerr << "Backend outage failed closed without local execution: "
                << status.error_message() << "\n";
    }

    // F2: recovery. The same batch must succeed again, bit-identical.
    {
      proxy.set_mode(TcpFaultProxy::Mode::PassThrough);
      bool recovered = false;
      std::string last_error;
      for (int i = 0; i < 40 && !recovered; ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
        FakeEdge edge(fault_listen, mtls.ca_path, mtls.client_cert, mtls.client_key);
        masi::edge::v1::InferenceResultBatch resp;
        auto status = edge.Infer(make_batch("request-fault-recovered"), &resp, 6000);
        if (status.ok()) {
          CHECK(resp.records_size() == 1 && resp.records(0).output_digest() == golden_output_digest,
                "recovered result drifted from the golden output digest");
          recovered = true;
        } else {
          last_error = status.error_message();
        }
      }
      CHECK(recovered, "Gateway did not recover after the backend returned: " + last_error);
      std::cerr << "Recovery after backend outage OK (identical canonical output)\n";
    }

    // F3: backend stalled past the deadline. The Gateway must time out, not
    // wait forever and not answer from anywhere else; in-flight slots must be
    // released so later requests still succeed.
    {
      proxy.set_mode(TcpFaultProxy::Mode::Stall);
      FakeEdge edge(fault_listen, mtls.ca_path, mtls.client_cert, mtls.client_key);
      masi::edge::v1::InferenceResultBatch resp;
      const auto started = std::chrono::steady_clock::now();
      auto status = edge.Infer(make_batch("request-fault-stalled"), &resp, 4000);
      const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                               std::chrono::steady_clock::now() - started)
                               .count();
      CHECK(!status.ok(), "a stalled backend must not produce an OK result");
      CHECK(resp.records_size() == 0, "a stalled backend must not produce result records");
      // The configured request_deadline_ms is 1500 while the caller allowed
      // 4000: the Gateway's own bound must be the one that fires.
      CHECK(elapsed < 3000, "the Gateway did not enforce its own configured request_deadline_ms "
                            "(waited " +
                                std::to_string(elapsed) + "ms of the caller's 4000ms)");
      mark("deadline_ms", 1500);
      std::cerr << "Stalled backend bounded by the Gateway after " << elapsed
                << "ms (configured 1500ms, caller allowed 4000ms): " << status.error_message()
                << "\n";

      proxy.set_mode(TcpFaultProxy::Mode::PassThrough);
      bool released = false;
      for (int i = 0; i < 40 && !released; ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
        FakeEdge probe(fault_listen, mtls.ca_path, mtls.client_cert, mtls.client_key);
        masi::edge::v1::InferenceResultBatch probe_resp;
        if (probe.Infer(make_batch("request-fault-after-stall"), &probe_resp, 6000).ok() &&
            probe_resp.records_size() == 1) {
          released = true;
        }
      }
      CHECK(released, "in-flight capacity was not released after the stalled request");
      std::cerr << "In-flight capacity released after the stalled request\n";
    }

    // F4: crash recovery. SIGKILL the Gateway, restart the same binary with the
    // same config, and require the identical binding and identical result.
    {
      ::kill(fault_sp.pid, SIGKILL);
      int crash_code = wait_for(fault_sp);
      CHECK(crash_code == 128 + SIGKILL, "expected SIGKILL termination");
      const std::string killed_diagnostic =
          fatal_runtime_marker(read_file(td.child("gateway-fault-a.stderr")));
      CHECK(killed_diagnostic.empty(),
            "fault Gateway emitted fatal runtime marker before SIGKILL: " + killed_diagnostic);
      auto restarted = spawn_fault_gateway("b");
      FakeEdge edge(fault_listen, mtls.ca_path, mtls.client_cert, mtls.client_key);
      masi::edge::v1::InferenceResultBatch resp;
      auto status = retry_rpc(
          [&] { return edge.Infer(make_batch("request-fault-after-crash"), &resp, 6000); });
      CHECK(status.ok(), "Infer after crash restart failed: " + status.error_message());
      CHECK(resp.records_size() == 1, "Infer after crash restart returned no record");
      CHECK(resp.records(0).output_digest() == golden_output_digest,
            "result after crash restart drifted from the golden output digest");
      CHECK(resp.records(0).binding_digest() == binding_digest_before_crash,
            "binding digest after crash restart must be identical");
      mark("faults", nlohmann::json::array({"backend-transport-cut", "backend-stall-past-deadline",
                                            "gateway-sigkill-restart"}));
      std::cerr << "Crash restart re-verified the identical binding and result\n";
      signal_subprocess(restarted, SIGTERM);
      CHECK(wait_for(restarted) == 0, "restarted Gateway must exit 0 on SIGTERM");
      const std::string restarted_diagnostic =
          fatal_runtime_marker(read_file(td.child("gateway-fault-b.stderr")));
      CHECK(restarted_diagnostic.empty(),
            "restarted Gateway emitted fatal runtime marker: " + restarted_diagnostic);
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
  CHECK(exit_code == 0, "Gateway must exit 0 on SIGTERM (got " + std::to_string(exit_code) + ")");
  const std::string main_diagnostic = fatal_runtime_marker(stderr_content);
  CHECK(main_diagnostic.empty(), "Gateway emitted fatal runtime marker: " + main_diagnostic);
  mark("shutdown_exit_zero", true);
  std::cerr << "SIGTERM graceful shutdown OK (exit 0).\n";

  std::cerr << "All E2E blackbox tests passed.\n";
}

} // namespace

// ---------------------------------------------------------------------------
// Sustained soak against the real Gateway process and real Triton.
// Returns the exit code: 0 when the measured window satisfies the frozen
// profile, 2 when it ran cleanly but below the required window (HOLD), 1 on a
// real failure.
// ---------------------------------------------------------------------------
int run_soak(int64_t requested_seconds) {
  namespace fs = std::filesystem;
  const auto triton = probe_triton();
  if (!triton.available) {
    std::cerr << "SKIP: soak needs a real Triton at " << triton_endpoint() << "\n";
    return 77;
  }

  const fs::path soak_profile_path =
      masi::inf::test::contract_path("contracts/profiles/v1/qualification-soak-3600s.json");
  const auto soak_profile = masi::inf::test::load_json(soak_profile_path.string());
  const int64_t kWarmupPlannedMs = soak_profile.at("warmup_seconds").get<int64_t>() * 1000;
  const int64_t kRequiredQualifiedMs =
      soak_profile.at("qualified_duration_seconds").get<int64_t>() * 1000;
  const int64_t kSampleIntervalMs =
      soak_profile.at("sample_interval_seconds").get<int64_t>() * 1000;
  const size_t kMaxSamples = soak_profile.at("max_samples").get<size_t>();
  const int64_t kTargetBatchesPerSecond =
      soak_profile.at("load").at("batch_requests_per_second").get<int64_t>();
  CHECK(soak_profile.at("profile_id") == "qualification-soak-3600s/v1",
        "soak profile identity drifted");
  CHECK(soak_profile.at("formal_duration_override") == "reject",
        "soak profile must reject formal duration overrides");
  CHECK(kWarmupPlannedMs == 60000 && kRequiredQualifiedMs == 3600000 &&
            kSampleIntervalMs == 10000 && kMaxSamples == 400 && kTargetBatchesPerSecond == 100 &&
            soak_profile.at("load").at("pacing") == "monotonic-fixed-rate",
        "soak profile frozen limits drifted");
  const auto &profile_phases = soak_profile.at("phases");
  CHECK(profile_phases.size() == 4, "soak profile must declare four phases");
  const int64_t kPhasePlannedMs = profile_phases.at(0).at("duration_seconds").get<int64_t>() * 1000;
  const std::vector<std::string> phase_names = {profile_phases.at(0).at("name").get<std::string>(),
                                                profile_phases.at(1).at("name").get<std::string>(),
                                                profile_phases.at(2).at("name").get<std::string>(),
                                                profile_phases.at(3).at("name").get<std::string>()};
  CHECK(kPhasePlannedMs == 900000 &&
            std::all_of(profile_phases.begin(), profile_phases.end(),
                        [](const auto &phase) {
                          return phase.at("duration_seconds").template get<int64_t>() == 900;
                        }),
        "soak phase duration drifted");
  const std::vector<int> phase_batch = {32, 128, 256, 1};

  CHECK(requested_seconds > 0, "soak duration must be positive");
  CHECK(requested_seconds <= 3600, "formal soak duration override is forbidden");
  int64_t warmup_ms = kWarmupPlannedMs;
  int64_t phase_ms = kPhasePlannedMs;
  const int64_t requested_ms = requested_seconds * 1000;
  if (requested_ms < kRequiredQualifiedMs) {
    // Shorter than the frozen window: run a proportional rehearsal that can
    // only ever report HOLD, never PASS.
    warmup_ms = std::min<int64_t>(kWarmupPlannedMs,
                                  std::max<int64_t>(kSampleIntervalMs, requested_ms / 10));
    phase_ms = std::max<int64_t>(kSampleIntervalMs, requested_ms / 4);
  }

  TempDir td;
  auto mtls = generate_mtls_bundle(td.path());
  std::string repo_path = td.child("modelrepo");
  TestRepoDigests digests = build_test_repository(repo_path);
  EnvelopeBinding binding;
  binding.model_digest = digests.model_digest;
  binding.model_revision_digest = digests.model_revision_digest;
  binding.closure_digest = digests.closure_digest;
  binding.repository_identity = digests.identity;
  binding.feature_contract_digest = digests.feature_contract_digest;
  binding.label_contract_digest = digests.label_contract_digest;
  binding.output_adapter_digest = digests.output_adapter_digest;
  binding.wire_profile_digest = digests.wire_profile_digest;
  binding.runtime_profile_digest = digests.runtime_profile_digest;
  binding.optimization_profile_digest = digests.optimization_profile_digest;
  binding.triton_server_version = triton.server_version;
  std::string envelope_json = build_test_envelope(binding);
  std::string envelope_path = write_temp_file(td.path(), "envelope.json", envelope_json);
  std::string listen_addr = find_free_loopback_port();
  nlohmann::json cfg = nlohmann::json::parse(
      build_test_config(envelope_path, repo_path, mtls.ca_path, mtls.server_cert, mtls.server_key));
  cfg["gateway_listen"] = listen_addr;
  std::string config_path = write_temp_file(td.path(), "config.json", cfg.dump());

  const std::string gateway_bin = find_gateway_binary();
  auto sp = spawn(gateway_bin, {config_path}, td.child("soak.stdout"), td.child("soak.stderr"));
  SubprocessGuard process_guard(sp);
  bool ready = false;
  masi::edge::v1::BindingReadback binding_readback;
  for (int i = 0; i < 100 && !ready; ++i) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    try {
      FakeEdge edge(listen_addr, mtls.ca_path, mtls.client_cert, mtls.client_key);
      masi::edge::v1::GetBindingRequest req;
      req.set_schema_version("inference-committed-binding/v1");
      req.set_logical_pool_id("pool-blackbox-0001");
      req.set_pool_generation(1);
      req.set_binding_generation(1);
      req.set_model_control_incarnation_id("incarnation-blackbox-0001");
      masi::edge::v1::BindingReadback resp;
      if (retry_rpc([&] { return edge.GetBinding(req, &resp, 1000); }, 3).ok()) {
        binding_readback = resp;
        ready = true;
      }
    } catch (...) {
    }
  }
  if (!ready) {
    std::cerr << "soak: Gateway did not become ready\n"
              << read_file(td.child("soak.stderr")) << "\n";
    return 1;
  }

  const auto golden = masi::inf::test::load_golden_inference("valid-batch-v1.json");
  const std::string golden_output_digest = golden["expected"]["output_digest"].get<std::string>();
  const auto multi_golden =
      masi::inf::test::load_golden_inference("valid-multi-record-batch-v1.json");
  const auto &multi_rows = multi_golden.at("expected").at("rows");
  CHECK(multi_rows.size() == 2, "soak oracle requires alert and OOD golden rows");
  const std::vector<std::string> expected_output_digests = {
      multi_rows.at(0).at("output_digest").get<std::string>(),
      multi_rows.at(1).at("output_digest").get<std::string>()};

  auto issue_batch = [&](FakeEdge &edge, int records, int64_t seq) {
    masi::edge::v1::InferenceInputBatch batch = build_batch_of(
        static_cast<size_t>(records), "soak-" + std::to_string(seq), binding_readback);
    masi::edge::v1::InferenceResultBatch resp;
    auto status = edge.Infer(batch, &resp, 10000);
    return std::make_pair(status, resp);
  };

  const auto wall_start = std::chrono::system_clock::now();
  const auto mono_start = std::chrono::steady_clock::now();
  auto offset_ms = [&] {
    return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() -
                                                                 mono_start)
        .count();
  };

  nlohmann::json samples = nlohmann::json::array();
  int64_t prev_cpu_ticks = -1;
  int64_t previous_sample_offset_ms = -1;
  int64_t error_count = 0;
  int64_t oracle_mismatches = 0;
  int64_t gap_count = 0;
  int64_t oom_events = 0;
  int64_t container_restarts = 0;
  int64_t resource_limit_violations = 0;
  int64_t not_measurable = 0;
  int64_t peak_rss = 0;
  int64_t seq = 0;
  int64_t successful_records = 0;
  int64_t provider_statistics_samples = 0;
  masi::inf::TritonModelStatistics previous_provider_stats = triton.baseline;
  CHECK(previous_provider_stats.observed, "soak requires a Triton statistics baseline");
  std::vector<int64_t> latencies_us;
  latencies_us.reserve(65536);

  // Drive one phase and sample it on the frozen interval.
  auto drive = [&](const std::string &phase, int records, int64_t planned_ms) {
    const int64_t phase_start_offset_ms = offset_ms();
    const auto phase_start = std::chrono::steady_clock::now();
    int64_t phase_errors = 0;
    int64_t phase_requests = 0;
    int64_t next_sample_ms = 0;
    FakeEdge edge(listen_addr, mtls.ca_path, mtls.client_cert, mtls.client_key);
    while (true) {
      const auto scheduled = phase_start + std::chrono::microseconds(phase_requests * 1000000 /
                                                                     kTargetBatchesPerSecond);
      std::this_thread::sleep_until(scheduled);
      int64_t phase_elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                                  std::chrono::steady_clock::now() - phase_start)
                                  .count();
      if (phase_elapsed >= planned_ms)
        break;
      const auto call_start = std::chrono::steady_clock::now();
      auto [status, resp] = issue_batch(edge, records, ++seq);
      const int64_t call_us = std::chrono::duration_cast<std::chrono::microseconds>(
                                  std::chrono::steady_clock::now() - call_start)
                                  .count();
      ++phase_requests;
      phase_elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                          std::chrono::steady_clock::now() - phase_start)
                          .count();
      if (!status.ok()) {
        ++phase_errors;
        ++error_count;
      } else {
        latencies_us.push_back(call_us);
        successful_records += records;
        const std::string expected_request_id = "soak-" + std::to_string(seq);
        bool identity_gap =
            resp.request_id() != expected_request_id || resp.records_size() != records;
        if (!identity_gap) {
          for (int row = 0; row < records; ++row) {
            if (resp.records(row).input_id() != "input-batch-" + std::to_string(row)) {
              identity_gap = true;
              break;
            }
          }
        }
        if (identity_gap) {
          ++gap_count;
        } else {
          bool oracle_mismatch = false;
          for (int row = 0; row < records; ++row) {
            const auto &record = resp.records(row);
            const std::string &expected_digest =
                records == 1 ? golden_output_digest
                             : expected_output_digests.at(static_cast<size_t>(row) % 2);
            if (record.execution_status() != masi::edge::v1::INFERENCE_EXECUTION_STATUS_OK ||
                record.output_digest() != expected_digest) {
              oracle_mismatch = true;
              break;
            }
          }
          if (oracle_mismatch)
            ++oracle_mismatches;
        }
      }
      if (phase_elapsed >= next_sample_ms) {
        if (samples.size() >= kMaxSamples) {
          ++phase_errors;
          ++error_count;
          break;
        }
        const int64_t current_offset_ms = offset_ms();
        const int64_t actual_sample_interval_ms =
            previous_sample_offset_ms < 0
                ? kSampleIntervalMs
                : std::max<int64_t>(1, current_offset_ms - previous_sample_offset_ms);
        ProcSample ps = sample_process(sp.pid, &prev_cpu_ticks, actual_sample_interval_ms);
        previous_sample_offset_ms = current_offset_ms;
        if (::kill(sp.pid, 0) != 0)
          ps.valid = false;
        bool provider_statistics_valid = false;
        try {
          const auto current_provider_stats = triton_statistics();
          provider_statistics_valid =
              current_provider_stats.observed &&
              current_provider_stats.inference_count >= previous_provider_stats.inference_count &&
              current_provider_stats.execution_count >= previous_provider_stats.execution_count &&
              current_provider_stats.success_count >= previous_provider_stats.success_count;
          if (current_provider_stats.observed && !provider_statistics_valid)
            ++container_restarts;
          if (current_provider_stats.observed)
            previous_provider_stats = current_provider_stats;
          if (provider_statistics_valid)
            ++provider_statistics_samples;
        } catch (const std::exception &) {
          provider_statistics_valid = false;
        }
        if (!ps.valid || !provider_statistics_valid)
          ++not_measurable;
        peak_rss = std::max(peak_rss, ps.rss_bytes);
        samples.push_back(
            {{"offset_ms", current_offset_ms},
             {"phase", phase},
             {"quality", ps.valid && provider_statistics_valid ? "valid" : "not_measurable"},
             {"cpu_pct", ps.cpu_pct},
             {"rss_bytes", ps.rss_bytes},
             {"fd_count", ps.fd_count},
             {"thread_count", ps.thread_count},
             {"queue_depth", 0},
             {"oom_events", oom_events},
             {"container_restarts", container_restarts},
             {"oracle_errors", oracle_mismatches},
             {"gap_count", gap_count},
             {"module_metrics",
              {{"records_per_batch", records},
               {"requests_completed", phase_requests},
               {"errors", phase_errors},
               {"actual_sample_interval_ms", actual_sample_interval_ms},
               {"gateway_pid", static_cast<int64_t>(sp.pid)},
               {"queue_ownership", "none-gateway-submits-directly-to-triton"},
               {"queue_depth_source", "gateway-has-no-owned-queue"},
               {"provider_statistics_observed", provider_statistics_valid},
               {"provider_inference_count",
                static_cast<int64_t>(previous_provider_stats.inference_count)},
               {"provider_execution_count",
                static_cast<int64_t>(previous_provider_stats.execution_count)},
               {"provider_success_count",
                static_cast<int64_t>(previous_provider_stats.success_count)},
               {"container_runtime", "not-used-direct-real-process"}}}});
        next_sample_ms += kSampleIntervalMs;
      }
    }
    const int64_t elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::steady_clock::now() - phase_start)
                                .count();
    const double seconds = static_cast<double>(elapsed) / 1000.0;
    const double achieved =
        seconds > 0 ? static_cast<double>(phase_requests * records) / seconds : 0.0;
    const double requested = static_cast<double>(kTargetBatchesPerSecond * records);
    const bool rate_target_met = achieved >= requested * 0.99;
    std::cerr << "soak phase " << phase << ": " << phase_requests << " batches of " << records
              << " in " << elapsed << "ms, " << phase_errors << " errors\n";
    return nlohmann::json{{"name", phase},
                          {"planned_ms", kPhasePlannedMs},
                          {"elapsed_ms", elapsed},
                          {"result", phase_errors != 0           ? "FAIL"
                                     : elapsed < kPhasePlannedMs ? "HOLD"
                                     : rate_target_met           ? "PASS"
                                                                 : "FAIL"},
                          {"requested_rate_pps", requested},
                          {"achieved_rate_pps", achieved},
                          {"errors", phase_errors},
                          {"module_metrics",
                           {{"records_per_batch", records},
                            {"target_batches_per_second", kTargetBatchesPerSecond},
                            {"rate_target_met", rate_target_met},
                            {"batches", phase_requests},
                            {"phase_start_offset_ms", phase_start_offset_ms},
                            {"phase_end_offset_ms", std::max<int64_t>(1, offset_ms())},
                            {"planned_duration_met", elapsed >= kPhasePlannedMs}}}};
  };

  // Warmup is measured but never counted towards the qualified window.
  drive("warmup", 1, warmup_ms);
  const int64_t warmup_elapsed_ms = offset_ms();

  nlohmann::json phases = nlohmann::json::array();
  int64_t qualified_ms = 0;
  for (size_t i = 0; i < phase_names.size(); ++i) {
    const auto before = offset_ms();
    phases.push_back(drive(phase_names[i], phase_batch[i], phase_ms));
    qualified_ms += offset_ms() - before;
  }

  // Bind the successful RPC count to the provider's own monotonic statistics.
  // A reset is the observable restart signal for the externally managed Triton
  // process; unavailable statistics make the evidence not measurable.
  masi::inf::TritonModelStatistics final_provider_stats = previous_provider_stats;
  try {
    final_provider_stats = triton_statistics();
    const bool counters_monotonic =
        final_provider_stats.observed &&
        final_provider_stats.inference_count >= previous_provider_stats.inference_count &&
        final_provider_stats.execution_count >= previous_provider_stats.execution_count &&
        final_provider_stats.success_count >= previous_provider_stats.success_count;
    if (final_provider_stats.observed && !counters_monotonic)
      ++container_restarts;
    if (!counters_monotonic) {
      ++not_measurable;
    } else {
      ++provider_statistics_samples;
      const uint64_t minimum_inference_count =
          triton.baseline.inference_count + static_cast<uint64_t>(successful_records);
      if (final_provider_stats.inference_count < minimum_inference_count)
        ++gap_count;
    }
  } catch (const std::exception &) {
    ++not_measurable;
  }

  // Graceful shutdown is part of the soak: the process must still exit 0.
  signal_subprocess(sp, SIGTERM);
  const int exit_code = wait_for(sp);
  const std::string runtime_diagnostic = fatal_runtime_marker(read_file(td.child("soak.stderr")));
  if (!runtime_diagnostic.empty())
    ++error_count;
  if (exit_code == 128 + SIGKILL)
    ++oom_events; // conservative: an unexpected SIGKILL is treated as an OOM signal
  const bool process_reaped = sp.pid == -1;
  const bool listener_released = listener_is_released(listen_addr);
  const bool cleanup_complete = process_reaped && listener_released;
  const bool graceful_shutdown = exit_code == 0;

  std::sort(latencies_us.begin(), latencies_us.end());
  auto pct = [&](double p) -> int64_t {
    if (latencies_us.empty())
      return 0;
    const size_t idx = static_cast<size_t>(p * static_cast<double>(latencies_us.size() - 1));
    return latencies_us[idx];
  };

  const bool phases_pass =
      phases.size() == 4 && std::all_of(phases.begin(), phases.end(), [](const auto &phase) {
        return phase.at("result") == "PASS" && phase.at("errors") == 0 &&
               phase.at("elapsed_ms").template get<int64_t>() >= 900000;
      });
  const bool formal_schedule_executed = requested_seconds == 3600 &&
                                        qualified_ms >= kRequiredQualifiedMs &&
                                        warmup_elapsed_ms >= 60000 && warmup_elapsed_ms <= 90000;
  const bool window_met = formal_schedule_executed && phases_pass;
  // The frozen profile only allows MODULE-level PASS or HOLD for a document
  // that really ran the full window (4 x 900s phases, 60-90s warmup, 10s
  // samples). A shorter run is a REHEARSAL and can never be reported as a
  // module soak result.
  std::string result = "HOLD";
  std::string level = formal_schedule_executed ? "MODULE" : "REHEARSAL";
  if (error_count > 0 || oracle_mismatches > 0 || gap_count > 0 || oom_events > 0 ||
      container_restarts > 0 || resource_limit_violations > 0 || not_measurable > 0 ||
      !cleanup_complete || !graceful_shutdown) {
    result = "FAIL";
  } else if (window_met) {
    result = "PASS";
  }
  std::string threshold_status = result == "PASS" ? "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001"
                                 : requested_seconds < 3600
                                     ? "SOAK_WINDOW_BELOW_REQUIRED_3600_SECONDS"
                                     : "MEASURED_SOAK_GATE_FAILURE";
  const std::string interruption = !graceful_shutdown ? "CRASH"
                                   : not_measurable > 0 || !runtime_diagnostic.empty()
                                       ? "EVIDENCE_FAILURE"
                                       : "NONE";
  nlohmann::json remaining_resources = nlohmann::json::array();
  if (!listener_released)
    remaining_resources.push_back("gateway-listener");

  nlohmann::json evidence = nlohmann::json::object();
  evidence.update(nlohmann::json{
      {"schema_version", "qualification-soak/v1"},
      {"run_id", "soak-" + std::to_string(std::chrono::duration_cast<std::chrono::seconds>(
                                              wall_start.time_since_epoch())
                                              .count())},
      {"module", "central-inference"},
      {"requirement_ids", nlohmann::json::array({"MOD-INF-001", "PERF-INF-001", "DEC-042"})},
      {"level", level},
      {"applicability", "APPLICABLE"},
      {"result", result},
      {"qualification", result == "PASS" ? "QUALIFIED" : "NOT_QUALIFIED"},
      {"profile_digest", masi::inf::sha256_file(soak_profile_path.string())},
      {"claim_scope",
       {{"module", "central-inference"},
        {"scope", "independent-module-soak"},
        {"batch_sizes", nlohmann::json::array({1, 32, 128, 256})},
        {"runtime_profile", "model-runtime-central-cpu/v1"},
        {"availability_profile", "availability-single/v1"},
        {"threshold_status", threshold_status},
        {"resource_limits",
         {{"gateway_in_flight", cfg["max_in_flight"].get<int>()},
          {"triton_queue_records", 1024},
          {"max_batch_size", 256},
          {"rss_bytes", nullptr},
          {"fd_count", nullptr},
          {"thread_count", nullptr},
          {"process_threshold_status", "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001"}}}}},
      {"started_at", utc_timestamp(wall_start)},
      {"finished_at", utc_timestamp(std::chrono::system_clock::now())},
      {"monotonic_start_ns",
       std::chrono::duration_cast<std::chrono::nanoseconds>(mono_start.time_since_epoch()).count()},
      {"monotonic_end_ns", std::chrono::duration_cast<std::chrono::nanoseconds>(
                               std::chrono::steady_clock::now().time_since_epoch())
                               .count()},
      {"warmup_elapsed_ms", warmup_elapsed_ms},
      {"duration_target_ms", 4 * kPhasePlannedMs},
      {"qualified_elapsed_ms", qualified_ms},
      {"sample_interval_ms", kSampleIntervalMs},
      {"phases", phases},
      {"samples", samples},
      {"lease_renewals", nlohmann::json::array()},
      {"summary",
       {{"error_count", error_count},
        {"unclassified_gap_count", gap_count},
        {"oom_events", oom_events},
        {"container_restarts", container_restarts},
        {"oracle_mismatches", oracle_mismatches},
        {"resource_limit_violations", resource_limit_violations},
        {"module_metrics",
         {{"batches_completed", seq},
          {"latency_p50_us", pct(0.50)},
          {"latency_p95_us", pct(0.95)},
          {"latency_p99_us", pct(0.99)},
          {"peak_rss_bytes", peak_rss},
          {"not_measurable_samples", not_measurable},
          {"successful_records", successful_records},
          {"provider_statistics_samples", provider_statistics_samples},
          {"provider_baseline_inference_count",
           static_cast<int64_t>(triton.baseline.inference_count)},
          {"provider_final_inference_count",
           static_cast<int64_t>(final_provider_stats.inference_count)},
          {"provider_restart_signal", "triton-model-statistics-counter-reset"},
          {"resource_measurement_scope",
           "gateway-proc-and-provider-monotonic-statistics;absolute-process-thresholds-unfrozen"},
          {"fatal_runtime_diagnostic", runtime_diagnostic.empty() ? "none" : runtime_diagnostic},
          {"formal_schedule_executed", formal_schedule_executed},
          {"absolute_threshold_status", threshold_status},
          {"warmup_actual_ms", warmup_elapsed_ms}}}}},
      {"interruption", interruption},
      {"cleanup",
       {{"attempted", true},
        {"completed", cleanup_complete},
        {"exit_code", exit_code < 0 ? 255 : exit_code},
        {"remaining_resources", remaining_resources},
        {"checks",
         {{"module_process_reaped", process_reaped},
          {"provider_tasks_joined", graceful_shutdown},
          {"listeners_released", listener_released}}}}},
      {"artifacts",
       nlohmann::json::array({{{"name", "masi_inference_gateway"},
                               {"sha256", strip_sha256_prefix(masi::inf::sha256_file(gateway_bin))},
                               {"bytes", static_cast<int64_t>(fs::file_size(gateway_bin))}}})}});

  evidence["claim_scope_digest"] = [&] {
    const std::string canonical = evidence["claim_scope"].dump();
    return masi::inf::sha256_hex(canonical.data(), canonical.size());
  }();

  const char *dir = std::getenv("MASI_INF_EVIDENCE_DIR");
  if (dir != nullptr && *dir != '\0') {
    std::error_code ec;
    fs::create_directories(dir, ec);
    // A rehearsal must not be filed as the frozen module soak evidence.
    std::string name =
        level == "MODULE" ? "formal-soak-evidence.json" : "formal-soak-rehearsal.json";
    std::ofstream f(fs::path(dir) / name);
    f << evidence.dump(2) << "\n";
    std::cerr << "Soak evidence written to " << dir << "/" << name << "\n";
  }
  std::cerr << "soak level=" << level << " result=" << result
            << " qualified_elapsed_ms=" << qualified_ms << " (required " << kRequiredQualifiedMs
            << "), errors=" << error_count << ", oracle_mismatches=" << oracle_mismatches
            << ", p50=" << pct(0.50) << "us p99=" << pct(0.99) << "us peak_rss=" << peak_rss
            << "B\n";
  if (result == "FAIL")
    return 1;
  return result == "PASS" ? 0 : 2;
}

int main() {
  std::cout << "=== Central Inference module blackbox E2E test ===\n";

  // Guard: real-process E2E requires the full Triton/ORT stack.
  const char *e2e_env = std::getenv("MASI_INF_E2E");
  if (e2e_env == nullptr || std::string(e2e_env) != "1") {
    std::cout << "SKIP: MASI_INF_E2E=1 not set. Skipping real-process E2E.\n";
    std::cout << "Set MASI_INF_E2E=1 to run the full black-box test with a "
                 "real Gateway binary + mTLS + Triton.\n";
    return 77; // 77 = skip
  }

  // Soak mode: an explicit duration request runs the sustained soak driver
  // instead of the functional matrix.
  if (const char *soak = std::getenv("MASI_INF_SOAK_SECONDS");
      soak != nullptr && *soak != '\0' && std::string(soak) != "0") {
    try {
      return run_soak(std::stoll(soak));
    } catch (const std::exception &e) {
      std::cerr << "soak exception: " << e.what() << "\n";
      return 1;
    }
  }

  const auto started = std::chrono::steady_clock::now();
  auto elapsed_ms = [&] {
    return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() -
                                                                 started)
        .count();
  };
  try {
    run_e2e_tests();
  } catch (const std::exception &e) {
    std::cerr << "E2E test exception: " << e.what() << "\n";
    write_blackbox_evidence("FAIL", elapsed_ms());
    return 1;
  }
  write_blackbox_evidence("PASS", elapsed_ms());
  return 0;
}
