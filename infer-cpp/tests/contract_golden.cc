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
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "support/mod.h"

#include "digest.h"
#include "numeric.h"

#include "edge/v1/edge.pb.h"
#include "inference/v1/inference.pb.h"

namespace {

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

  // Frozen cross-language bytes: the digest is pinned, not merely checked for
  // a prefix, so any wire or golden drift fails this gate.
  CHECK(digest == "sha256:75c57f866ac5ee134ea956d6c3454be166442a06feaeeb685616ca8aa6d37004",
        "InferenceRecord frozen bytes drifted: " + digest);
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
  CHECK(digest == "sha256:ede0e84ae6c3abc0a1e4d19ad45db1a5f826c610e2612ad370922728c1d0399a",
        "InferenceInputBatch frozen bytes drifted: " + digest);
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
       "edge-command-execution/v1"},
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
  CHECK(cat["schema_version"].get<std::string>() == "inference-golden-catalog/v2",
        "catalog schema_version wrong");
  const auto& vectors = cat["vectors"];
  CHECK(vectors.size() == 11, "catalog must list 11 golden vectors");
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
    const std::string status = j["expected"]["status"].get<std::string>();
    CHECK(status == "rejected" || status == "fenced",
          std::string("negative golden ") + n.file +
              " expected.status is not 'rejected' or 'fenced'");
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
  rec.set_input_id("input-001");
  rec.set_event_idempotency_key("event-001");
  rec.set_input_digest("sha256:aaaa");
  rec.set_model_control_incarnation_id("inc-001");
  rec.set_operation_id("op-001");
  rec.set_scope("scope-001");
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
  // The 29 result_fence dimensions are conceptual identity dimensions from
  // contracts/inference/v1/profile.json. They are carried across
  // InferenceInputBatch (request_id), InferenceRoute (shard_id, route_epoch,
  // pool/binding generation, etc.), and InferenceResultRecord (input_id,
  // event_idempotency_key, worker_id, etc.). Verify the fields that ARE on
  // InferenceResultRecord are present and stable.
  static const std::vector<std::string> kRecordFenceFields = {
      "input_id", "event_idempotency_key", "input_digest",
      "model_control_incarnation_id", "operation_id", "scope",
      "route_epoch", "logical_pool_id", "pool_generation", "binding_generation",
      "startup_envelope_digest", "pool_observation_digest", "binding_digest",
      "model_revision_digest", "model_bundle_digest", "feature_contract_digest",
      "label_contract_digest", "output_adapter_digest",
      "wire_profile_digest", "runtime_profile_digest",
      "optimization_profile_digest", "worker_id", "worker_digest",
      "worker_attempt_id", "source_wal_sequence", "input_wal_sequence",
      "result_wal_sequence", "trace_id",
  };
  for (const auto& fname : kRecordFenceFields) {
    const auto* field = desc->FindFieldByName(fname);
    CHECK(field != nullptr, "InferenceResultRecord missing fence field: " + fname);
  }
}

// ---------------------------------------------------------------------------
// Test: the valid golden vectors' pinned canonical output equals what the
// qualified adapter implementation produces from the pinned raw scores. This
// keeps the golden expectations and the shipped decision rule from drifting
// apart without needing a running Triton.
// ---------------------------------------------------------------------------
void test_golden_adapter_cross_check() {
  const auto bundle = masi::inf::test::load_json(
      masi::inf::test::repo_root() +
      "/testkit/fixtures/repositories/masi-ids-window-v1-r2/bundle-manifest.json");

  masi::inf::NumericProfile p;
  p.adapter_id = bundle["output_adapter"]["adapter_id"].get<std::string>();
  p.adapter_digest = bundle["output_adapter"]["adapter_digest"].get<std::string>();
  p.score_domain = bundle["label_taxonomy"]["score_domain"].get<std::string>();
  for (const auto& c : bundle["output_adapter"]["class_order"])
    p.class_order.push_back(c.get<uint32_t>());
  p.alert_threshold = bundle["output_adapter"]["threshold"].get<double>();
  p.abstain_below = bundle["label_taxonomy"]["threshold"]["value"].get<double>();

  const std::vector<std::string> vectors = {"valid-batch-v1.json",
                                            "valid-multi-record-batch-v1.json"};
  for (const auto& name : vectors) {
    const auto golden = masi::inf::test::load_golden_inference(name);
    // The golden must pin the adapter identity it was computed with.
    CHECK(golden["adapter"]["adapter_id"].get<std::string>() == p.adapter_id,
          name + ": adapter identity differs from the pinned bundle manifest");
    CHECK(golden["adapter"]["score_domain"].get<std::string>() == p.score_domain,
          name + ": score_domain differs from the pinned bundle manifest");

    const auto& expected = golden["expected"];
    std::vector<nlohmann::json> rows;
    if (expected.contains("rows")) {
      for (const auto& r : expected["rows"]) rows.push_back(r);
    } else {
      rows.push_back(expected);
    }
    for (size_t i = 0; i < rows.size(); ++i) {
      std::vector<float> raw;
      for (const auto& v : rows[i]["raw_scores"]) raw.push_back(v.get<float>());
      const auto adapted = masi::inf::apply_output_adapter(raw, p);
      CHECK(adapted.decision == rows[i]["decision"].get<std::string>(),
            name + " row " + std::to_string(i) + ": adapter decision '" +
                adapted.decision + "' != golden");
      CHECK(adapted.predicted_label == rows[i]["predicted_label"].get<uint32_t>(),
            name + " row " + std::to_string(i) + ": adapter predicted_label != golden");
      CHECK(adapted.quality == rows[i]["quality"].get<std::string>(),
            name + " row " + std::to_string(i) + ": adapter quality != golden");
      CHECK(adapted.scores.size() == rows[i]["scores"].size(),
            name + " row " + std::to_string(i) + ": score count != golden");
      for (size_t k = 0; k < adapted.scores.size(); ++k) {
        const double want = rows[i]["scores"][k].get<double>();
        const double got = adapted.scores[k];
        CHECK(std::fabs(got - want) <= 1e-6 + 1e-5 * std::fabs(want),
              name + " row " + std::to_string(i) + ": score " + std::to_string(k) +
                  " != golden");
      }
      // Canonical float32 little-endian digest over the class-ordered scores.
      std::string canonical;
      canonical.resize(adapted.scores.size() * 4);
      for (size_t k = 0; k < adapted.scores.size(); ++k) {
        uint32_t bits;
        std::memcpy(&bits, &adapted.scores[k], sizeof(bits));
        canonical[k * 4 + 0] = static_cast<char>(bits & 0xff);
        canonical[k * 4 + 1] = static_cast<char>((bits >> 8) & 0xff);
        canonical[k * 4 + 2] = static_cast<char>((bits >> 16) & 0xff);
        canonical[k * 4 + 3] = static_cast<char>((bits >> 24) & 0xff);
      }
      const std::string digest =
          masi::inf::sha256_hex(canonical.data(), canonical.size());
      CHECK(digest == rows[i]["output_digest"].get<std::string>(),
            name + " row " + std::to_string(i) + ": canonical output digest != golden");
    }
  }
  std::cout << "Golden adapter cross-check OK (" << vectors.size() << " vectors).\n";
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
  test_golden_adapter_cross_check();
  std::cout << "All contract golden tests passed.\n";
  return 0;
}