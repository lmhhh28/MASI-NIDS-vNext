#pragma once

#include <cstdint>
#include <fstream>
#include <map>
#include <nlohmann/json.hpp>
#include <set>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

#include "digest.h"
#include "error.h"

namespace masi::inf {

inline constexpr int32_t kProfileMaxRecordsPerBatch = 256;
inline constexpr int32_t kProfileMaxMessageBytes = 4194304;
inline constexpr int32_t kProfileMaxInFlight = 64;
inline constexpr int32_t kProfileMaxRequestDeadlineMs = 2000;
inline constexpr int32_t kProfileMaxDrainMs = 30000;

struct Config {
  std::string startup_envelope_path;
  std::string model_repository_path;
  std::string triton_endpoint = "127.0.0.1:8001";
  // Gateway-to-Triton channel identity. When these are empty the endpoint must
  // be loopback; a non-loopback plaintext endpoint is rejected at connect().
  std::string triton_tls_ca_path;
  std::string triton_tls_cert_path;
  std::string triton_tls_key_path;
  std::string triton_tls_server_name;
  std::string gateway_listen = "0.0.0.0:7443";
  std::string tls_ca_path;
  std::string tls_cert_path;
  std::string tls_key_path;
  // Exact client SAN allowlist. Required and non-empty: the frozen profile
  // demands CA-chain-plus-exact-SAN peer verification.
  std::vector<std::string> client_san_allowlist;
  int32_t max_records_per_batch = 256;
  int32_t max_request_bytes = 4194304;
  int32_t max_response_bytes = 4194304;
  int32_t max_in_flight = 64;
  int32_t request_deadline_ms = 2000;
  std::string runtime_profile = "model-runtime-central-cpu/v1";
  std::string availability_profile = "availability-single/v1";
  int32_t intra_op_num_threads = 0;
  int32_t inter_op_num_threads = 0;
  std::string intra_op_affinity;
  bool enable_cpu_arena = true;
  int32_t drain_ms = 5000;
};

inline bool is_regular_file_no_symlink(const std::string &path) {
  struct stat st;
  if (lstat(path.c_str(), &st) != 0)
    return false;
  if (!S_ISREG(st.st_mode))
    return false;
  if (S_ISLNK(st.st_mode))
    return false;
  return true;
}

inline void validate_file_path(const std::string &path, const std::string &field) {
  if (path.empty())
    throw error::Exception(error::Code::kInvalidManifest, field + " empty");
  if (!is_regular_file_no_symlink(path))
    throw error::Exception(error::Code::kInvalidManifest,
                           field + " not regular file or is symlink");
  struct stat st;
  if (stat(path.c_str(), &st) != 0)
    throw error::Exception(error::Code::kInvalidManifest, field + " stat failed");
  if (st.st_size > 1048576)
    throw error::Exception(error::Code::kInvalidManifest, field + " exceeds 1MiB");
}

inline Config load_config(const std::string &path) {
  validate_file_path(path, "config");
  std::ifstream f(path);
  if (!f)
    throw error::Exception(error::Code::kInvalidManifest, "config open failed");
  nlohmann::json j;
  try {
    f >> j;
  } catch (const std::exception &e) {
    throw error::Exception(error::Code::kInvalidManifest,
                           std::string("config json parse: ") + e.what());
  }
  if (!j.is_object())
    throw error::Exception(error::Code::kInvalidManifest, "config not object");
  Config cfg;
  cfg.startup_envelope_path = j.value("startup_envelope_path", "");
  cfg.model_repository_path = j.value("model_repository_path", "");
  cfg.triton_endpoint = j.value("triton_endpoint", cfg.triton_endpoint);
  cfg.triton_tls_ca_path = j.value("triton_tls_ca_path", "");
  cfg.triton_tls_cert_path = j.value("triton_tls_cert_path", "");
  cfg.triton_tls_key_path = j.value("triton_tls_key_path", "");
  cfg.triton_tls_server_name = j.value("triton_tls_server_name", "");
  cfg.gateway_listen = j.value("gateway_listen", cfg.gateway_listen);
  cfg.tls_ca_path = j.value("tls_ca_path", "");
  cfg.tls_cert_path = j.value("tls_cert_path", "");
  cfg.tls_key_path = j.value("tls_key_path", "");
  if (j.contains("client_san_allowlist")) {
    const auto &a = j.at("client_san_allowlist");
    if (!a.is_array())
      throw error::Exception(error::Code::kInvalidManifest, "client_san_allowlist not array");
    if (a.size() > 64)
      throw error::Exception(error::Code::kInvalidManifest,
                             "client_san_allowlist exceeds 64 entries");
    for (const auto &v : a) {
      if (!v.is_string() || v.get<std::string>().empty())
        throw error::Exception(error::Code::kInvalidManifest,
                               "client_san_allowlist entry not a non-empty string");
      cfg.client_san_allowlist.push_back(v.get<std::string>());
    }
  }
  cfg.max_records_per_batch = j.value("max_records_per_batch", cfg.max_records_per_batch);
  cfg.max_request_bytes = j.value("max_request_bytes", cfg.max_request_bytes);
  cfg.max_response_bytes = j.value("max_response_bytes", cfg.max_response_bytes);
  cfg.max_in_flight = j.value("max_in_flight", cfg.max_in_flight);
  cfg.request_deadline_ms = j.value("request_deadline_ms", cfg.request_deadline_ms);
  cfg.runtime_profile = j.value("runtime_profile", cfg.runtime_profile);
  cfg.availability_profile = j.value("availability_profile", cfg.availability_profile);
  cfg.intra_op_num_threads = j.value("intra_op_num_threads", cfg.intra_op_num_threads);
  cfg.inter_op_num_threads = j.value("inter_op_num_threads", cfg.inter_op_num_threads);
  cfg.intra_op_affinity = j.value("intra_op_affinity", cfg.intra_op_affinity);
  cfg.enable_cpu_arena = j.value("enable_cpu_arena", cfg.enable_cpu_arena);
  cfg.drain_ms = j.value("drain_ms", cfg.drain_ms);
  // Unknown fields reject
  for (auto it = j.begin(); it != j.end(); ++it) {
    static const std::set<std::string> known = {
        "startup_envelope_path",  "model_repository_path", "triton_endpoint",
        "triton_tls_ca_path",     "triton_tls_cert_path",  "triton_tls_key_path",
        "triton_tls_server_name", "gateway_listen",        "tls_ca_path",
        "tls_cert_path",          "tls_key_path",          "client_san_allowlist",
        "max_records_per_batch",  "max_request_bytes",     "max_response_bytes",
        "max_in_flight",          "request_deadline_ms",   "runtime_profile",
        "availability_profile",   "intra_op_num_threads",  "inter_op_num_threads",
        "intra_op_affinity",      "enable_cpu_arena",      "drain_ms"};
    if (known.find(it.key()) == known.end())
      throw error::Exception(error::Code::kInvalidManifest, "config unknown field: " + it.key());
  }
  if (cfg.startup_envelope_path.empty())
    throw error::Exception(error::Code::kInvalidManifest, "startup_envelope_path empty");
  if (cfg.model_repository_path.empty())
    throw error::Exception(error::Code::kInvalidManifest, "model_repository_path empty");
  if (cfg.client_san_allowlist.empty())
    throw error::Exception(error::Code::kInvalidManifest,
                           "client_san_allowlist must declare at least one exact SAN");
  auto bounded_positive = [](int32_t value, int32_t ceiling, const char *field) {
    if (value <= 0 || value > ceiling) {
      throw error::Exception(error::Code::kInvalidManifest, std::string(field) + " must be in [1," +
                                                                std::to_string(ceiling) + "]");
    }
  };
  bounded_positive(cfg.max_records_per_batch, kProfileMaxRecordsPerBatch, "max_records_per_batch");
  bounded_positive(cfg.max_request_bytes, kProfileMaxMessageBytes, "max_request_bytes");
  bounded_positive(cfg.max_response_bytes, kProfileMaxMessageBytes, "max_response_bytes");
  bounded_positive(cfg.max_in_flight, kProfileMaxInFlight, "max_in_flight");
  bounded_positive(cfg.drain_ms, kProfileMaxDrainMs, "drain_ms");
  bounded_positive(cfg.request_deadline_ms, kProfileMaxRequestDeadlineMs, "request_deadline_ms");
  if (cfg.intra_op_num_threads < 0 || cfg.intra_op_num_threads > 512 ||
      cfg.inter_op_num_threads < 0 || cfg.inter_op_num_threads > 512) {
    throw error::Exception(error::Code::kInvalidManifest,
                           "intra/inter_op_num_threads must be in [0,512]");
  }
  const bool any_triton_tls =
      !cfg.triton_tls_ca_path.empty() || !cfg.triton_tls_cert_path.empty() ||
      !cfg.triton_tls_key_path.empty() || !cfg.triton_tls_server_name.empty();
  const bool all_triton_tls =
      !cfg.triton_tls_ca_path.empty() && !cfg.triton_tls_cert_path.empty() &&
      !cfg.triton_tls_key_path.empty() && !cfg.triton_tls_server_name.empty();
  if (any_triton_tls && !all_triton_tls)
    throw error::Exception(error::Code::kInvalidManifest,
                           "triton_tls_* requires ca, cert, key and server_name together");
  if (cfg.runtime_profile != "model-runtime-central-cpu/v1" &&
      cfg.runtime_profile != "model-runtime-central-cuda/v1")
    throw error::Exception(error::Code::kRuntimeProfileUnsupported, cfg.runtime_profile);
  return cfg;
}

} // namespace masi::inf
