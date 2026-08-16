#pragma once

#include <cstdint>
#include <fstream>
#include <map>
#include <nlohmann/json.hpp>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>

#include "digest.h"
#include "error.h"

namespace masi::inf {

struct Config {
  std::string startup_envelope_path;
  std::string model_repository_path;
  std::string triton_endpoint = "127.0.0.1:8001";
  std::string gateway_listen = "0.0.0.0:7443";
  std::string tls_ca_path;
  std::string tls_cert_path;
  std::string tls_key_path;
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

inline bool is_regular_file_no_symlink(const std::string& path) {
  struct stat st;
  if (stat(path.c_str(), &st) != 0) return false;
  if (!S_ISREG(st.st_mode)) return false;
  if (S_ISLNK(st.st_mode)) return false;
  return true;
}

inline void validate_file_path(const std::string& path, const std::string& field) {
  if (path.empty()) throw error::Exception(error::Code::kInvalidManifest, field + " empty");
  if (!is_regular_file_no_symlink(path))
    throw error::Exception(error::Code::kInvalidManifest, field + " not regular file or is symlink");
  struct stat st;
  if (stat(path.c_str(), &st) != 0)
    throw error::Exception(error::Code::kInvalidManifest, field + " stat failed");
  if (st.st_size > 1048576)
    throw error::Exception(error::Code::kInvalidManifest, field + " exceeds 1MiB");
}

inline Config load_config(const std::string& path) {
  validate_file_path(path, "config");
  std::ifstream f(path);
  if (!f) throw error::Exception(error::Code::kInvalidManifest, "config open failed");
  nlohmann::json j;
  try {
    f >> j;
  } catch (const std::exception& e) {
    throw error::Exception(error::Code::kInvalidManifest, std::string("config json parse: ") + e.what());
  }
  if (!j.is_object())
    throw error::Exception(error::Code::kInvalidManifest, "config not object");
  Config cfg;
  cfg.startup_envelope_path = j.value("startup_envelope_path", "");
  cfg.model_repository_path = j.value("model_repository_path", "");
  cfg.triton_endpoint = j.value("triton_endpoint", cfg.triton_endpoint);
  cfg.gateway_listen = j.value("gateway_listen", cfg.gateway_listen);
  cfg.tls_ca_path = j.value("tls_ca_path", "");
  cfg.tls_cert_path = j.value("tls_cert_path", "");
  cfg.tls_key_path = j.value("tls_key_path", "");
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
      "startup_envelope_path", "model_repository_path", "triton_endpoint",
      "gateway_listen", "tls_ca_path", "tls_cert_path", "tls_key_path",
      "max_records_per_batch", "max_request_bytes", "max_response_bytes",
      "max_in_flight", "request_deadline_ms", "runtime_profile",
      "availability_profile", "intra_op_num_threads", "inter_op_num_threads",
      "intra_op_affinity", "enable_cpu_arena", "drain_ms"
    };
    if (known.find(it.key()) == known.end())
      throw error::Exception(error::Code::kInvalidManifest, "config unknown field: " + it.key());
  }
  if (cfg.startup_envelope_path.empty())
    throw error::Exception(error::Code::kInvalidManifest, "startup_envelope_path empty");
  if (cfg.model_repository_path.empty())
    throw error::Exception(error::Code::kInvalidManifest, "model_repository_path empty");
  if (cfg.runtime_profile != "model-runtime-central-cpu/v1" &&
      cfg.runtime_profile != "model-runtime-central-cuda/v1")
    throw error::Exception(error::Code::kRuntimeProfileUnsupported, cfg.runtime_profile);
  return cfg;
}

}  // namespace masi::inf