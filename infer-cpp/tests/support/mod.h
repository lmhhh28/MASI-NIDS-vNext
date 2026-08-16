// Central Inference test support header.
//
// Shared utilities for the contract_golden, property_invariants, and
// module_blackbox tests:
//   - Minimal CHECK(cond, msg) macro (no Google Test dependency).
//   - Temporary file/directory helpers with cleanup.
//   - mTLS certificate generation via openssl(1) subprocess.
//   - Golden JSON loading from contracts/golden/inference/*.json.
//   - Contract-consistent fake Edge gRPC client (header-only inline).
//
// All paths are resolved relative to the repository root so the tests can run
// from the infer-cpp build directory without environment-specific setup.

#pragma once

#include <grpcpp/grpcpp.h>

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <memory>
#include <nlohmann/json.hpp>
#include <sstream>
#include <string>
#include <vector>

#include "edge/v1/edge.grpc.pb.h"
#include "edge/v1/edge.pb.h"
#include "inference/v1/inference.grpc.pb.h"

// ---------------------------------------------------------------------------
// Minimal CHECK macro
// ---------------------------------------------------------------------------
#define CHECK(cond, msg)                                                       \
  do {                                                                         \
    if (!(cond)) {                                                             \
      std::cerr << "FAIL: " << (msg) << " at " << __FILE__ << ":" << __LINE__   \
                << std::endl;                                                  \
      std::exit(1);                                                            \
    }                                                                          \
  } while (0)

namespace masi::inf::test {

// ---------------------------------------------------------------------------
// Repository root resolution
// ---------------------------------------------------------------------------
// Tests run from the infer-cpp build directory. The contracts live at
// <repo_root>/contracts. We resolve the repo root by walking up from the
// current working directory until we find contracts/inference/v1/profile.json.
inline std::string repo_root() {
  const char* env = std::getenv("MASI_REPO_ROOT");
  if (env && env[0] != '\0') return std::string(env);
  // Walk up from CWD looking for contracts/inference/v1/profile.json.
  namespace fs = std::filesystem;
  fs::path cwd = fs::current_path();
  for (fs::path p = cwd; p.has_parent_path(); p = p.parent_path()) {
    if (fs::exists(p / "contracts" / "inference" / "v1" / "profile.json")) {
      return p.string();
    }
    if (p == p.parent_path()) break;
  }
  // Fall back to the infer-cpp source directory layout: infer-cpp/../
  fs::path infer_cpp = cwd;
  if (fs::exists(infer_cpp / "CMakeLists.txt") &&
      fs::exists(infer_cpp / "src" / "gateway.cc")) {
    return infer_cpp.parent_path().string();
  }
  // Common case: build/cpu-release/ -> ../../
  if (fs::exists(cwd / ".." / ".." / "CMakeLists.txt") &&
      fs::exists(cwd / ".." / ".." / "src" / "gateway.cc")) {
    return cwd / ".." / ".." / "..";
  }
  std::cerr << "test support: cannot locate repo root from CWD="
            << cwd.string() << "\n";
  std::exit(1);
}

inline std::string contract_path(const std::string& relative) {
  return repo_root() + "/" + relative;
}

inline std::string golden_inference_path(const std::string& name) {
  return repo_root() + "/contracts/golden/inference/" + name;
}

inline std::string golden_evidence_path(const std::string& name) {
  return repo_root() + "/contracts/golden/evidence/" + name;
}

// ---------------------------------------------------------------------------
// Golden JSON loading
// ---------------------------------------------------------------------------
inline nlohmann::json load_json(const std::string& path) {
  std::ifstream f(path);
  CHECK(f.good(), "load_json: cannot open " + path);
  nlohmann::json j;
  try {
    f >> j;
  } catch (const std::exception& e) {
    CHECK(false, std::string("load_json: parse failed: ") + e.what() + " in " + path);
  }
  return j;
}

// Load a golden vector file from contracts/golden/inference/<name>.
inline nlohmann::json load_golden_inference(const std::string& name) {
  return load_json(golden_inference_path(name));
}

inline nlohmann::json load_golden_evidence(const std::string& name) {
  return load_json(golden_evidence_path(name));
}

// ---------------------------------------------------------------------------
// Hex helpers
// ---------------------------------------------------------------------------
inline std::vector<uint8_t> hex_to_bytes(const std::string& hex) {
  std::vector<uint8_t> out;
  out.reserve(hex.size() / 2);
  auto nib = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'a' + 10;
    return -1;
  };
  for (size_t i = 0; i + 1 < hex.size(); i += 2) {
    int hi = nib(hex[i]);
    int lo = nib(hex[i + 1]);
    CHECK(hi >= 0 && lo >= 0, "hex_to_bytes: invalid hex char");
    out.push_back(static_cast<uint8_t>((hi << 4) | lo));
  }
  return out;
}

// ---------------------------------------------------------------------------
// Temporary directory / file helpers
// ---------------------------------------------------------------------------
class TempDir {
 public:
  TempDir() {
    namespace fs = std::filesystem;
    std::string tmpl = "/tmp/masi-inf-test-XXXXXX";
    std::vector<char> buf(tmpl.begin(), tmpl.end());
    buf.push_back('\0');
    char* d = mkdtemp(buf.data());
    CHECK(d != nullptr, "TempDir: mkdtemp failed");
    path_ = d;
  }
  ~TempDir() {
    if (!path_.empty()) {
      std::error_code ec;
      std::filesystem::remove_all(path_, ec);
    }
  }
  TempDir(const TempDir&) = delete;
  TempDir& operator=(const TempDir&) = delete;
  const std::string& path() const { return path_; }
  std::string child(const std::string& name) const { return path_ + "/" + name; }

 private:
  std::string path_;
};

inline std::string write_temp_file(const std::string& dir,
                                   const std::string& name,
                                   const std::string& content) {
  std::string p = dir + "/" + name;
  std::ofstream f(p);
  CHECK(f.good(), "write_temp_file: cannot open " + p);
  f << content;
  f.close();
  return p;
}

// ---------------------------------------------------------------------------
// mTLS certificate generation (via openssl(1))
// ---------------------------------------------------------------------------
struct MtlsBundle {
  std::string ca_path;        // CA certificate (PEM)
  std::string server_cert;    // server leaf cert (PEM)
  std::string server_key;     // server leaf key (PEM, 0600)
  std::string client_cert;    // client leaf cert (PEM)
  std::string client_key;     // client leaf key (PEM, 0600)
  std::string wrong_cert;     // wrong-identity client cert (PEM)
  std::string wrong_key;      // wrong-identity client key (PEM, 0600)
};

inline void run_openssl(const std::vector<std::string>& argv,
                        const std::string& what) {
  std::vector<const char*> args;
  args.push_back("openssl");
  for (const auto& a : argv) args.push_back(a.c_str());
  args.push_back(nullptr);
  int rc = std::system(nullptr);  // no-op to silence warning
  (void)rc;
  std::string cmd = "openssl";
  for (const auto& a : argv) {
    cmd += " ";
    cmd += a;
  }
  cmd += " >/dev/null 2>&1";
  int status = std::system(cmd.c_str());
  CHECK(status == 0, "run_openssl failed: " + what + " (cmd: " + cmd + ")");
}

inline MtlsBundle generate_mtls_bundle(const std::string& dir) {
  MtlsBundle b;
  std::string ca_key = dir + "/ca.key";
  b.ca_path = dir + "/ca.pem";
  run_openssl({"req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256",
               "-days", "2", "-subj", "/CN=MASI Inference test CA",
               "-addext", "basicConstraints=critical,CA:TRUE",
               "-addext", "keyUsage=critical,keyCertSign,cRLSign",
               "-keyout", ca_key, "-out", b.ca_path},
              "generate CA");

  // Server leaf with DNS:inference.test SAN.
  b.server_cert = dir + "/inference-server.pem";
  b.server_key = dir + "/inference-server.key";
  std::string server_csr = dir + "/inference-server.csr";
  run_openssl({"req", "-new", "-newkey", "rsa:3072", "-nodes", "-sha256",
               "-subj", "/CN=inference-server",
               "-addext", "extendedKeyUsage=serverAuth",
               "-addext", "subjectAltName=DNS:inference.test",
               "-keyout", b.server_key, "-out", server_csr},
              "generate server CSR");
  run_openssl({"x509", "-req", "-sha256", "-days", "2", "-set_serial", "201",
               "-in", server_csr, "-CA", b.ca_path, "-CAkey", ca_key,
               "-copy_extensions", "copyall", "-out", b.server_cert},
              "sign server cert");
  std::filesystem::permissions(b.server_key,
                               std::filesystem::perms::owner_read |
                                   std::filesystem::perms::owner_write,
                               std::filesystem::perm_options::replace);
  std::filesystem::remove(server_csr);

  // Edge client leaf (clientAuth).
  b.client_cert = dir + "/edge-client.pem";
  b.client_key = dir + "/edge-client.key";
  std::string client_csr = dir + "/edge-client.csr";
  run_openssl({"req", "-new", "-newkey", "rsa:3072", "-nodes", "-sha256",
               "-subj", "/CN=edge-client",
               "-addext", "extendedKeyUsage=clientAuth",
               "-keyout", b.client_key, "-out", client_csr},
              "generate client CSR");
  run_openssl({"x509", "-req", "-sha256", "-days", "2", "-set_serial", "202",
               "-in", client_csr, "-CA", b.ca_path, "-CAkey", ca_key,
               "-copy_extensions", "copyall", "-out", b.client_cert},
              "sign client cert");
  std::filesystem::permissions(b.client_key,
                               std::filesystem::perms::owner_read |
                                   std::filesystem::perms::owner_write,
                               std::filesystem::perm_options::replace);
  std::filesystem::remove(client_csr);

  // Wrong-identity client leaf (different CN, still signed by same CA so the
  // TLS handshake completes but the Gateway must reject it at the
  // application/identity layer if identity binding is enforced).
  b.wrong_cert = dir + "/wrong-client.pem";
  b.wrong_key = dir + "/wrong-client.key";
  std::string wrong_csr = dir + "/wrong-client.csr";
  run_openssl({"req", "-new", "-newkey", "rsa:3072", "-nodes", "-sha256",
               "-subj", "/CN=wrong-identity",
               "-addext", "extendedKeyUsage=clientAuth",
               "-keyout", b.wrong_key, "-out", wrong_csr},
              "generate wrong CSR");
  run_openssl({"x509", "-req", "-sha256", "-days", "2", "-set_serial", "203",
               "-in", wrong_csr, "-CA", b.ca_path, "-CAkey", ca_key,
               "-copy_extensions", "copyall", "-out", b.wrong_cert},
              "sign wrong cert");
  std::filesystem::permissions(b.wrong_key,
                               std::filesystem::perms::owner_read |
                                   std::filesystem::perms::owner_write,
                               std::filesystem::perm_options::replace);
  std::filesystem::remove(wrong_csr);

  std::filesystem::remove(ca_key);
  return b;
}

// ---------------------------------------------------------------------------
// Protobuf helpers for golden-digest tests
// ---------------------------------------------------------------------------
// Serialize a protobuf message to a deterministic byte string and compute
// sha256: over the bytes. We reuse the Gateway library's digest helper.
inline std::string sha256_hex_wrapper(const void* data, size_t len) {
  // Minimal SHA-256 implementation to avoid linking OpenSSL test deps when
  // the property test links against the gateway library. The gateway library
  // already exports masi::inf::sha256_hex; we delegate to it.
  extern std::string masi_inf_sha256_hex_proxy(const void*, size_t);
  return masi_inf_sha256_hex_proxy(data, len);
}

// ---------------------------------------------------------------------------
// Fake Edge gRPC client (contract-consistent)
// ---------------------------------------------------------------------------
// A minimal mTLS gRPC client that connects to the real Gateway and calls
// GetBinding / Infer. This is used by module_blackbox.cc to exercise the
// public CentralInference boundary without bringing up a second real Edge.
class FakeEdge {
 public:
  FakeEdge(const std::string& endpoint,
           const std::string& ca_path,
           const std::string& cert_path,
           const std::string& key_path) {
    grpc::SslCredentialsOptions opts;
    std::ifstream ca(ca_path);
    std::stringstream cas; cas << ca.rdbuf();
    opts.pem_root_certs = cas.str();
    std::ifstream ce(cert_path);
    std::stringstream ces; ces << ce.rdbuf();
    opts.pem_cert_chain = ces.str();
    std::ifstream kf(key_path);
    std::stringstream kfs; kfs << kf.rdbuf();
    opts.pem_private_key = kfs.str();
    CHECK(!opts.pem_root_certs.empty() && !opts.pem_cert_chain.empty() &&
              !opts.pem_private_key.empty(),
          "FakeEdge: empty PEM");
    auto creds = grpc::SslCredentials(opts);
    channel_ = grpc::CreateChannel(endpoint, creds);
    stub_ = masi::inference::v1::CentralInference::NewStub(channel_);
    CHECK(stub_ != nullptr, "FakeEdge: stub creation failed");
  }

  // Plaintext channel (for negative test: plaintext rejected).
  FakeEdge(const std::string& endpoint, int /*plaintext_marker*/) {
    auto creds = grpc::InsecureChannelCredentials();
    channel_ = grpc::CreateChannel(endpoint, creds);
    stub_ = masi::inference::v1::CentralInference::NewStub(channel_);
    CHECK(stub_ != nullptr, "FakeEdge: plaintext stub creation failed");
  }

  grpc::Status GetBinding(const masi::edge::v1::GetBindingRequest& req,
                           masi::edge::v1::BindingReadback* resp,
                           int64_t deadline_ms = 2000) {
    grpc::ClientContext ctx;
    ctx.set_deadline(std::chrono::system_clock::now() +
                     std::chrono::milliseconds(deadline_ms));
    return stub_->GetBinding(&ctx, req, resp);
  }

  grpc::Status Infer(const masi::edge::v1::InferenceInputBatch& req,
                     masi::edge::v1::InferenceResultBatch* resp,
                     int64_t deadline_ms = 2000) {
    grpc::ClientContext ctx;
    ctx.set_deadline(std::chrono::system_clock::now() +
                     std::chrono::milliseconds(deadline_ms));
    return stub_->Infer(&ctx, req, resp);
  }

 private:
  std::shared_ptr<grpc::Channel> channel_;
  std::unique_ptr<masi::inference::v1::CentralInference::Stub> stub_;
};

// ---------------------------------------------------------------------------
// Build an InferenceRecord from the golden valid-batch-v1.json input
// ---------------------------------------------------------------------------
inline void fill_record_from_golden(masi::edge::v1::InferenceRecord* rec,
                                    const nlohmann::json& j) {
  rec->set_input_id(j.value("input_id", ""));
  rec->set_event_idempotency_key(j.value("event_idempotency_key", ""));
  rec->set_window_id(j.value("window_id", ""));
  rec->set_quality(j.value("quality", "valid"));
  rec->set_dtype(j.value("dtype", "uint64-le"));
  rec->set_final_window(j.value("final_window", true));
  if (j.contains("feature_tensor_hex")) {
    auto bytes = hex_to_bytes(j["feature_tensor_hex"].get<std::string>());
    rec->set_feature_tensor(bytes.data(), bytes.size());
  }
  if (j.contains("shape")) {
    for (const auto& s : j["shape"]) rec->add_shape(s.get<uint32_t>());
  }
  if (j.contains("feature_contract_digest"))
    rec->set_feature_contract_digest(j["feature_contract_digest"].get<std::string>());
  if (j.contains("label_contract_digest"))
    rec->set_label_contract_digest(j["label_contract_digest"].get<std::string>());
  if (j.contains("output_adapter_digest"))
    rec->set_output_adapter_digest(j["output_adapter_digest"].get<std::string>());
}

inline void fill_route_from_golden(masi::edge::v1::InferenceRoute* route,
                                   const nlohmann::json& j) {
  route->set_schema_version("inference-central-grpc-batch/v1");
  route->set_shard_id(j.value("shard_id", ""));
  route->set_model_control_incarnation_id(j.value("model_control_incarnation_id", ""));
  route->set_logical_pool_id(j.value("logical_pool_id", ""));
  route->set_pool_generation(j.value("pool_generation", 0));
  route->set_binding_generation(j.value("binding_generation", 0));
  route->set_route_epoch(j.value("route_epoch", 0));
  route->set_runtime_profile(j.value("runtime_profile", "model-runtime-central-cpu/v1"));
  route->set_wire_profile(j.value("wire_profile", "inference-central-grpc-batch/v1"));
}

// Build a valid InferenceInputBatch from valid-batch-v1.json.
inline masi::edge::v1::InferenceInputBatch build_valid_batch_from_golden() {
  auto j = load_golden_inference("valid-batch-v1.json");
  masi::edge::v1::InferenceInputBatch batch;
  batch.set_schema_version(j["input"].value("schema_version", "inference-central-grpc-batch/v1"));
  batch.set_request_id(j["input"].value("request_id", ""));
  batch.set_deadline_unix_ms(j["input"].value("deadline_unix_ms", 9999999999999LL));
  batch.set_batch_digest(j["input"].value("batch_digest", ""));
  batch.set_trace_id("trace-golden-0001");
  auto* route = batch.mutable_route();
  fill_route_from_golden(route, j["input"]["route"]);
  for (const auto& rj : j["input"]["records"]) {
    auto* rec = batch.add_records();
    fill_record_from_golden(rec, rj);
  }
  return batch;
}

}  // namespace masi::inf::test