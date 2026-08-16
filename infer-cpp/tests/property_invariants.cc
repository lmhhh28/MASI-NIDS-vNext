// Central Inference property invariant tests.
//
// Property-based tests for the Gateway library (masi_inf_gateway_lib):
//   - error::to_string() produces stable error code strings for all 20 codes.
//   - digest::sha256_hex() produces the correct "sha256:" + 64 hex format.
//   - digest::digest_match() is a constant-time comparison.
//   - admission::check_tensor_bounds() rejects oversize/misaligned/overflow.
//   - numeric::reject_nan_inf() rejects NaN and Inf float values.
//   - config unknown-field rejection.
//   - repository_closure extra-member rejection.
//
// This test links against masi_inf_gateway_lib and uses the minimal CHECK
// macro (no Google Test dependency).

#include <openssl/sha.h>

#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <string>
#include <vector>

#include "support/mod.h"

#include "admission.h"
#include "config.h"
#include "digest.h"
#include "error.h"
#include "numeric.h"
#include "repository_closure.h"

namespace {

using masi::inf::test::TempDir;
using masi::inf::test::write_temp_file;

// ---------------------------------------------------------------------------
// Test: error::to_string() stable for all 20 codes
// ---------------------------------------------------------------------------
void test_error_codes_stable() {
  using masi::inf::error::Code;
  using masi::inf::error::to_string;

  struct Entry {
    Code code;
    const char* expected;
  };
  static const Entry entries[] = {
      {Code::kInvalidManifest, "invalid_manifest"},
      {Code::kIncompatibleContract, "incompatible_contract"},
      {Code::kUnqualified, "unqualified"},
      {Code::kResourceExhausted, "resource_exhausted"},
      {Code::kStartupFailed, "startup_failed"},
      {Code::kReadbackMismatch, "readback_mismatch"},
      {Code::kCasConflict, "cas_conflict"},
      {Code::kBufferOverflow, "buffer_overflow"},
      {Code::kPoolUnavailable, "pool_unavailable"},
      {Code::kFenced, "fenced"},
      {Code::kRouteFenceMismatch, "route_fence_mismatch"},
      {Code::kResultIdentityMismatch, "result_identity_mismatch"},
      {Code::kResultDigestConflict, "result_digest_conflict"},
      {Code::kDeadlineExceeded, "deadline_exceeded"},
      {Code::kInferenceMessageTooLarge, "inference_message_too_large"},
      {Code::kRuntimeProfileUnsupported, "runtime_profile_unsupported"},
      {Code::kRepositoryClosureViolation, "repository_closure_violation"},
      {Code::kInstanceGroupImplicit, "instance_group_implicit"},
      {Code::kProviderPartitionDrift, "provider_partition_drift"},
      {Code::kAborted, "aborted"},
  };
  constexpr size_t kCount = sizeof(entries) / sizeof(entries[0]);
  CHECK(kCount == 20, "error code table must have 20 entries");

  for (const auto& e : entries) {
    std::string got = to_string(e.code);
    CHECK(got == e.expected,
          "error::to_string(" + std::to_string(static_cast<int>(e.code)) +
              ") = '" + got + "' expected '" + e.expected + "'");
  }

  // Verify all code strings are distinct (no aliasing).
  std::vector<std::string> strings;
  for (const auto& e : entries) strings.push_back(e.expected);
  for (size_t i = 0; i < strings.size(); ++i) {
    for (size_t j = i + 1; j < strings.size(); ++j) {
      CHECK(strings[i] != strings[j],
            "duplicate error code string: " + strings[i]);
    }
  }

  // retryable() stability.
  CHECK(masi::inf::error::retryable(Code::kResourceExhausted) == true,
        "kResourceExhausted must be retryable");
  CHECK(masi::inf::error::retryable(Code::kPoolUnavailable) == true,
        "kPoolUnavailable must be retryable");
  CHECK(masi::inf::error::retryable(Code::kDeadlineExceeded) == true,
        "kDeadlineExceeded must be retryable");
  CHECK(masi::inf::error::retryable(Code::kBufferOverflow) == true,
        "kBufferOverflow must be retryable");
  CHECK(masi::inf::error::retryable(Code::kInvalidManifest) == false,
        "kInvalidManifest must not be retryable");
  CHECK(masi::inf::error::retryable(Code::kAborted) == false,
        "kAborted must not be retryable");
}

// ---------------------------------------------------------------------------
// Test: digest::sha256_hex() format
// ---------------------------------------------------------------------------
void test_sha256_format() {
  // Empty input.
  std::string d = masi::inf::sha256_hex("", 0);
  CHECK(d.rfind("sha256:", 0) == 0, "sha256_hex prefix wrong");
  CHECK(d.size() == 7 + 64, "sha256_hex length wrong");
  // Known empty-SHA-256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
  CHECK(d == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "sha256_hex(\"\") does not match known empty hash");

  // "abc" known SHA-256: ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad
  std::string abc = "abc";
  std::string d2 = masi::inf::sha256_hex(abc.data(), abc.size());
  CHECK(d2 == "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "sha256_hex(\"abc\") does not match known hash");

  // All hex chars must be lowercase [0-9a-f].
  for (size_t i = 7; i < d.size(); ++i) {
    char c = d[i];
    CHECK((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'),
          "sha256_hex has non-lowercase-hex char");
  }
}

// ---------------------------------------------------------------------------
// Test: digest::digest_match() constant-time compare
// ---------------------------------------------------------------------------
void test_digest_match() {
  std::string a = "sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789";
  std::string b = a;
  CHECK(masi::inf::digest_match(a, b) == true,
        "digest_match: identical digests must match");

  // Single-char difference.
  std::string c = a;
  c[c.size() - 1] = (c[c.size() - 1] == '0') ? '1' : '0';
  CHECK(masi::inf::digest_match(a, c) == false,
        "digest_match: single-char difference must not match");

  // Different length.
  std::string d = "sha256:abc";
  CHECK(masi::inf::digest_match(a, d) == false,
        "digest_match: different length must not match");

  // Both empty.
  std::string e;
  std::string f;
  CHECK(masi::inf::digest_match(e, f) == true,
        "digest_match: two empty strings must match");

  // One empty, one non-empty.
  CHECK(masi::inf::digest_match(a, e) == false,
        "digest_match: one empty must not match");
}

// ---------------------------------------------------------------------------
// Test: admission::check_tensor_layout() rejects oversize/misaligned/overflow
// ---------------------------------------------------------------------------
void test_tensor_layout_checks() {
  using masi::inf::check_tensor_layout;

  // Valid: [1,6] uint64-le, 48 bytes, alignment 8.
  auto ok = check_tensor_layout({1, 6}, "uint64-le", 8);
  CHECK(ok.ok, "check_tensor_layout: valid [1,6] uint64-le should be ok");
  CHECK(ok.total_bytes == 48, "check_tensor_layout: total_bytes wrong");

  // Misaligned: [1,3] uint64-le = 24 bytes, alignment 16 -> 24 % 16 != 0.
  auto misaligned = check_tensor_layout({1, 3}, "uint64-le", 16);
  CHECK(!misaligned.ok, "check_tensor_layout: misaligned should be rejected");
  CHECK(misaligned.reason == "alignment",
        "check_tensor_layout: misaligned reason wrong");

  // Unknown dtype.
  auto bad_dtype = check_tensor_layout({1, 6}, "int8-le", 1);
  CHECK(!bad_dtype.ok, "check_tensor_layout: unknown dtype should be rejected");
  CHECK(bad_dtype.reason == "unknown dtype",
        "check_tensor_layout: unknown dtype reason wrong");

  // Empty shape.
  auto empty_shape = check_tensor_layout({}, "uint64-le", 8);
  CHECK(!empty_shape.ok, "check_tensor_layout: empty shape should be rejected");
  CHECK(empty_shape.reason == "empty shape",
        "check_tensor_layout: empty shape reason wrong");

  // Zero dim.
  auto zero_dim = check_tensor_layout({1, 0, 6}, "uint64-le", 8);
  CHECK(!zero_dim.ok, "check_tensor_layout: zero dim should be rejected");
  CHECK(zero_dim.reason == "zero dim",
        "check_tensor_layout: zero dim reason wrong");

  // Shape overflow: two very large dims whose product overflows size_t.
  uint32_t big = 0x80000000u;
  auto overflow = check_tensor_layout({big, big}, "uint64-le", 8);
  CHECK(!overflow.ok, "check_tensor_layout: shape overflow should be rejected");
  CHECK(overflow.reason == "shape overflow" || overflow.reason == "element count overflow",
        "check_tensor_layout: shape overflow reason wrong");

  // float32-le: [1,6] = 24 bytes.
  auto f32 = check_tensor_layout({1, 6}, "float32-le", 4);
  CHECK(f32.ok, "check_tensor_layout: float32-le valid should be ok");
  CHECK(f32.total_bytes == 24, "check_tensor_layout: float32 total_bytes wrong");

  // float64-le: [1,6] = 48 bytes.
  auto f64 = check_tensor_layout({1, 6}, "float64-le", 8);
  CHECK(f64.ok, "check_tensor_layout: float64-le valid should be ok");
  CHECK(f64.total_bytes == 48, "check_tensor_layout: float64 total_bytes wrong");
}

// ---------------------------------------------------------------------------
// Test: numeric::assert_input_finite() rejects NaN and Inf
// ---------------------------------------------------------------------------
void test_nan_inf_rejection() {
  using masi::inf::assert_input_finite;

  // uint64-le: cannot represent NaN/Inf; valid buffer passes.
  {
    std::vector<uint8_t> buf(48, 0);
    assert_input_finite(buf, "uint64-le");  // should not throw
  }
  // uint64-le: misaligned length (not multiple of 8).
  {
    std::vector<uint8_t> buf(7, 0);
    bool threw = false;
    try {
      assert_input_finite(buf, "uint64-le");
    } catch (const masi::inf::error::Exception&) {
      threw = true;
    }
    CHECK(threw, "assert_input_finite: uint64 misaligned length must throw");
  }
  // float32-le: NaN value (0x7fc00000).
  {
    std::vector<uint8_t> buf(4);
    uint32_t nan_bits = 0x7fc00000u;
    std::memcpy(buf.data(), &nan_bits, 4);
    bool threw = false;
    try {
      assert_input_finite(buf, "float32-le");
    } catch (const masi::inf::error::Exception&) {
      threw = true;
    }
    CHECK(threw, "assert_input_finite: float32 NaN must throw");
  }
  // float32-le: +Inf (0x7f800000).
  {
    std::vector<uint8_t> buf(4);
    uint32_t inf_bits = 0x7f800000u;
    std::memcpy(buf.data(), &inf_bits, 4);
    bool threw = false;
    try {
      assert_input_finite(buf, "float32-le");
    } catch (const masi::inf::error::Exception&) {
      threw = true;
    }
    CHECK(threw, "assert_input_finite: float32 +Inf must throw");
  }
  // float32-le: -Inf (0xff800000).
  {
    std::vector<uint8_t> buf(4);
    uint32_t neg_inf_bits = 0xff800000u;
    std::memcpy(buf.data(), &neg_inf_bits, 4);
    bool threw = false;
    try {
      assert_input_finite(buf, "float32-le");
    } catch (const masi::inf::error::Exception&) {
      threw = true;
    }
    CHECK(threw, "assert_input_finite: float32 -Inf must throw");
  }
  // float32-le: valid finite value (1.0 = 0x3f800000).
  {
    std::vector<uint8_t> buf(4);
    uint32_t one = 0x3f800000u;
    std::memcpy(buf.data(), &one, 4);
    assert_input_finite(buf, "float32-le");  // should not throw
  }
  // float64-le: NaN.
  {
    std::vector<uint8_t> buf(8);
    uint64_t nan_bits = 0x7ff8000000000000ULL;
    std::memcpy(buf.data(), &nan_bits, 8);
    bool threw = false;
    try {
      assert_input_finite(buf, "float64-le");
    } catch (const masi::inf::error::Exception&) {
      threw = true;
    }
    CHECK(threw, "assert_input_finite: float64 NaN must throw");
  }
  // Unknown dtype.
  {
    std::vector<uint8_t> buf(8, 0);
    bool threw = false;
    try {
      assert_input_finite(buf, "int16-le");
    } catch (const masi::inf::error::Exception&) {
      threw = true;
    }
    CHECK(threw, "assert_input_finite: unknown dtype must throw");
  }
}

// ---------------------------------------------------------------------------
// Test: numeric::assert_output_finite() rejects NaN/Inf in output
// ---------------------------------------------------------------------------
void test_output_finite_rejection() {
  using masi::inf::assert_output_finite;

  // Valid finite.
  assert_output_finite({0.1f, -0.1f, 0.5f});  // should not throw
  // NaN.
  bool threw = false;
  try {
    assert_output_finite({0.1f, std::numeric_limits<float>::quiet_NaN()});
  } catch (const masi::inf::error::Exception&) {
    threw = true;
  }
  CHECK(threw, "assert_output_finite: NaN must throw");
  // Inf.
  threw = false;
  try {
    assert_output_finite({std::numeric_limits<float>::infinity()});
  } catch (const masi::inf::error::Exception&) {
    threw = true;
  }
  CHECK(threw, "assert_output_finite: Inf must throw");
}

// ---------------------------------------------------------------------------
// Test: numeric::float_within_tolerance() basic properties
// ---------------------------------------------------------------------------
void test_float_tolerance() {
  using masi::inf::NumericProfile;
  using masi::inf::float_within_tolerance;

  NumericProfile p;
  p.abs_tol = 1e-6;
  p.rel_tol = 1e-6;
  p.ulp_tol = 4;

  CHECK(float_within_tolerance(1.0f, 1.0f, p) == true,
        "float_within_tolerance: identical values must match");
  CHECK(float_within_tolerance(1.0f, 1.0f + 1e-7f, p) == true,
        "float_within_tolerance: within abs tolerance must match");
  CHECK(float_within_tolerance(0.0f, 1e-7f, p) == true,
        "float_within_tolerance: near-zero within abs tolerance");
  CHECK(float_within_tolerance(1.0f, 2.0f, p) == false,
        "float_within_tolerance: large difference must not match");
  CHECK(float_within_tolerance(std::numeric_limits<float>::quiet_NaN(),
                               0.0f, p) == false,
        "float_within_tolerance: NaN must not match");
}

// ---------------------------------------------------------------------------
// Test: config unknown-field rejection
// ---------------------------------------------------------------------------
void test_config_unknown_field_rejected() {
  TempDir td;
  // Valid minimal config (but startup_envelope_path and model_repository_path
  // must point to existing regular non-symlink files for the full load; we
  // only test the unknown-field rejection path, so we create placeholder
  // files).
  std::string envelope = td.child("envelope.json");
  std::string repo = td.child("modelrepo");
  std::filesystem::create_directory(repo);
  write_temp_file(td.path(), "envelope.json", "{}");
  // Create a placeholder model file so the path is a regular file/dir.
  std::string cfg_json =
      "{"
      "\"startup_envelope_path\":\"" + envelope + "\","
      "\"model_repository_path\":\"" + repo + "\","
      "\"triton_endpoint\":\"127.0.0.1:8001\","
      "\"gateway_listen\":\"0.0.0.0:7443\","
      "\"tls_ca_path\":\"" + td.child("ca.pem") + "\","
      "\"tls_cert_path\":\"" + td.child("cert.pem") + "\","
      "\"tls_key_path\":\"" + td.child("key.pem") + "\","
      "\"runtime_profile\":\"model-runtime-central-cpu/v1\","
      "\"unknown_field\":42"
      "}";
  std::string cfg_path = write_temp_file(td.path(), "config.json", cfg_json);

  bool threw = false;
  std::string msg;
  try {
    masi::inf::load_config(cfg_path);
  } catch (const masi::inf::error::Exception& e) {
    threw = true;
    msg = e.what();
  }
  CHECK(threw, "load_config must throw on unknown field");
  CHECK(msg.find("unknown field") != std::string::npos,
        "load_config error message must mention 'unknown field'");
}

// ---------------------------------------------------------------------------
// Test: config valid (all known fields) loads
// ---------------------------------------------------------------------------
void test_config_valid_loads() {
  TempDir td;
  std::string envelope = td.child("envelope.json");
  std::string repo = td.child("modelrepo");
  std::filesystem::create_directory(repo);
  write_temp_file(td.path(), "envelope.json", "{}");
  write_temp_file(td.path(), "ca.pem", "fake-ca");
  write_temp_file(td.path(), "cert.pem", "fake-cert");
  write_temp_file(td.path(), "key.pem", "fake-key");
  std::string cfg_json =
      "{"
      "\"startup_envelope_path\":\"" + envelope + "\","
      "\"model_repository_path\":\"" + repo + "\","
      "\"triton_endpoint\":\"127.0.0.1:8001\","
      "\"gateway_listen\":\"0.0.0.0:7443\","
      "\"tls_ca_path\":\"" + td.child("ca.pem") + "\","
      "\"tls_cert_path\":\"" + td.child("cert.pem") + "\","
      "\"tls_key_path\":\"" + td.child("key.pem") + "\","
      "\"runtime_profile\":\"model-runtime-central-cpu/v1\","
      "\"max_records_per_batch\":256"
      "}";
  std::string cfg_path = write_temp_file(td.path(), "config.json", cfg_json);

  masi::inf::Config cfg = masi::inf::load_config(cfg_path);
  CHECK(cfg.max_records_per_batch == 256,
        "config max_records_per_batch not loaded");
  CHECK(cfg.runtime_profile == "model-runtime-central-cpu/v1",
        "config runtime_profile not loaded");
}

// ---------------------------------------------------------------------------
// Test: config rejects unsupported runtime profile
// ---------------------------------------------------------------------------
void test_config_unsupported_runtime_profile() {
  TempDir td;
  std::string envelope = td.child("envelope.json");
  std::string repo = td.child("modelrepo");
  std::filesystem::create_directory(repo);
  write_temp_file(td.path(), "envelope.json", "{}");
  std::string cfg_json =
      "{"
      "\"startup_envelope_path\":\"" + envelope + "\","
      "\"model_repository_path\":\"" + repo + "\","
      "\"runtime_profile\":\"tensorrt-future/v1\""
      "}";
  std::string cfg_path = write_temp_file(td.path(), "config.json", cfg_json);

  bool threw = false;
  try {
    masi::inf::load_config(cfg_path);
  } catch (const masi::inf::error::Exception&) {
    threw = true;
  }
  CHECK(threw, "load_config must throw on unsupported runtime profile");
}

// ---------------------------------------------------------------------------
// Test: repository_closure extra-member rejection
// ---------------------------------------------------------------------------
void test_repository_closure_extra_member_rejected() {
  TempDir td;
  std::string root = td.child("repo");
  std::filesystem::create_directory(root);

  // Create closure-manifest.json with one expected member.
  std::string model_path = root + "/model.onnx";
  write_temp_file(root, "model.onnx", "fake-model-bytes");
  // Compute the digest of model.onnx.
  std::string model_bytes = "fake-model-bytes";
  std::string model_digest = masi::inf::sha256_hex(model_bytes.data(), model_bytes.size());

  // closure digest = sha256 over sorted member digests joined by newline.
  std::string closure_input = model_digest + "\n";
  std::string closure_digest = masi::inf::sha256_hex(closure_input.data(), closure_input.size());

  std::string manifest =
      "{"
      "\"identity\":\"repo-test-001\","
      "\"closure_digest\":\"" + closure_digest + "\","
      "\"members\":[{\"rel_path\":\"model.onnx\",\"member_digest\":\"" +
      model_digest + "\",\"role\":\"model\"}]"
      "}";
  write_temp_file(root, "closure-manifest.json", manifest);

  // Add an EXTRA file not in the manifest.
  write_temp_file(root, "extra_config.pbtxt", "unexpected");

  // Make all files AND the root directory read-only (required by verify_repository_closure).
  namespace fs = std::filesystem;
  for (auto& p : fs::recursive_directory_iterator(root)) {
    fs::permissions(p.path(),
                    fs::perms::owner_read | fs::perms::group_read |
                        fs::perms::others_read,
                    fs::perm_options::replace);
  }
  fs::permissions(root,
                  fs::perms::owner_read | fs::perms::owner_exec |
                      fs::perms::group_read | fs::perms::group_exec |
                      fs::perms::others_read | fs::perms::others_exec,
                  fs::perm_options::replace);

  bool threw = false;
  try {
    auto v = masi::inf::verify_repository_closure(root, "repo-test-001",
                                                  closure_digest);
    CHECK(!v.extra.empty(), "verify_repository_closure: extra member not detected");
    masi::inf::assert_closure_ok(v);
  } catch (const masi::inf::error::Exception&) {
    threw = true;
  }
  CHECK(threw, "assert_closure_ok must throw when extra member present");
}

// ---------------------------------------------------------------------------
// Test: repository_closure valid closure passes
// ---------------------------------------------------------------------------
void test_repository_closure_valid() {
  TempDir td;
  std::string root = td.child("repo2");
  std::filesystem::create_directory(root);

  std::string model_bytes = "fake-model-bytes-2";
  std::string model_digest = masi::inf::sha256_hex(model_bytes.data(), model_bytes.size());
  write_temp_file(root, "model.onnx", model_bytes);

  std::string closure_input = model_digest + "\n";
  std::string closure_digest = masi::inf::sha256_hex(closure_input.data(), closure_input.size());

  std::string manifest =
      "{"
      "\"identity\":\"repo-test-002\","
      "\"closure_digest\":\"" + closure_digest + "\","
      "\"members\":[{\"rel_path\":\"model.onnx\",\"member_digest\":\"" +
      model_digest + "\",\"role\":\"model\"}]"
      "}";
  write_temp_file(root, "closure-manifest.json", manifest);

  namespace fs = std::filesystem;
  for (auto& p : fs::recursive_directory_iterator(root)) {
    fs::permissions(p.path(),
                    fs::perms::owner_read | fs::perms::group_read |
                        fs::perms::others_read,
                    fs::perm_options::replace);
  }
  fs::permissions(root,
                  fs::perms::owner_read | fs::perms::owner_exec |
                      fs::perms::group_read | fs::perms::group_exec |
                      fs::perms::others_read | fs::perms::others_exec,
                  fs::perm_options::replace);

  auto v = masi::inf::verify_repository_closure(root, "repo-test-002",
                                                closure_digest);
  CHECK(v.ok, "verify_repository_closure: valid closure should pass");
  CHECK(v.extra.empty(), "verify_repository_closure: valid closure has no extra");
  CHECK(v.missing.empty(), "verify_repository_closure: valid closure has no missing");
  CHECK(v.digest_mismatches.empty(),
        "verify_repository_closure: valid closure has no digest mismatch");
}

// ---------------------------------------------------------------------------
// Test: numeric::apply_output_adapter() softmax conversion
// ---------------------------------------------------------------------------
void test_output_adapter_softmax() {
  using masi::inf::apply_output_adapter;
  using masi::inf::ClassLabel;
  using masi::inf::NumericProfile;
  using masi::inf::NumericResult;

  NumericProfile p;
  p.abs_tol = 1e-6;
  p.rel_tol = 1e-6;
  p.ulp_tol = 4;
  p.ood_threshold = 1.0;
  p.abstain_threshold = 0.0;
  p.alert_threshold = 0.5;
  p.softmax_output = true;
  ClassLabel benign;
  benign.label = 0;
  benign.name = "benign";
  benign.output_index = 0;
  ClassLabel alert;
  alert.label = 1;
  alert.name = "alert";
  alert.output_index = 1;
  p.class_order = {benign, alert};

  // Equal logits -> 0.5/0.5 probabilities, no abstain, BENIGN (max < 0.5).
  {
    NumericResult r = apply_output_adapter({0.0f, 0.0f}, p);
    CHECK(r.scores.size() == 2, "softmax: scores size wrong");
    CHECK(std::fabs(r.scores[0] - 0.5f) < 1e-6 && std::fabs(r.scores[1] - 0.5f) < 1e-6,
          "softmax: equal logits must yield 0.5/0.5");
    CHECK(!r.abstain, "softmax: equal logits must not abstain");
    CHECK(r.decision == "BENIGN", "softmax: equal logits must be BENIGN");
    CHECK(!r.out_of_distribution, "softmax: probabilities cannot be OOD");
  }
  // Logits [-10, 10] -> probabilities ~[0, 1], ALERT (class 1).
  {
    NumericResult r = apply_output_adapter({-10.0f, 10.0f}, p);
    CHECK(r.predicted_label == 1, "softmax: argmax must be class 1");
    CHECK(r.scores[1] > 0.99f, "softmax: class 1 probability must be ~1");
    CHECK(r.decision == "ALERT", "softmax: confident class 1 must be ALERT");
  }
  // Logits [-10, -20] -> class 0 wins, BENIGN.
  {
    NumericResult r = apply_output_adapter({-10.0f, -20.0f}, p);
    CHECK(r.predicted_label == 0, "softmax: argmax must be class 0");
    CHECK(r.decision == "BENIGN", "softmax: confident class 0 must be BENIGN");
  }
  // softmax_output=false keeps raw scores and applies logit-scale thresholds.
  {
    NumericProfile q = p;
    q.softmax_output = false;
    q.ood_threshold = 0.0;
    NumericResult r = apply_output_adapter({1.0f, 2.0f}, q);
    CHECK(r.out_of_distribution, "raw mode: score above OOD threshold is OOD");
    CHECK(r.decision == "ABSTAIN", "raw mode: OOD must abstain");
  }
}

// ---------------------------------------------------------------------------
// Test: admission reject unknown wire profile major
// ---------------------------------------------------------------------------
void test_admission_unknown_major_rejected() {
  masi::inf::Config cfg;
  masi::inf::WireProfile profile;
  // Golden semantics: the batch schema_version is the wire profile id.
  masi::inf::assert_schema_profile("inference-central-grpc-batch/v1",
                                    "inference-central-grpc-batch/v1");
  bool threw = false;
  try {
    masi::inf::assert_schema_profile("inference-central-grpc-batch/v2",
                                      "inference-central-grpc-batch/v2");
  } catch (const masi::inf::error::Exception&) {
    threw = true;
  }
  CHECK(threw, "assert_schema_profile must reject unknown major");
}

// ---------------------------------------------------------------------------
// Test: admission result_fence_dimensions() returns 29 entries
// ---------------------------------------------------------------------------
void test_result_fence_dimensions_count() {
  const auto& dims = masi::inf::result_fence_dimensions();
  CHECK(dims.size() == 29, "result_fence_dimensions() must return 29 entries");
  // Verify a few key entries.
  CHECK(dims[0] == "request_id", "result_fence_dimensions[0] wrong");
  CHECK(dims[28] == "trace_id", "result_fence_dimensions[28] wrong");
  CHECK(dims[12] == "startup_envelope_digest",
        "result_fence_dimensions[12] wrong");
}

// ---------------------------------------------------------------------------
// Test: admission oversize batch rejected
// ---------------------------------------------------------------------------
void test_admission_oversize_rejected() {
  masi::inf::Config cfg;
  cfg.max_records_per_batch = 256;
  cfg.max_request_bytes = 4194304;
  masi::inf::WireProfile profile;
  std::vector<uint8_t> feature(48, 0);
  auto d = masi::inf::admit(
      cfg, profile, "req-001", "inference-central-grpc-batch/v1",
      "inference-central-grpc-batch/v1", 9999999999999LL,
      profile.maximum_request_bytes + 1, 1, "shard-001", 1, 1, 1,
      "inc-001", feature, {1, 6}, "uint64-le", 8);
  CHECK(d.verdict == masi::inf::AdmissionVerdict::kHold,
        "admission: oversize request must be HOLD");
  CHECK(d.reason_code == "INFERENCE_MESSAGE_TOO_LARGE",
        "admission: oversize reason code wrong");
}

}  // namespace

int main() {
  std::cout << "=== Central Inference property invariant tests ===\n";
  test_error_codes_stable();
  test_sha256_format();
  test_digest_match();
  test_tensor_layout_checks();
  test_nan_inf_rejection();
  test_output_finite_rejection();
  test_float_tolerance();
  test_output_adapter_softmax();
  test_config_unknown_field_rejected();
  test_config_valid_loads();
  test_config_unsupported_runtime_profile();
  test_repository_closure_extra_member_rejected();
  test_repository_closure_valid();
  test_admission_unknown_major_rejected();
  test_result_fence_dimensions_count();
  test_admission_oversize_rejected();
  std::cout << "All property invariant tests passed.\n";
  return 0;
}