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
#include <functional>
#include <iostream>
#include <limits>
#include <string>
#include <vector>

#include "support/mod.h"

#include "admission.h"
#include "config.h"
#include "digest.h"
#include "error.h"
#include "gateway.h"
#include "numeric.h"
#include "repository_closure.h"
#include "startup.h"

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
    const char *expected;
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

  for (const auto &e : entries) {
    std::string got = to_string(e.code);
    CHECK(got == e.expected, "error::to_string(" + std::to_string(static_cast<int>(e.code)) +
                                 ") = '" + got + "' expected '" + e.expected + "'");
  }

  // Verify all code strings are distinct (no aliasing).
  std::vector<std::string> strings;
  for (const auto &e : entries)
    strings.push_back(e.expected);
  for (size_t i = 0; i < strings.size(); ++i) {
    for (size_t j = i + 1; j < strings.size(); ++j) {
      CHECK(strings[i] != strings[j], "duplicate error code string: " + strings[i]);
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
  CHECK(masi::inf::error::retryable(Code::kAborted) == false, "kAborted must not be retryable");
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
  CHECK(masi::inf::is_sha256_digest(d), "known digest must validate");
  CHECK(!masi::inf::is_sha256_digest("sha256:"), "empty digest body must reject");
  CHECK(!masi::inf::is_sha256_digest("sha256:" + std::string(63, '0')), "short digest must reject");
  CHECK(!masi::inf::is_sha256_digest("sha256:" + std::string(64, 'A')),
        "uppercase digest must reject");
  CHECK(!masi::inf::is_sha256_digest("sha256:" + std::string(64, 'z')),
        "non-hex digest must reject");
  CHECK(!masi::inf::is_sha256_digest("sha256:" + std::string(64, '0')),
        "all-zero digest sentinel must reject");
}

// ---------------------------------------------------------------------------
// Test: digest::digest_match() constant-time compare
// ---------------------------------------------------------------------------
void test_digest_match() {
  std::string a = "sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789";
  const std::string &b = a;
  CHECK(masi::inf::digest_match(a, b) == true, "digest_match: identical digests must match");

  // Single-char difference.
  std::string c = a;
  c[c.size() - 1] = (c[c.size() - 1] == '0') ? '1' : '0';
  CHECK(masi::inf::digest_match(a, c) == false,
        "digest_match: single-char difference must not match");

  // Different length.
  std::string d = "sha256:abc";
  CHECK(masi::inf::digest_match(a, d) == false, "digest_match: different length must not match");

  // Both empty.
  std::string e;
  std::string f;
  CHECK(masi::inf::digest_match(e, f) == true, "digest_match: two empty strings must match");

  // One empty, one non-empty.
  CHECK(masi::inf::digest_match(a, e) == false, "digest_match: one empty must not match");
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
  CHECK(misaligned.reason == "alignment", "check_tensor_layout: misaligned reason wrong");

  // Unknown dtype.
  auto bad_dtype = check_tensor_layout({1, 6}, "int8-le", 1);
  CHECK(!bad_dtype.ok, "check_tensor_layout: unknown dtype should be rejected");
  CHECK(bad_dtype.reason == "unknown dtype", "check_tensor_layout: unknown dtype reason wrong");

  // Empty shape.
  auto empty_shape = check_tensor_layout({}, "uint64-le", 8);
  CHECK(!empty_shape.ok, "check_tensor_layout: empty shape should be rejected");
  CHECK(empty_shape.reason == "empty shape", "check_tensor_layout: empty shape reason wrong");

  // Zero dim.
  auto zero_dim = check_tensor_layout({1, 0, 6}, "uint64-le", 8);
  CHECK(!zero_dim.ok, "check_tensor_layout: zero dim should be rejected");
  CHECK(zero_dim.reason == "zero dim", "check_tensor_layout: zero dim reason wrong");

  // Shape overflow: two very large dims whose product overflows size_t.
  uint32_t big = 0x80000000u;
  auto overflow = check_tensor_layout({big, big}, "uint64-le", 8);
  CHECK(!overflow.ok, "check_tensor_layout: shape overflow should be rejected");
  CHECK(overflow.reason == "shape overflow" || overflow.reason == "element count overflow",
        "check_tensor_layout: shape overflow reason wrong");

  // Floating-point representations are not part of the frozen wire profile.
  auto f32 = check_tensor_layout({1, 6}, "float32-le", 4);
  CHECK(!f32.ok, "check_tensor_layout: float32-le must be rejected");

  auto f64 = check_tensor_layout({1, 6}, "float64-le", 8);
  CHECK(!f64.ok, "check_tensor_layout: float64-le must be rejected");
}

// ---------------------------------------------------------------------------
// Test: numeric::assert_input_finite() rejects NaN and Inf
// ---------------------------------------------------------------------------
void test_nan_inf_rejection() {
  using masi::inf::assert_input_finite;

  // uint64-le: cannot represent NaN/Inf; valid buffer passes.
  {
    std::vector<uint8_t> buf(48, 0);
    assert_input_finite(buf, "uint64-le"); // should not throw
  }
  // uint64-le: misaligned length (not multiple of 8).
  {
    std::vector<uint8_t> buf(7, 0);
    bool threw = false;
    try {
      assert_input_finite(buf, "uint64-le");
    } catch (const masi::inf::error::Exception &) {
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
    } catch (const masi::inf::error::Exception &) {
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
    } catch (const masi::inf::error::Exception &) {
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
    } catch (const masi::inf::error::Exception &) {
      threw = true;
    }
    CHECK(threw, "assert_input_finite: float32 -Inf must throw");
  }
  // float32-le: valid finite value (1.0 = 0x3f800000).
  {
    std::vector<uint8_t> buf(4);
    uint32_t one = 0x3f800000u;
    std::memcpy(buf.data(), &one, 4);
    assert_input_finite(buf, "float32-le"); // should not throw
  }
  // float64-le: NaN.
  {
    std::vector<uint8_t> buf(8);
    uint64_t nan_bits = 0x7ff8000000000000ULL;
    std::memcpy(buf.data(), &nan_bits, 8);
    bool threw = false;
    try {
      assert_input_finite(buf, "float64-le");
    } catch (const masi::inf::error::Exception &) {
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
    } catch (const masi::inf::error::Exception &) {
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
  assert_output_finite({0.1f, -0.1f, 0.5f}); // should not throw
  // NaN.
  bool threw = false;
  try {
    assert_output_finite({0.1f, std::numeric_limits<float>::quiet_NaN()});
  } catch (const masi::inf::error::Exception &) {
    threw = true;
  }
  CHECK(threw, "assert_output_finite: NaN must throw");
  // Inf.
  threw = false;
  try {
    assert_output_finite({std::numeric_limits<float>::infinity()});
  } catch (const masi::inf::error::Exception &) {
    threw = true;
  }
  CHECK(threw, "assert_output_finite: Inf must throw");
}

// ---------------------------------------------------------------------------
// Test: numeric::float_within_tolerance() basic properties
// ---------------------------------------------------------------------------
void test_float_tolerance() {
  using masi::inf::float_within_tolerance;
  using masi::inf::NumericProfile;

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
  CHECK(float_within_tolerance(std::numeric_limits<float>::quiet_NaN(), 0.0f, p) == false,
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
  std::string cfg_json = "{"
                         "\"startup_envelope_path\":\"" +
                         envelope +
                         "\","
                         "\"model_repository_path\":\"" +
                         repo +
                         "\","
                         "\"triton_endpoint\":\"127.0.0.1:8001\","
                         "\"gateway_listen\":\"0.0.0.0:7443\","
                         "\"tls_ca_path\":\"" +
                         td.child("ca.pem") +
                         "\","
                         "\"tls_cert_path\":\"" +
                         td.child("cert.pem") +
                         "\","
                         "\"tls_key_path\":\"" +
                         td.child("key.pem") +
                         "\","
                         "\"runtime_profile\":\"model-runtime-central-cpu/v1\","
                         "\"unknown_field\":42"
                         "}";
  std::string cfg_path = write_temp_file(td.path(), "config.json", cfg_json);

  bool threw = false;
  std::string msg;
  try {
    masi::inf::load_config(cfg_path);
  } catch (const masi::inf::error::Exception &e) {
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
  std::string cfg_json = "{"
                         "\"startup_envelope_path\":\"" +
                         envelope +
                         "\","
                         "\"model_repository_path\":\"" +
                         repo +
                         "\","
                         "\"triton_endpoint\":\"127.0.0.1:8001\","
                         "\"gateway_listen\":\"0.0.0.0:7443\","
                         "\"tls_ca_path\":\"" +
                         td.child("ca.pem") +
                         "\","
                         "\"tls_cert_path\":\"" +
                         td.child("cert.pem") +
                         "\","
                         "\"tls_key_path\":\"" +
                         td.child("key.pem") +
                         "\","
                         "\"runtime_profile\":\"model-runtime-central-cpu/v1\","
                         "\"client_san_allowlist\":[\"masi-edge.test\"],"
                         "\"max_records_per_batch\":256"
                         "}";
  std::string cfg_path = write_temp_file(td.path(), "config.json", cfg_json);

  masi::inf::Config cfg = masi::inf::load_config(cfg_path);
  CHECK(cfg.max_records_per_batch == 256, "config max_records_per_batch not loaded");
  CHECK(cfg.runtime_profile == "model-runtime-central-cpu/v1", "config runtime_profile not loaded");
}

// ---------------------------------------------------------------------------
// Test: config rejects a missing client SAN allowlist
// ---------------------------------------------------------------------------
void test_config_requires_client_san_allowlist() {
  TempDir td;
  std::string envelope = td.child("envelope.json");
  std::string repo = td.child("modelrepo");
  std::filesystem::create_directory(repo);
  write_temp_file(td.path(), "envelope.json", "{}");
  std::string cfg_json = "{"
                         "\"startup_envelope_path\":\"" +
                         envelope +
                         "\","
                         "\"model_repository_path\":\"" +
                         repo +
                         "\","
                         "\"runtime_profile\":\"model-runtime-central-cpu/v1\""
                         "}";
  std::string cfg_path = write_temp_file(td.path(), "config-no-san.json", cfg_json);

  bool threw = false;
  std::string msg;
  try {
    masi::inf::load_config(cfg_path);
  } catch (const masi::inf::error::Exception &e) {
    threw = true;
    msg = e.what();
  }
  CHECK(threw, "load_config must reject a config without client_san_allowlist");
  CHECK(msg.find("client_san_allowlist") != std::string::npos,
        "load_config rejection must name client_san_allowlist: " + msg);
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
  std::string cfg_json = "{"
                         "\"startup_envelope_path\":\"" +
                         envelope +
                         "\","
                         "\"model_repository_path\":\"" +
                         repo +
                         "\","
                         "\"runtime_profile\":\"tensorrt-future/v1\""
                         "}";
  std::string cfg_path = write_temp_file(td.path(), "config.json", cfg_json);

  bool threw = false;
  try {
    masi::inf::load_config(cfg_path);
  } catch (const masi::inf::error::Exception &) {
    threw = true;
  }
  CHECK(threw, "load_config must throw on unsupported runtime profile");
}

void test_config_resource_ceilings() {
  TempDir td;
  nlohmann::json base = {
      {"startup_envelope_path", td.child("envelope.json")},
      {"model_repository_path", td.child("modelrepo")},
      {"client_san_allowlist", nlohmann::json::array({"masi-edge.test"})},
      {"max_records_per_batch", 256},
      {"max_request_bytes", 4194304},
      {"max_response_bytes", 4194304},
      {"max_in_flight", 64},
      {"request_deadline_ms", 2000},
      {"drain_ms", 5000},
  };
  write_temp_file(td.path(), "envelope.json", "{}");
  std::filesystem::create_directory(td.child("modelrepo"));

  int serial = 0;
  auto rejects = [&](const std::string &field, int64_t value) {
    nlohmann::json candidate = base;
    candidate[field] = value;
    const std::string path = write_temp_file(
        td.path(), "config-bound-" + std::to_string(++serial) + ".json", candidate.dump());
    try {
      (void)masi::inf::load_config(path);
    } catch (const masi::inf::error::Exception &) {
      return true;
    }
    return false;
  };

  for (const auto &[field, ceiling] : std::vector<std::pair<std::string, int64_t>>{
           {"max_records_per_batch", 256},
           {"max_request_bytes", 4194304},
           {"max_response_bytes", 4194304},
           {"max_in_flight", 64},
           {"request_deadline_ms", 2000},
           {"drain_ms", 30000},
       }) {
    CHECK(rejects(field, 0), field + " zero must reject");
    CHECK(rejects(field, -1), field + " negative must reject");
    CHECK(rejects(field, ceiling + 1), field + " above frozen ceiling must reject");
  }
  CHECK(rejects("intra_op_num_threads", -1), "negative intra-op threads must reject");
  CHECK(rejects("inter_op_num_threads", 513), "excess inter-op threads must reject");
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

  // closure digest preimage binds role and path, not only content:
  // sorted "role\trel_path\tmember_digest" lines, newline terminated.
  std::string closure_input = "model\tmodel.onnx\t" + model_digest + "\n";
  std::string closure_digest = masi::inf::sha256_hex(closure_input.data(), closure_input.size());

  std::string manifest = "{"
                         "\"identity\":\"repo-test-001\","
                         "\"closure_digest\":\"" +
                         closure_digest +
                         "\","
                         "\"members\":[{\"rel_path\":\"model.onnx\",\"member_digest\":\"" +
                         model_digest +
                         "\",\"role\":\"model\"}]"
                         "}";
  write_temp_file(root, "closure-manifest.json", manifest);

  // Add an EXTRA file not in the manifest.
  write_temp_file(root, "extra_config.pbtxt", "unexpected");

  // Make all files AND the root directory read-only (required by verify_repository_closure).
  namespace fs = std::filesystem;
  for (auto &p : fs::recursive_directory_iterator(root)) {
    fs::permissions(p.path(),
                    fs::perms::owner_read | fs::perms::group_read | fs::perms::others_read,
                    fs::perm_options::replace);
  }
  fs::permissions(root,
                  fs::perms::owner_read | fs::perms::owner_exec | fs::perms::group_read |
                      fs::perms::group_exec | fs::perms::others_read | fs::perms::others_exec,
                  fs::perm_options::replace);

  bool threw = false;
  try {
    auto v = masi::inf::verify_repository_closure(root, "repo-test-001", closure_digest);
    CHECK(!v.extra.empty(), "verify_repository_closure: extra member not detected");
    masi::inf::assert_closure_ok(v);
  } catch (const masi::inf::error::Exception &) {
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

  std::string closure_input = "model\tmodel.onnx\t" + model_digest + "\n";
  std::string closure_digest = masi::inf::sha256_hex(closure_input.data(), closure_input.size());

  std::string manifest = "{"
                         "\"identity\":\"repo-test-002\","
                         "\"closure_digest\":\"" +
                         closure_digest +
                         "\","
                         "\"members\":[{\"rel_path\":\"model.onnx\",\"member_digest\":\"" +
                         model_digest +
                         "\",\"role\":\"model\"}]"
                         "}";
  write_temp_file(root, "closure-manifest.json", manifest);

  namespace fs = std::filesystem;
  for (auto &p : fs::recursive_directory_iterator(root)) {
    fs::permissions(p.path(),
                    fs::perms::owner_read | fs::perms::group_read | fs::perms::others_read,
                    fs::perm_options::replace);
  }
  fs::permissions(root,
                  fs::perms::owner_read | fs::perms::owner_exec | fs::perms::group_read |
                      fs::perms::group_exec | fs::perms::others_read | fs::perms::others_exec,
                  fs::perm_options::replace);

  auto v = masi::inf::verify_repository_closure(root, "repo-test-002", closure_digest);
  CHECK(v.ok, "verify_repository_closure: valid closure should pass");
  CHECK(v.extra.empty(), "verify_repository_closure: valid closure has no extra");
  CHECK(v.missing.empty(), "verify_repository_closure: valid closure has no missing");
  CHECK(v.digest_mismatches.empty(),
        "verify_repository_closure: valid closure has no digest mismatch");
}

// ---------------------------------------------------------------------------
// Test: closure digest binds role and path, not only member content
// ---------------------------------------------------------------------------
void test_repository_closure_digest_binds_path_and_role() {
  namespace fs = std::filesystem;
  TempDir td;
  const std::string bytes = "same-bytes-different-place";
  const std::string member_digest = masi::inf::sha256_hex(bytes.data(), bytes.size());

  auto build = [&](const std::string &dir_name, const std::string &rel_path,
                   const std::string &role) {
    const std::string root = td.child(dir_name);
    fs::create_directories(root + "/" + fs::path(rel_path).parent_path().string());
    write_temp_file(root + "/" + fs::path(rel_path).parent_path().string(),
                    fs::path(rel_path).filename().string(), bytes);
    const std::string preimage = role + "\t" + rel_path + "\t" + member_digest + "\n";
    const std::string closure_digest = masi::inf::sha256_hex(preimage.data(), preimage.size());
    const std::string manifest = "{"
                                 "\"identity\":\"repo-path-bind\","
                                 "\"closure_digest\":\"" +
                                 closure_digest +
                                 "\","
                                 "\"members\":[{\"rel_path\":\"" +
                                 rel_path + "\",\"member_digest\":\"" + member_digest +
                                 "\",\"role\":\"" + role +
                                 "\"}]"
                                 "}";
    write_temp_file(root, "closure-manifest.json", manifest);
    for (auto &p : fs::recursive_directory_iterator(root)) {
      if (p.is_regular_file())
        fs::permissions(p.path(),
                        fs::perms::owner_read | fs::perms::group_read | fs::perms::others_read,
                        fs::perm_options::replace);
    }
    for (auto &p : fs::recursive_directory_iterator(root)) {
      if (p.is_directory())
        fs::permissions(p.path(),
                        fs::perms::owner_read | fs::perms::owner_exec | fs::perms::group_read |
                            fs::perms::group_exec | fs::perms::others_read | fs::perms::others_exec,
                        fs::perm_options::replace);
    }
    fs::permissions(root,
                    fs::perms::owner_read | fs::perms::owner_exec | fs::perms::group_read |
                        fs::perms::group_exec | fs::perms::others_read | fs::perms::others_exec,
                    fs::perm_options::replace);
    auto v = masi::inf::verify_repository_closure(root, "repo-path-bind", closure_digest);
    return std::make_pair(closure_digest, v.observed_closure_digest);
  };

  const auto a = build("bind_a", "m/1/model.onnx", "model");
  const auto b = build("bind_b", "m/2/model.onnx", "model");
  CHECK(a.first == a.second, "closure digest: observed must match the manifest (a)");
  CHECK(b.first == b.second, "closure digest: observed must match the manifest (b)");
  CHECK(a.second != b.second,
        "closure digest must change when identical bytes move to another path");
}

// ---------------------------------------------------------------------------
// Test: bundle-manifest adapter data configuration self-verification
// ---------------------------------------------------------------------------
void test_bundle_manifest_loads_and_self_verifies() {
  namespace fs = std::filesystem;
  const std::string repo =
      masi::inf::test::repo_root() + "/testkit/fixtures/repositories/masi-ids-window-v1-r3";
  if (!fs::is_directory(repo)) {
    std::cout << "  (skip bundle-manifest test: fixture repository missing)\n";
    return;
  }
  const auto b = masi::inf::load_bundle_manifest(repo);
  CHECK(b.adapter_id == "masi-window-adapter-v1", "bundle: adapter_id wrong");
  CHECK(b.score_domain == "logit", "bundle: score_domain wrong");
  CHECK(b.class_order.size() == 2 && b.class_order[0] == 0 && b.class_order[1] == 1,
        "bundle: class_order wrong");

  TempDir zero_td;
  const std::string zero_root = zero_td.child("repo_zero_revision");
  fs::copy(repo, zero_root, fs::copy_options::recursive);
  for (auto &p : fs::recursive_directory_iterator(zero_root))
    fs::permissions(p.path(), fs::perms::owner_all, fs::perm_options::add);
  const std::string zero_path = zero_root + "/bundle-manifest.json";
  auto zero_bundle = b;
  zero_bundle.revision = std::string(40, '0');
  std::ifstream zero_input(zero_path, std::ios::binary);
  auto zero_json = nlohmann::json::parse(zero_input);
  zero_json["revision"] = zero_bundle.revision;
  zero_json["binding_identity"]["revision"] = zero_bundle.revision;
  zero_json["model_revision_digest"] = masi::inf::compute_model_revision_digest(zero_bundle);
  {
    std::ofstream output(zero_path, std::ios::binary | std::ios::trunc);
    output << zero_json.dump(2) << '\n';
  }
  bool zero_threw = false;
  try {
    masi::inf::load_bundle_manifest(zero_root);
  } catch (const masi::inf::error::Exception &) {
    zero_threw = true;
  }
  CHECK(zero_threw, "bundle: all-zero revision must be rejected even with a matching digest");
  CHECK(b.top_k == 1 && b.axis == 1, "bundle: top_k/axis wrong");
  CHECK(std::fabs(b.alert_threshold - 0.5) < 1e-12, "bundle: alert threshold wrong");
  CHECK(b.output_adapter_digest == b.adapter_digest,
        "bundle: output_adapter_digest must equal adapter_digest");
  CHECK(b.triton_max_batch_size == 256, "bundle: declared max_batch_size wrong");
  CHECK(b.triton_max_queue_size == 1024, "bundle: declared max_queue_size wrong");
  CHECK(b.triton_instance_group_kind == "KIND_CPU", "bundle: instance_group kind wrong");
  CHECK(b.ood_mode == "max-probability-below-threshold", "bundle: OOD policy mode wrong");
  CHECK(std::fabs(b.ood_below - 0.55) < 1e-12, "bundle: OOD threshold wrong");
  CHECK(b.model_revision_digest == masi::inf::compute_model_revision_digest(b),
        "bundle: model revision preimage must verify");
  CHECK(b.runtime_profile_digest == MASI_INF_RUNTIME_PROFILE_DIGEST,
        "bundle: runtime profile preimage must verify");
  CHECK(b.optimization_profile_digest == MASI_INF_OPTIMIZATION_PROFILE_DIGEST,
        "bundle: optimization profile preimage must verify");

  const auto profile = masi::inf::make_numeric_profile(b);
  CHECK(profile.score_domain == b.score_domain, "numeric profile: score_domain not bound");
  CHECK(profile.class_order == b.class_order, "numeric profile: class_order not bound");

  // A tampered adapter parameter with a stale digest must be rejected.
  TempDir td;
  const std::string copy_root = td.child("repo_tampered");
  fs::copy(repo, copy_root, fs::copy_options::recursive);
  for (auto &p : fs::recursive_directory_iterator(copy_root))
    fs::permissions(p.path(), fs::perms::owner_all, fs::perm_options::add);
  const std::string bundle_path = copy_root + "/bundle-manifest.json";
  std::string text;
  {
    std::ifstream f(bundle_path, std::ios::binary);
    text.assign((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
  }
  const std::string needle = "\"threshold\": 0.5";
  const auto pos = text.find(needle);
  CHECK(pos != std::string::npos, "bundle: expected adapter threshold literal not found");
  text.replace(pos, needle.size(), "\"threshold\": 0.9");
  {
    std::ofstream f(bundle_path, std::ios::binary | std::ios::trunc);
    f << text;
  }
  bool threw = false;
  try {
    masi::inf::load_bundle_manifest(copy_root);
  } catch (const masi::inf::error::Exception &) {
    threw = true;
  }
  CHECK(threw, "bundle: tampered adapter parameter with a stale digest must be rejected");
}

// ---------------------------------------------------------------------------
// Test: numeric::apply_output_adapter() data-driven adapter semantics
// ---------------------------------------------------------------------------
namespace {

masi::inf::NumericProfile logit_profile() {
  masi::inf::NumericProfile p;
  p.adapter_id = "masi-window-adapter-v1";
  p.adapter_digest = "sha256:" + std::string(64, '1');
  p.score_domain = "logit";
  p.class_order = {0, 1};
  p.alert_threshold = 0.5;
  p.abstain_below = 0.0;
  return p;
}

} // namespace

void test_output_adapter_semantics() {
  using masi::inf::apply_output_adapter;
  using masi::inf::NumericProfile;
  using masi::inf::NumericResult;

  const NumericProfile p = logit_profile();

  // Equal logits -> 0.5/0.5 probabilities; the baseline label (class_order[0])
  // wins ties, so the decision is benign.
  {
    NumericResult r = apply_output_adapter({0.0f, 0.0f}, p);
    CHECK(r.scores.size() == 2, "adapter: scores size wrong");
    CHECK(std::fabs(r.scores[0] - 0.5f) < 1e-6 && std::fabs(r.scores[1] - 0.5f) < 1e-6,
          "adapter: equal logits must yield 0.5/0.5");
    CHECK(!r.abstain, "adapter: equal logits must not abstain");
    CHECK(r.decision == "benign", "adapter: tie must resolve to the baseline label");
    CHECK(r.predicted_label == 0, "adapter: tie must predict the baseline label");
    CHECK(!r.out_of_distribution, "adapter: default OOD floor must not flag");
    CHECK(r.quality == "valid", "adapter: quality must be the lowercase contract value");
  }
  // The qualified OOD rule is live and fail closed: an ambiguous 0.5/0.5 row
  // below the 0.51 floor is flagged and forced to abstain.
  {
    NumericProfile q = p;
    q.ood_below = 0.51;
    NumericResult r = apply_output_adapter({0.0f, 0.0f}, q);
    CHECK(r.out_of_distribution, "adapter: ambiguous row must be OOD");
    CHECK(r.abstain && r.decision == "abstain", "adapter: OOD must force deterministic abstention");
  }
  // Logits [-10, 10] -> probabilities ~[0, 1] for the non-baseline label.
  {
    NumericResult r = apply_output_adapter({-10.0f, 10.0f}, p);
    CHECK(r.predicted_label == 1, "adapter: argmax must be label 1");
    CHECK(r.scores[1] > 0.99f, "adapter: label 1 probability must be ~1");
    CHECK(r.decision == "alert", "adapter: confident non-baseline label must alert");
  }
  // Logits [-10, -20] -> the baseline label wins.
  {
    NumericResult r = apply_output_adapter({-10.0f, -20.0f}, p);
    CHECK(r.predicted_label == 0, "adapter: argmax must be label 0");
    CHECK(r.decision == "benign", "adapter: baseline label must be benign");
  }
  // score_domain=probability applies no normalization.
  {
    NumericProfile q = p;
    q.score_domain = "probability";
    NumericResult r = apply_output_adapter({0.25f, 0.75f}, q);
    CHECK(std::fabs(r.scores[0] - 0.25f) < 1e-6 && std::fabs(r.scores[1] - 0.75f) < 1e-6,
          "adapter: probability domain must not normalize");
    CHECK(r.decision == "alert", "adapter: 0.75 >= threshold must alert");
  }
  // The alert threshold is live, not vacuous: a three-class distribution whose
  // winning non-baseline probability is below the threshold stays benign.
  {
    NumericProfile q = p;
    q.class_order = {0, 1, 2};
    q.alert_threshold = 0.6;
    NumericResult r = apply_output_adapter({0.0f, 0.5f, 0.4f}, q);
    CHECK(r.predicted_label == 1, "adapter: three-class argmax must be label 1");
    CHECK(r.scores[1] < 0.6f, "adapter: winning probability must be below threshold");
    CHECK(r.decision == "benign",
          "adapter: non-baseline label below the alert threshold must be benign");
  }
  // The abstain floor is live.
  {
    NumericProfile q = p;
    q.class_order = {0, 1, 2};
    q.abstain_below = 0.9;
    NumericResult r = apply_output_adapter({0.0f, 0.5f, 0.4f}, q);
    CHECK(r.abstain, "adapter: top-1 below the abstain floor must abstain");
    CHECK(r.decision == "abstain", "adapter: abstain decision string wrong");
  }
  // Non-baseline class order is honoured: class_order[0] is the baseline.
  {
    NumericProfile q = p;
    q.class_order = {1, 0};
    NumericResult r = apply_output_adapter({-10.0f, 10.0f}, q);
    CHECK(r.predicted_label == 1, "adapter: label 1 still wins on raw score");
    CHECK(r.decision == "benign", "adapter: label 1 is the baseline under this class order");
  }
  // Unsupported score domains fail closed.
  {
    NumericProfile q = p;
    q.score_domain = "distance";
    bool threw = false;
    try {
      apply_output_adapter({0.1f, 0.2f}, q);
    } catch (const masi::inf::error::Exception &) {
      threw = true;
    }
    CHECK(threw, "adapter: unsupported score_domain must be rejected");
  }
}

// ---------------------------------------------------------------------------
// Test: admission reject unknown wire profile major
// ---------------------------------------------------------------------------
void test_admission_unknown_major_rejected() {
  // Golden semantics: the batch schema_version is the wire profile id.
  masi::inf::assert_schema_profile("inference-central-grpc-batch/v1",
                                   "inference-central-grpc-batch/v1");
  bool threw = false;
  try {
    masi::inf::assert_schema_profile("inference-central-grpc-batch/v2",
                                     "inference-central-grpc-batch/v2");
  } catch (const masi::inf::error::Exception &) {
    threw = true;
  }
  CHECK(threw, "assert_schema_profile must reject unknown major");
}

// ---------------------------------------------------------------------------
// Test: admission result_fence_dimensions() returns 29 entries
// ---------------------------------------------------------------------------
void test_result_fence_dimensions_count() {
  const auto &dims = masi::inf::result_fence_dimensions();
  CHECK(dims.size() == 29, "result_fence_dimensions() must return 29 entries");
  // Verify a few key entries.
  CHECK(dims[0] == "request_id", "result_fence_dimensions[0] wrong");
  CHECK(dims[28] == "trace_id", "result_fence_dimensions[28] wrong");
  CHECK(dims[12] == "startup_envelope_digest", "result_fence_dimensions[12] wrong");
}

std::string fixed_digest(char c) { return "sha256:" + std::string(64, c); }

masi::inf::PoolReadback fence_readback() {
  masi::inf::PoolReadback rb;
  rb.model_control_incarnation_id = "inc-fence-1";
  rb.operation_id = "operation-fence-1";
  rb.logical_pool_id = "pool-fence-1";
  rb.pool_generation = 4;
  rb.proposed_binding_generation = 9;
  rb.startup_envelope_digest = fixed_digest('1');
  rb.model_revision_digest = fixed_digest('2');
  rb.model_bundle_digest = fixed_digest('3');
  rb.feature_contract_digest = fixed_digest('4');
  rb.label_contract_digest = fixed_digest('5');
  rb.output_adapter_digest = fixed_digest('6');
  rb.wire_profile = "inference-central-grpc-batch/v1";
  rb.wire_profile_digest = fixed_digest('7');
  rb.runtime_profile = "model-runtime-central-cpu/v1";
  rb.runtime_profile_digest = fixed_digest('8');
  rb.optimization_profile_digest = fixed_digest('9');
  rb.pool_observation_digest = fixed_digest('a');
  rb.binding_digest = fixed_digest('b');
  return rb;
}

masi::edge::v1::InferenceRoute fenced_route(const masi::inf::PoolReadback &rb) {
  masi::edge::v1::InferenceRoute route;
  route.set_schema_version("inference-route/v1");
  route.set_shard_id("shard-fence-1");
  route.set_model_control_incarnation_id(rb.model_control_incarnation_id);
  route.set_logical_pool_id(rb.logical_pool_id);
  route.set_pool_generation(rb.pool_generation);
  route.set_binding_generation(rb.proposed_binding_generation);
  route.set_route_epoch(3);
  route.set_model_revision_digest(rb.model_revision_digest);
  route.set_feature_contract_digest(rb.feature_contract_digest);
  route.set_label_contract_digest(rb.label_contract_digest);
  route.set_output_adapter_digest(rb.output_adapter_digest);
  route.set_wire_profile(rb.wire_profile);
  route.set_runtime_profile(rb.runtime_profile);
  route.set_operation_id(rb.operation_id);
  route.set_scope("scope-fence-1");
  route.set_expected_binding_generation(rb.proposed_binding_generation - 1);
  route.set_proposed_binding_generation(rb.proposed_binding_generation);
  route.set_current_binding_generation(rb.proposed_binding_generation);
  route.set_startup_envelope_digest(rb.startup_envelope_digest);
  route.set_pool_observation_digest(rb.pool_observation_digest);
  route.set_binding_digest(rb.binding_digest);
  route.set_model_bundle_digest(rb.model_bundle_digest);
  route.set_wire_profile_digest(rb.wire_profile_digest);
  route.set_runtime_profile_digest(rb.runtime_profile_digest);
  route.set_optimization_profile_digest(rb.optimization_profile_digest);
  return route;
}

masi::edge::v1::InferenceRecord fenced_record(const masi::edge::v1::InferenceRoute &route) {
  masi::edge::v1::InferenceRecord rec;
  rec.set_input_id("input-fence-1");
  rec.set_event_idempotency_key("event-fence-1");
  rec.set_target_id(route.shard_id());
  rec.set_source_runtime_epoch("source-runtime-fence-1");
  rec.set_source_sequence_start(10);
  rec.set_source_sequence_end(10);
  rec.set_window_start_unix_ms(1000);
  rec.set_window_end_unix_ms(2000);
  rec.set_watermark_unix_ms(2000);
  rec.set_finalized_at_unix_ms(2001);
  rec.set_enqueued_at_unix_ms(2002);
  rec.set_quality("valid");
  rec.set_quality_code(masi::edge::v1::DATA_QUALITY_VALID);
  rec.add_quality_reasons("NONE");
  rec.set_final_window(true);
  rec.set_sampling_coverage_ppm(1000000);
  rec.set_window_id("window-fence-1");
  rec.set_schema_version("edge-inference-record/v1");
  rec.set_source_wal_sequence(7);
  rec.set_input_wal_sequence(8);
  rec.set_input_digest(fixed_digest('c'));
  rec.set_model_control_incarnation_id(route.model_control_incarnation_id());
  rec.set_logical_pool_id(route.logical_pool_id());
  rec.set_pool_generation(route.pool_generation());
  rec.set_binding_generation(route.binding_generation());
  rec.set_route_epoch(route.route_epoch());
  rec.set_model_revision_digest(route.model_revision_digest());
  rec.set_feature_contract_digest(route.feature_contract_digest());
  rec.set_label_contract_digest(route.label_contract_digest());
  rec.set_output_adapter_digest(route.output_adapter_digest());
  rec.set_wire_profile(route.wire_profile());
  rec.set_runtime_profile(route.runtime_profile());
  rec.set_operation_id(route.operation_id());
  rec.set_scope(route.scope());
  rec.set_expected_binding_generation(route.expected_binding_generation());
  rec.set_proposed_binding_generation(route.proposed_binding_generation());
  rec.set_current_binding_generation(route.current_binding_generation());
  rec.set_startup_envelope_digest(route.startup_envelope_digest());
  rec.set_pool_observation_digest(route.pool_observation_digest());
  rec.set_binding_digest(route.binding_digest());
  rec.set_model_bundle_digest(route.model_bundle_digest());
  rec.set_wire_profile_digest(route.wire_profile_digest());
  rec.set_runtime_profile_digest(route.runtime_profile_digest());
  rec.set_optimization_profile_digest(route.optimization_profile_digest());
  return rec;
}

void test_complete_route_and_record_fence() {
  const auto rb = fence_readback();
  const auto route = fenced_route(rb);
  CHECK(masi::inf::validate_route_fence(route, rb).ok(), "complete exact route fence must pass");

  using RouteMutation =
      std::pair<std::string, std::function<void(masi::edge::v1::InferenceRoute &)>>;
  const std::vector<RouteMutation> route_mutations = {
      {"schema_version", [](auto &r) { r.set_schema_version("inference-route/v2"); }},
      {"shard_id", [](auto &r) { r.clear_shard_id(); }},
      {"incarnation", [](auto &r) { r.set_model_control_incarnation_id("old"); }},
      {"operation_id", [](auto &r) { r.set_operation_id("other"); }},
      {"scope", [](auto &r) { r.clear_scope(); }},
      {"route_epoch", [](auto &r) { r.set_route_epoch(0); }},
      {"logical_pool_id", [](auto &r) { r.set_logical_pool_id("other"); }},
      {"pool_generation", [](auto &r) { r.set_pool_generation(5); }},
      {"binding_generation", [](auto &r) { r.set_binding_generation(10); }},
      {"expected_generation", [](auto &r) { r.set_expected_binding_generation(9); }},
      {"proposed_generation", [](auto &r) { r.set_proposed_binding_generation(10); }},
      {"current_generation", [](auto &r) { r.set_current_binding_generation(8); }},
      {"startup_envelope", [](auto &r) { r.set_startup_envelope_digest(fixed_digest('d')); }},
      {"pool_observation", [](auto &r) { r.set_pool_observation_digest(fixed_digest('d')); }},
      {"binding_digest", [](auto &r) { r.set_binding_digest(fixed_digest('d')); }},
      {"model_revision", [](auto &r) { r.set_model_revision_digest(fixed_digest('d')); }},
      {"model_bundle", [](auto &r) { r.set_model_bundle_digest(fixed_digest('d')); }},
      {"feature_contract", [](auto &r) { r.set_feature_contract_digest(fixed_digest('d')); }},
      {"label_contract", [](auto &r) { r.set_label_contract_digest(fixed_digest('d')); }},
      {"output_adapter", [](auto &r) { r.set_output_adapter_digest(fixed_digest('d')); }},
      {"wire_profile", [](auto &r) { r.set_wire_profile("other/v1"); }},
      {"wire_digest", [](auto &r) { r.set_wire_profile_digest(fixed_digest('d')); }},
      {"runtime_profile", [](auto &r) { r.set_runtime_profile("other/v1"); }},
      {"runtime_digest", [](auto &r) { r.set_runtime_profile_digest(fixed_digest('d')); }},
      {"optimization_digest",
       [](auto &r) { r.set_optimization_profile_digest(fixed_digest('d')); }},
  };
  for (const auto &[name, mutate] : route_mutations) {
    auto changed = route;
    mutate(changed);
    CHECK(!masi::inf::validate_route_fence(changed, rb).ok(),
          "route fence mutation must reject: " + name);
  }

  const auto record = fenced_record(route);
  CHECK(masi::inf::validate_record_fence(route, record).ok(),
        "complete exact record fence must pass");
  using RecordMutation =
      std::pair<std::string, std::function<void(masi::edge::v1::InferenceRecord &)>>;
  const std::vector<RecordMutation> record_mutations = {
      {"input_id", [](auto &r) { r.clear_input_id(); }},
      {"event_id", [](auto &r) { r.clear_event_idempotency_key(); }},
      {"window_id", [](auto &r) { r.clear_window_id(); }},
      {"source_runtime", [](auto &r) { r.clear_source_runtime_epoch(); }},
      {"record_schema", [](auto &r) { r.set_schema_version("edge-inference-record/v2"); }},
      {"target", [](auto &r) { r.set_target_id("other"); }},
      {"final_window", [](auto &r) { r.set_final_window(false); }},
      {"quality", [](auto &r) { r.set_quality("gap"); }},
      {"quality_code", [](auto &r) { r.set_quality_code(masi::edge::v1::DATA_QUALITY_GAP); }},
      {"sequence", [](auto &r) { r.set_source_sequence_start(11); }},
      {"window_time", [](auto &r) { r.set_window_end_unix_ms(1000); }},
      {"watermark", [](auto &r) { r.set_watermark_unix_ms(1999); }},
      {"finalized", [](auto &r) { r.set_finalized_at_unix_ms(1999); }},
      {"source_wal", [](auto &r) { r.set_source_wal_sequence(0); }},
      {"input_wal", [](auto &r) { r.set_input_wal_sequence(0); }},
      {"quality_reason", [](auto &r) { r.set_quality_reasons(0, "GAP"); }},
      {"coverage", [](auto &r) { r.set_sampling_coverage_ppm(1000001); }},
      {"incarnation", [](auto &r) { r.set_model_control_incarnation_id("other"); }},
      {"operation", [](auto &r) { r.set_operation_id("other"); }},
      {"scope", [](auto &r) { r.set_scope("other"); }},
      {"route_epoch", [](auto &r) { r.set_route_epoch(4); }},
      {"pool", [](auto &r) { r.set_logical_pool_id("other"); }},
      {"pool_generation", [](auto &r) { r.set_pool_generation(5); }},
      {"binding_generation", [](auto &r) { r.set_binding_generation(10); }},
      {"model_revision", [](auto &r) { r.set_model_revision_digest(fixed_digest('d')); }},
      {"model_bundle", [](auto &r) { r.set_model_bundle_digest(fixed_digest('d')); }},
      {"feature", [](auto &r) { r.set_feature_contract_digest(fixed_digest('d')); }},
      {"label", [](auto &r) { r.set_label_contract_digest(fixed_digest('d')); }},
      {"adapter", [](auto &r) { r.set_output_adapter_digest(fixed_digest('d')); }},
      {"wire", [](auto &r) { r.set_wire_profile_digest(fixed_digest('d')); }},
      {"runtime", [](auto &r) { r.set_runtime_profile_digest(fixed_digest('d')); }},
      {"optimization", [](auto &r) { r.set_optimization_profile_digest(fixed_digest('d')); }},
      {"pool_observation", [](auto &r) { r.set_pool_observation_digest(fixed_digest('d')); }},
      {"binding", [](auto &r) { r.set_binding_digest(fixed_digest('d')); }},
  };
  for (const auto &[name, mutate] : record_mutations) {
    masi::edge::v1::InferenceRecord changed;
    changed.CopyFrom(record);
    mutate(changed);
    CHECK(!masi::inf::validate_record_fence(route, changed).ok(),
          "record fence mutation must reject: " + name);
  }
}

// ---------------------------------------------------------------------------
// Test: admission per-record checks over batches of 1..256 records
// ---------------------------------------------------------------------------
namespace {

// One 48-byte record of six little-endian uint64 values.
std::vector<uint8_t> make_record_bytes(uint64_t seed) {
  std::vector<uint8_t> out(48, 0);
  for (int f = 0; f < 6; ++f) {
    uint64_t v = seed + static_cast<uint64_t>(f);
    for (int b = 0; b < 8; ++b)
      out[f * 8 + b] = static_cast<uint8_t>((v >> (8 * b)) & 0xff);
  }
  return out;
}

masi::inf::BatchAdmissionInput make_batch(std::vector<std::vector<uint8_t>> &storage,
                                          size_t records) {
  storage.clear();
  storage.reserve(records);
  masi::inf::BatchAdmissionInput in;
  in.request_id = "req-001";
  in.schema_version = "inference-central-grpc-batch/v1";
  in.wire_profile = "inference-central-grpc-batch/v1";
  in.deadline_unix_ms = 9999999999999LL;
  in.request_bytes = records * 64;
  in.route_shard_id = "shard-001";
  in.route_epoch = 1;
  in.pool_generation = 1;
  in.binding_generation = 1;
  in.model_control_incarnation_id = "inc-001";
  for (size_t i = 0; i < records; ++i)
    storage.push_back(make_record_bytes(i));
  for (size_t i = 0; i < records; ++i) {
    masi::inf::RecordView rv;
    rv.bytes = storage[i].data();
    rv.byte_count = storage[i].size();
    rv.shape = {1, 6};
    rv.dtype = "uint64-le";
    rv.input_digest = masi::inf::sha256_hex(storage[i].data(), storage[i].size());
    in.records.push_back(rv);
  }
  return in;
}

} // namespace

void test_admission_batch_sizes() {
  masi::inf::Config cfg;
  masi::inf::WireProfile profile;
  std::vector<std::vector<uint8_t>> storage;

  for (size_t n : {size_t{1}, size_t{2}, size_t{32}, size_t{256}}) {
    auto in = make_batch(storage, n);
    auto d = masi::inf::admit(cfg, profile, in);
    CHECK(d.verdict == masi::inf::AdmissionVerdict::kAccept,
          "admission: batch of " + std::to_string(n) + " records must be accepted (" +
              d.reason_code + ")");
    CHECK(d.accepted_records == n, "admission: accepted_records wrong");
    CHECK(d.accepted_bytes == n * 48, "admission: accepted_bytes wrong");
    CHECK(d.input_digest.rfind("sha256:", 0) == 0, "admission: batch digest format wrong");
  }
  // Empty and oversize batches are bounded.
  {
    auto in = make_batch(storage, 0);
    auto d = masi::inf::admit(cfg, profile, in);
    CHECK(d.verdict == masi::inf::AdmissionVerdict::kHold, "admission: empty batch must be HOLD");
  }
  {
    auto in = make_batch(storage, 257);
    auto d = masi::inf::admit(cfg, profile, in);
    CHECK(d.verdict == masi::inf::AdmissionVerdict::kHold,
          "admission: 257-record batch must be HOLD");
    CHECK(d.reason_code == "record_count out of range", "admission: oversize count reason wrong");
  }
  // The batch digest binds every record, so changing one record changes it.
  {
    auto a = make_batch(storage, 2);
    auto da = masi::inf::admit(cfg, profile, a);
    std::vector<std::vector<uint8_t>> storage_b;
    auto b = make_batch(storage_b, 2);
    b.records[1].input_digest = masi::inf::sha256_hex(storage_b[1].data(), storage_b[1].size());
    storage_b[1][0] ^= 0xff;
    b.records[1].input_digest = masi::inf::sha256_hex(storage_b[1].data(), storage_b[1].size());
    auto db = masi::inf::admit(cfg, profile, b);
    CHECK(db.verdict == masi::inf::AdmissionVerdict::kAccept,
          "admission: modified record with matching digest must be accepted");
    CHECK(da.input_digest != db.input_digest,
          "admission: batch digest must change when a record changes");
  }
}

// ---------------------------------------------------------------------------
// Test: admission verifies the declared per-record input_digest
// ---------------------------------------------------------------------------
void test_admission_input_digest_verified() {
  masi::inf::Config cfg;
  masi::inf::WireProfile profile;
  std::vector<std::vector<uint8_t>> storage;

  {
    auto in = make_batch(storage, 2);
    in.records[1].input_digest = "sha256:" + std::string(64, 'd');
    auto d = masi::inf::admit(cfg, profile, in);
    CHECK(d.verdict == masi::inf::AdmissionVerdict::kFail,
          "admission: wrong input_digest must FAIL");
    CHECK(d.reason_code.find("input_digest mismatch") != std::string::npos,
          "admission: input_digest mismatch reason wrong: " + d.reason_code);
  }
  {
    auto in = make_batch(storage, 1);
    in.records[0].input_digest.clear();
    auto d = masi::inf::admit(cfg, profile, in);
    CHECK(d.verdict == masi::inf::AdmissionVerdict::kFail,
          "admission: empty input_digest must FAIL");
  }
  // Per-record shape is checked for every record, not only the first one.
  {
    auto in = make_batch(storage, 3);
    in.records[2].shape = {2, 6};
    auto d = masi::inf::admit(cfg, profile, in);
    CHECK(d.verdict == masi::inf::AdmissionVerdict::kFail,
          "admission: wrong per-record shape must FAIL");
    CHECK(d.reason_code.find("at record 2") != std::string::npos,
          "admission: rejection must name the offending record: " + d.reason_code);
  }
  // Byte-count/shape disagreement is rejected per record.
  {
    auto in = make_batch(storage, 2);
    in.records[0].byte_count = 40;
    auto d = masi::inf::admit(cfg, profile, in);
    CHECK(d.verdict == masi::inf::AdmissionVerdict::kFail, "admission: truncated record must FAIL");
  }
  for (const std::string &dtype : {"float32-le", "float64-le"}) {
    auto in = make_batch(storage, 1);
    in.records[0].dtype = dtype;
    auto d = masi::inf::admit(cfg, profile, in);
    CHECK(d.verdict == masi::inf::AdmissionVerdict::kFail,
          "admission: " + dtype + " must fail before Triton");
    CHECK(d.reason_code.find("feature dtype must be uint64-le") != std::string::npos,
          "admission: dtype rejection reason drifted: " + d.reason_code);
  }
  for (const std::string &digest : std::vector<std::string>{
           "sha256:", "sha256:" + std::string(63, 'a'), "sha256:" + std::string(64, 'A')}) {
    auto in = make_batch(storage, 1);
    in.records[0].input_digest = digest;
    auto d = masi::inf::admit(cfg, profile, in);
    CHECK(d.verdict == masi::inf::AdmissionVerdict::kFail,
          "admission: malformed input digest must fail");
    CHECK(d.reason_code.find("input_digest format invalid") != std::string::npos,
          "admission: malformed digest reason drifted: " + d.reason_code);
  }
}

// ---------------------------------------------------------------------------
// Test: admission oversize batch rejected
// ---------------------------------------------------------------------------
// A startup envelope must carry real digest-typed identities. An empty or
// foreign wire profile digest is a missing binding and must fail closed.
void test_envelope_profile_digests_required() {
  const std::string valid_wire_digest = MASI_INF_WIRE_PROFILE_DIGEST;
  auto base = [&]() {
    nlohmann::json j;
    j["schema_version"] = "inference-startup-envelope/v1";
    j["model_control_incarnation_id"] = "inc-1";
    j["operation_id"] = "op-1";
    j["kind"] = "binding";
    j["logical_pool_id"] = "pool-1";
    j["pool_generation"] = 1;
    j["availability_profile_id"] = "availability-single/v1";
    j["deployment_tier"] = "acceptance";
    j["model_revision_digest"] = std::string("sha256:") + std::string(64, 'a');
    j["feature_contract_digest"] = std::string("sha256:") + std::string(64, 'b');
    j["label_contract_digest"] = std::string("sha256:") + std::string(64, 'c');
    j["output_adapter_digest"] = std::string("sha256:") + std::string(64, 'd');
    j["inference_wire_profile_digest"] = valid_wire_digest;
    j["runtime_profile_id"] = "model-runtime-central-cpu/v1";
    j["runtime_profile_digest"] = MASI_INF_RUNTIME_PROFILE_DIGEST;
    j["optimization_profile_digest"] = MASI_INF_OPTIMIZATION_PROFILE_DIGEST;
    j["triton_server_version"] = "2.59.0";
    j["repository_snapshot"] = {{"identity", "repo-1"},
                                {"closure_digest", std::string("sha256:") + std::string(64, '2')}};
    j["instance_group"] = {
        {"kind", "KIND_CPU"},
        {"count", 1},
        {"operator_partition_digest", std::string("sha256:") + std::string(64, '1')}};
    j["proposed_binding_generation"] = 1;
    j["issued_at_unix_ms"] = 1786406400000;
    j["expires_at_unix_ms"] = 1786406460000;
    j["trace_id"] = "trace-envelope-test";
    std::ostringstream body;
    body << j["schema_version"].get<std::string>() << '\n'
         << j["model_control_incarnation_id"].get<std::string>() << '\n'
         << j["operation_id"].get<std::string>() << '\n'
         << j["kind"].get<std::string>() << '\n'
         << j["logical_pool_id"].get<std::string>() << '\n'
         << j["pool_generation"].get<uint64_t>() << '\n'
         << j["availability_profile_id"].get<std::string>() << '\n'
         << j["deployment_tier"].get<std::string>() << '\n'
         << j["model_revision_digest"].get<std::string>() << '\n'
         << j["feature_contract_digest"].get<std::string>() << '\n'
         << j["label_contract_digest"].get<std::string>() << '\n'
         << j["output_adapter_digest"].get<std::string>() << '\n'
         << j["inference_wire_profile_digest"].get<std::string>() << '\n'
         << j["runtime_profile_id"].get<std::string>() << '\n'
         << j["runtime_profile_digest"].get<std::string>() << '\n'
         << j["optimization_profile_digest"].get<std::string>() << '\n'
         << j["triton_server_version"].get<std::string>() << '\n'
         << j["repository_snapshot"]["identity"].get<std::string>() << '\n'
         << j["repository_snapshot"]["closure_digest"].get<std::string>() << '\n'
         << j["instance_group"]["kind"].get<std::string>() << '\n'
         << j["instance_group"]["count"].get<int32_t>() << '\n'
         << j["instance_group"]["operator_partition_digest"].get<std::string>() << '\n'
         << j["proposed_binding_generation"].get<uint64_t>() << '\n'
         << j["issued_at_unix_ms"].get<int64_t>() << '\n'
         << j["expires_at_unix_ms"].get<int64_t>() << '\n'
         << j["trace_id"].get<std::string>() << '\n';
    const std::string canonical = body.str();
    j["envelope_digest"] = masi::inf::sha256_hex(canonical.data(), canonical.size());
    return j;
  };

  TempDir td;
  {
    const auto valid = base();
    const std::string path = write_temp_file(td.path(), "env-valid.json", valid.dump());
    const auto parsed = masi::inf::parse_startup_envelope(path, 1786406400000);
    CHECK(parsed.runtime_profile_digest == MASI_INF_RUNTIME_PROFILE_DIGEST,
          "valid envelope runtime preimage did not parse");
  }
  auto parse_rejects = [&](const nlohmann::json &j, const std::string &name) {
    const std::string path = write_temp_file(td.path(), name, j.dump());
    try {
      masi::inf::parse_startup_envelope(path, 1786406400000);
    } catch (const masi::inf::error::Exception &) {
      return true;
    }
    return false;
  };

  // Each digest-typed field must be rejected when it is empty.
  int negative = 0;
  for (const char *field : {"model_revision_digest", "inference_wire_profile_digest",
                            "runtime_profile_digest", "optimization_profile_digest"}) {
    nlohmann::json j = base();
    j[field] = "";
    CHECK(parse_rejects(j, "env-empty-" + std::to_string(++negative) + ".json"),
          std::string("empty ") + field + " must be rejected");
  }

  // A foreign wire profile revision must be rejected even when well formed.
  {
    nlohmann::json j = base();
    j["inference_wire_profile_digest"] = std::string("sha256:") + std::string(64, '9');
    CHECK(parse_rejects(j, "env-foreign-wire.json"),
          "a foreign wire profile digest must be rejected");
  }
  std::cout << "  envelope profile digests are required and bound: OK\n";
}

void test_admission_oversize_rejected() {
  masi::inf::Config cfg;
  cfg.max_records_per_batch = 256;
  cfg.max_request_bytes = 4194304;
  masi::inf::WireProfile profile;
  std::vector<std::vector<uint8_t>> storage;
  auto in = make_batch(storage, 1);
  in.request_bytes = static_cast<size_t>(profile.maximum_request_bytes) + 1;
  auto d = masi::inf::admit(cfg, profile, in);
  CHECK(d.verdict == masi::inf::AdmissionVerdict::kHold,
        "admission: oversize request must be HOLD");
  CHECK(d.reason_code == "INFERENCE_MESSAGE_TOO_LARGE", "admission: oversize reason code wrong");
}

void test_triton_output_shape_is_exact() {
  masi::inf::TritonInferResult result;
  result.values = {0.1F, 0.9F, 0.8F, 0.2F};
  result.shape = {2, 2};
  CHECK(masi::inf::validate_triton_output_shape(result, 2, 2).ok(),
        "triton output: exact [records, classes] shape must pass");

  result.shape = {4};
  CHECK(!masi::inf::validate_triton_output_shape(result, 2, 2).ok(),
        "triton output: rank-1 output must fail closed");
  result.shape = {1, 2, 2};
  CHECK(!masi::inf::validate_triton_output_shape(result, 2, 2).ok(),
        "triton output: rank-3 output must fail closed");
  result.shape = {4, 1};
  CHECK(!masi::inf::validate_triton_output_shape(result, 2, 2).ok(),
        "triton output: wrong dimensions must fail closed");
  result.shape = {2, 2};
  result.values.pop_back();
  CHECK(!masi::inf::validate_triton_output_shape(result, 2, 2).ok(),
        "triton output: wrong element count must fail closed");
}

} // namespace

int main() {
  std::cout << "=== Central Inference property invariant tests ===\n";
  test_error_codes_stable();
  test_sha256_format();
  test_digest_match();
  test_tensor_layout_checks();
  test_nan_inf_rejection();
  test_output_finite_rejection();
  test_float_tolerance();
  test_output_adapter_semantics();
  test_config_unknown_field_rejected();
  test_config_valid_loads();
  test_config_requires_client_san_allowlist();
  test_config_unsupported_runtime_profile();
  test_config_resource_ceilings();
  test_repository_closure_extra_member_rejected();
  test_repository_closure_valid();
  test_repository_closure_digest_binds_path_and_role();
  test_bundle_manifest_loads_and_self_verifies();
  test_admission_unknown_major_rejected();
  test_result_fence_dimensions_count();
  test_complete_route_and_record_fence();
  test_admission_batch_sizes();
  test_admission_input_digest_verified();
  test_admission_oversize_rejected();
  test_triton_output_shape_is_exact();
  test_envelope_profile_digests_required();
  std::cout << "All property invariant tests passed.\n";
  return 0;
}
