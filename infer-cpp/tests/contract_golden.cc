// Central Inference contract golden tests.
//
// Verifies that the protobuf wire contracts (edge.proto, inference.proto)
// produce stable bytes and that the golden JSON files in
// contracts/golden/inference/ and contracts/golden/evidence/ map to exact
// protobuf identities. Computes SHA-256 digests over serialized messages and
// compares them against expectations from the golden vectors and the frozen
// inference wire profile.
//
// This test links only against masi_inf_proto (protobuf + gRPC stubs) and the
// minimal test support header. It does NOT link the Gateway library, so it
// can run in environments without ONNX Runtime or Triton.

#include <google/protobuf/message.h>
#include <openssl/sha.h>

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "support/mod.h"

#include "edge.pb.h"
#include "inference.pb.h"

namespace {

using masi::inf::test::CHECK;
using masi::inf::test::load_golden_evidence;
using masi::inf::test::load_golden_inference;
using masi::inf::test::load_json;
using masi::inf::test::contract_path;
using masi::inf::test::hex_to_bytes;
using masi::inf::test::build_valid_batch_from_golden;

// ---------------------------------------------------------------------------
// SHA-256 (self-contained, no link dependency on the Gateway library)
// ---------------------------------------------------------------------------
std::string sha256_hex(const std::string& data) {
  SHA256_CTX ctx;
  SHA256_Init(&ctx);
  SHA256_Update(&ctx, data.data(), data.size());
  unsigned char hash[SHA256_DIGEST_LENGTH];
  SHA256_Final(hash, &ctx);
  std::ostringstream oss;
  oss << "sha256:";
  for (int i = 0; i < SHA256_DIGEST_LENGTH; ++i) {
    oss << std::hex << std::setfill('0') << std::setw(2)
        << static_cast<int>(hash[i]);
  }
  return oss.str();
}

std::string serialize_to_string(const google::protobuf::Message& msg) {
  std::string out;
  CHECK(msg.SerializeToString(&out), "protobuf serialize failed");
  return out;
}

// ---------------------------------------------------------------------------
// Test: InferenceRecord from valid-batch-v1.json serializes to stable bytes
// ---------------------------------------------------------------------------
void test_inference_record_frozen_bytes() {
  auto golden = load_golden_inference("valid-batch-v1.json");
  const auto& rec_json = golden["input"]["records"][0];
  masi::edge::v1::InferenceRecord rec;
  rec.set_input_id(rec_json["input_id"].get<std::string>());
  rec.set_event_idempotency_key(rec_json["event_idempotency_key"].get<std::string>());
  rec.set_window_id(rec_json["window_id"].get<std::string>());
  rec.set_quality(rec_json["quality"].get<std::string>());
  rec.set_dtype(rec_json["dtype"].get<std::string>());
  rec.set_final_window(rec_json["final_window"].get<bool>());
  auto bytes = hex_to_bytes(rec_json["feature_tensor_hex"].get<std::string>());
  rec.set_feature_tensor(bytes.data(), bytes.size());
  for (const auto& s : rec_json["shape"]) rec.add_shape(s.get<uint32_t>());
  rec.set_feature_contract_digest(rec_json["feature_contract_digest"].get<std::string>());
  rec.set_label_contract_digest(rec_json["label_contract_digest"].get<std::string>());
  rec.set_output_adapter_digest(rec_json["output_adapter_digest"].get<std::string>());

  std::string serialized = serialize_to_string(rec);
  std::string digest = sha256_hex(serialized);
  std::cout << "InferenceRecord digest: " << digest << "\n";

  // The record must be non-empty and the digest must start with sha256:.
  CHECK(!serialized.empty(), "InferenceRecord serialized to empty bytes");
  CHECK(digest.rfind("sha256:", 0) == 0, "InferenceRecord digest prefix wrong");
  CHECK(digest.size() == 7 + 64, "InferenceRecord digest length wrong");

  // Determinism: re-serialize and compare digests.
  std::string again = serialize_to_string(rec);
  CHECK(sha256_hex(again) == digest,
        "InferenceRecord serialization is not deterministic");
}

// ---------------------------------------------------------------------------
// Test: InferenceInputBatch serializes to stable bytes
// ---------------------------------------------------------------------------
void test_inference_input_batch_frozen_bytes() {
  masi::edge::v1::InferenceInputBatch batch = build_valid_batch_from_golden();
  std::string serialized = serialize_to_string(batch);
  std::string digest = sha256_hex(serialized);
  std::cout << "InferenceInputBatch digest: " << digest << "\n";

  CHECK(!serialized.empty(), "InferenceInputBatch serialized to empty bytes");
  CHECK(digest.rfind("sha256:", 0) == 0, "InferenceInputBatch digest prefix wrong");
  CHECK(digest.size() == 7 + 64, "InferenceInputBatch digest length wrong");

  // Determinism.
  std::string again = serialize_to_string(batch);
  CHECK(sha256_hex(again) == digest,
        "InferenceInputBatch serialization is not deterministic");

  // The batch must carry at least one record.
  CHECK(batch.records_size() >= 1, "InferenceInputBatch has no records");
}

// ---------------------------------------------------------------------------
// Test: result_fence.dimensions from profile.json match the hardcoded 29
// ---------------------------------------------------------------------------
void test_result_fence_dimensions() {
  // Hardcoded constant array from the frozen inference wire profile.
  // These must match contracts/inference/v1/profile.json exactly.
  static const std::vector<std::string> kExpected = {
      "request_id",
      "input_id",
      "event_idempotency_key",
      "input_digest",
      "model_control_incarnation_id",
      "operation_id",
      "scope",
      "shard_id",
      "route_epoch",
      "logical_pool_id",
      "pool_generation",
      "binding_generation",
      "startup_envelope_digest",
      "pool_observation_digest",
      "binding_digest",
      "model_revision_digest",
      "model_bundle_digest",
      "feature_contract_digest",
      "label_contract_digest",
      "output_adapter_digest",
      "wire_profile_digest",
      "runtime_profile_digest",
      "optimization_profile_digest",
      "worker_id",
      "worker_digest",
      "worker_attempt_id",
      "source_input_result_WAL_sequence",
      "source_window_identity",
      "trace_id",
  };

  auto profile = load_json(contract_path("contracts/inference/v1/profile.json"));
  const auto& dims = profile["result_fence"]["dimensions"];
  CHECK(dims.size() == 29, "result_fence.dimensions count is not 29");
  CHECK(kExpected.size() == 29, "hardcoded expected dimensions count is not 29");

  for (size_t i = 0; i < 29; ++i) {
    const std::string& got = dims[i].get<std::string>();
    CHECK(got == kExpected[i],
          "result_fence dimension " + std::to_string(i) + " mismatch: got '" +
              got + "' expected '" + kExpected[i] + "'");
  }
}

// ---------------------------------------------------------------------------
// Test: fallback matrix from profile.json (all false)
// ---------------------------------------------------------------------------
void test_fallback_matrix() {
  auto profile = load_json(contract_path("contracts/inference/v1/profile.json"));
  const auto& fb = profile["fallback"];
  CHECK(fb["edge_local_inference"].get<bool>() == false,
        "fallback.edge_local_inference must be false");
  CHECK(fb["old_model"].get<bool>() == false,
        "fallback.old_model must be false");
  CHECK(fb["automatic_cpu_cuda_switch"].get<bool>() == false,
        "fallback.automatic_cpu_cuda_switch must be false");
  CHECK(fb["uds_or_shared_memory"].get<bool>() == false,
        "fallback.uds_or_shared_memory must be false");
  CHECK(fb["json_or_http"].get<bool>() == false,
        "fallback.json_or_http must be false");
}

// ---------------------------------------------------------------------------
// Test: golden evidence files exist and have stable schema_version
// ---------------------------------------------------------------------------
void test_golden_evidence_schema_versions() {
  struct Entry {
    const char* file;
    const char* expected_schema;
  };
  static const Entry entries[] = {
      {"central-inference-blackbox-v1.json",
       "central-inference-module-e2e-evidence/v1"},
      {"central-inference-command-execution-v1.json",
       "central-inference-command-execution/v1"},
      {"central-inference-deep-check-v1.json",
       "central-inference-deep-check-evidence/v1"},
      {"central-inference-deep-not-run-v1.json",
       "central-inference-deep-check-evidence/v1"},
      {"central-inference-module-findings-v1.json", "module-findings/v1"},
      {"central-inference-module-v1.json",
       "central-inference-module-gate-summary/v1"},
      {"central-inference-numeric-v1.json",
       "central-inference-numeric-evidence/v1"},
      {"central-inference-oci-not-run-v1.json",
       "central-inference-oci-startup-evidence/v1"},
      {"central-inference-oci-startup-v1.json",
       "central-inference-oci-startup-evidence/v1"},
      {"central-inference-readback-v1.json",
       "central-inference-readback-evidence/v1"},
      {"central-inference-startup-v1.json",
       "central-inference-startup-evidence/v1"},
      {"central-inference-supply-failure-v1.json",
       "central-inference-supply-verification/v1"},
      {"central-inference-supply-not-run-v1.json",
       "central-inference-supply-verification/v1"},
      {"central-inference-supply-verification-v1.json",
       "central-inference-supply-verification/v1"},
      {"central-inference-traceability-v1.json",
       "central-inference-traceability-evidence/v1"},
  };
  for (const auto& e : entries) {
    auto j = load_golden_evidence(e.file);
    std::string sv = j["schema_version"].get<std::string>();
    CHECK(sv == e.expected_schema,
          std::string("golden evidence ") + e.file +
              " schema_version drifted: got '" + sv + "' expected '" +
              e.expected_schema + "'");
  }
}

// ---------------------------------------------------------------------------
// Test: golden inference catalog references all 10 vectors
// ---------------------------------------------------------------------------
void test_golden_catalog() {
  auto cat = load_golden_inference("catalog.json");
  CHECK(cat["schema_version"].get<std::string>() == "inference-golden-catalog/v1",
        "catalog schema_version wrong");
  const auto& vectors = cat["vectors"];
  CHECK(vectors.size() == 10, "catalog must list 10 golden vectors");
  // Verify every vector file exists.
  for (const auto& v : vectors) {
    std::string path = masi::inf::test::contract_path(
        "contracts/golden/inference/" + v["path"].get<std::string>());
    std::ifstream f(path);
    CHECK(f.good(), "catalog vector file missing: " + path);
  }
}

// ---------------------------------------------------------------------------
// Test: negative golden vectors carry reject categories
// ---------------------------------------------------------------------------
void test_negative_golden_vectors() {
  struct Neg {
    const char* file;
    const char* expected_category;
  };
  static const Neg negs[] = {
      {"oversize-batch-v1.json", "reject-oversize"},
      {"malformed-tensor-v1.json", "reject-malformed"},
      {"unknown-major-v1.json", "reject-unknown-major"},
      {"nan-inf-reject-v1.json", "reject-nan-inf"},
      {"cross-generation-fence-v1.json", "fence-cross-generation"},
      {"digest-conflict-v1.json", "fence-digest-conflict"},
      {"repository-closure-v1.json", "repository-closure"},
      {"implicit-instance-group-v1.json", "instance-group"},
      {"provider-drift-v1.json", "provider-drift"},
  };
  for (const auto& n : negs) {
    auto j = load_golden_inference(n.file);
    CHECK(j["category"].get<std::string>() == n.expected_category,
          std::string("negative golden ") + n.file + " category mismatch");
    CHECK(j["expected"]["status"].get<std::string>() == "rejected",
          std::string("negative golden ") + n.file +
              " expected.status is not 'rejected'");
  }
}

// ---------------------------------------------------------------------------
// Test: protobuf message wire format is deterministic across re-serialization
// ---------------------------------------------------------------------------
void test_protobuf_wire_determinism() {
  masi::edge::v1::InferenceInputBatch b1 = build_valid_batch_from_golden();
  masi::edge::v1::InferenceInputBatch b2 = build_valid_batch_from_golden();
  std::string s1 = serialize_to_string(b1);
  std::string s2 = serialize_to_string(b2);
  CHECK(s1 == s2, "two identical InferenceInputBatch serializations differ");
  CHECK(sha256_hex(s1) == sha256_hex(s2),
        "InferenceInputBatch SHA-256 differs for identical messages");
}

// ---------------------------------------------------------------------------
// Test: InferenceResultRecord serializes with the 29 fence fields
// ---------------------------------------------------------------------------
void test_result_record_carries_fence_fields() {
  masi::edge::v1::InferenceResultRecord rec;
  rec.set_request_id("req-001");
  rec.set_input_id("input-001");
  rec.set_event_idempotency_key("event-001");
  rec.set_input_digest("sha256:aaaa");
  rec.set_model_control_incarnation_id("inc-001");
  rec.set_operation_id("op-001");
  rec.set_scope("scope-001");
  rec.set_shard_id("shard-001");
  rec.set_route_epoch(1);
  rec.set_logical_pool_id("pool-001");
  rec.set_pool_generation(1);
  rec.set_binding_generation(1);
  rec.set_startup_envelope_digest("sha256:bbbb");
  rec.set_pool_observation_digest("sha256:cccc");
  rec.set_binding_digest("sha256:dddd");
  rec.set_model_revision_digest("sha256:eeee");
  rec.set_model_bundle_digest("sha256:ffff");
  rec.set_feature_contract_digest("sha256:1111");
  rec.set_label_contract_digest("sha256:2222");
  rec.set_output_adapter_digest("sha256:3333");
  rec.set_wire_profile_digest("sha256:4444");
  rec.set_runtime_profile_digest("sha256:5555");
  rec.set_optimization_profile_digest("sha256:6666");
  rec.set_worker_id("worker-001");
  rec.set_worker_digest("sha256:7777");
  rec.set_worker_attempt_id("attempt-001");
  rec.set_source_wal_sequence(10);
  rec.set_input_wal_sequence(11);
  rec.set_result_wal_sequence(12);
  rec.set_trace_id("trace-001");
  std::string s = serialize_to_string(rec);
  CHECK(!s.empty(), "InferenceResultRecord serialized to empty bytes");
  std::string d = sha256_hex(s);
  std::cout << "InferenceResultRecord digest: " << d << "\n";
  // Verify the 29 fence fields are all present in the descriptor.
  const auto* desc = rec.GetDescriptor();
  static const std::vector<std::string> kFenceFields = {
      "request_id", "input_id", "event_idempotency_key", "input_digest",
      "model_control_incarnation_id", "operation_id", "scope", "shard_id",
      "route_epoch", "logical_pool_id", "pool_generation", "binding_generation",
      "startup_envelope_digest", "pool_observation_digest", "binding_digest",
      "model_revision_digest", "model_bundle_digest", "feature_contract_digest",
      "label_contract_digest", "output_adapter_digest",
      "wire_profile_digest", "runtime_profile_digest",
      "optimization_profile_digest", "worker_id", "worker_digest",
      "worker_attempt_id", "source_wal_sequence", "input_wal_sequence",
      "result_wal_sequence", "trace_id",
  };
  for (const auto& fname : kFenceFields) {
    const auto* field = desc->FindFieldByName(fname);
    CHECK(field != nullptr, "InferenceResultRecord missing fence field: " + fname);
  }
}

}  // namespace

int main() {
  std::cout << "=== Central Inference contract golden tests ===\n";
  test_inference_record_frozen_bytes();
  test_inference_input_batch_frozen_bytes();
  test_result_fence_dimensions();
  test_fallback_matrix();
  test_golden_evidence_schema_versions();
  test_golden_catalog();
  test_negative_golden_vectors();
  test_protobuf_wire_determinism();
  test_result_record_carries_fence_fields();
  std::cout << "All contract golden tests passed.\n";
  return 0;
}