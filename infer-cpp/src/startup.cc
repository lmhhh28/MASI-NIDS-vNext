#include "startup.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <sstream>

#include "digest.h"
#include "numeric.h"

namespace masi::inf {

namespace {

int64_t now_ms() {
  using namespace std::chrono;
  return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

StageResult make_stage(const std::string &name) {
  StageResult s;
  s.stage = name;
  s.started_at_unix_ms = now_ms();
  return s;
}

[[noreturn]] void fail_stage(StageResult &s, const std::string &reason) {
  s.completed_at_unix_ms = now_ms();
  s.status = "FAIL";
  s.reason_code = reason;
  throw error::Exception(error::Code::kStartupFailed, "stage " + s.stage + " failed: " + reason);
}

void ok_stage(StageResult &s, std::string digest = "") {
  s.completed_at_unix_ms = now_ms();
  s.status = "OK";
  s.observed_digest = std::move(digest);
}

std::string digest_of(const std::string &s) { return sha256_hex(s.data(), s.size()); }

std::string trim(const std::string &s) {
  const auto b = s.find_first_not_of(" \t\r\n");
  if (b == std::string::npos)
    return "";
  const auto e = s.find_last_not_of(" \t\r\n");
  return s.substr(b, e - b + 1);
}

std::string strip_quotes(const std::string &s) {
  if (s.size() >= 2 && s.front() == '"' && s.back() == '"')
    return s.substr(1, s.size() - 2);
  return s;
}

// Extract the numbers from a `[ 1, 2, 3 ]` or `1` value form.
std::vector<int64_t> parse_int_list(const std::string &raw) {
  std::vector<int64_t> out;
  std::string cur;
  for (char c : raw) {
    if ((c >= '0' && c <= '9') || c == '-') {
      cur.push_back(c);
    } else if (!cur.empty()) {
      out.push_back(std::strtoll(cur.c_str(), nullptr, 10));
      cur.clear();
    }
  }
  if (!cur.empty())
    out.push_back(std::strtoll(cur.c_str(), nullptr, 10));
  return out;
}

std::string join_i64(const std::vector<int64_t> &v) {
  std::ostringstream oss;
  for (size_t i = 0; i < v.size(); ++i) {
    if (i)
      oss << ',';
    oss << v[i];
  }
  return oss.str();
}

std::string join_i32(const std::vector<int32_t> &v) {
  std::ostringstream oss;
  for (size_t i = 0; i < v.size(); ++i) {
    if (i)
      oss << ',';
    oss << v[i];
  }
  return oss.str();
}

} // namespace

NumericProfile make_numeric_profile(const BundleManifest &bundle) {
  NumericProfile p;
  p.adapter_id = bundle.adapter_id;
  p.adapter_digest = bundle.adapter_digest;
  p.score_domain = bundle.score_domain;
  p.class_order = bundle.class_order;
  p.alert_threshold = bundle.alert_threshold;
  p.abstain_below = bundle.abstain_below;
  p.ood_mode = bundle.ood_mode;
  p.ood_below = bundle.ood_below;
  return p;
}

// Minimal, bounded text-proto reader for exactly the `config.pbtxt` fields the
// startup gate compares. Unknown keys are ignored; the authoritative check is
// the field-by-field comparison against the live ModelConfig, so an unparsed
// field cannot silently pass as equal.
TritonModelConfigProjection parse_pinned_config_text(const std::string &text) {
  if (text.size() > 1048576)
    throw error::Exception(error::Code::kInvalidManifest, "pinned config.pbtxt exceeds 1MiB");
  TritonModelConfigProjection c;
  std::istringstream in(text);
  std::string line;
  int section = 0; // 0 none, 1 input, 2 output, 3 dynamic_batching, 4 instance_group
  int32_t pending_instance_count = 0;
  std::string pending_instance_kind;
  int brace_depth = 0;

  while (std::getline(in, line)) {
    const std::string t = trim(line);
    if (t.empty() || t[0] == '#')
      continue;

    if (t.rfind("input", 0) == 0 && t.find('[') != std::string::npos) {
      section = 1;
      brace_depth = 0;
      continue;
    }
    if (t.rfind("output", 0) == 0 && t.find('[') != std::string::npos) {
      section = 2;
      brace_depth = 0;
      continue;
    }
    if (t.rfind("dynamic_batching", 0) == 0) {
      section = 3;
      c.dynamic_batching = true;
      brace_depth = 0;
      continue;
    }
    if (t.rfind("instance_group", 0) == 0) {
      section = 4;
      brace_depth = 0;
      continue;
    }

    if (t == "{") {
      ++brace_depth;
      continue;
    }
    if (t == "}" || t == "},") {
      if (section == 4 && !pending_instance_kind.empty()) {
        c.instance_groups.emplace_back(pending_instance_kind, pending_instance_count);
        pending_instance_kind.clear();
        pending_instance_count = 0;
      }
      continue;
    }
    if (t == "]" || t == "],") {
      section = 0;
      continue;
    }

    const auto colon = t.find(':');
    if (colon == std::string::npos)
      continue;
    const std::string key = trim(t.substr(0, colon));
    std::string value = trim(t.substr(colon + 1));
    if (!value.empty() && value.back() == ',')
      value.pop_back();
    value = strip_quotes(trim(value));

    if (section == 0) {
      if (key == "name")
        c.name = value;
      else if (key == "backend")
        c.backend = value;
      else if (key == "platform")
        c.platform = value;
      else if (key == "max_batch_size")
        c.max_batch_size = static_cast<int32_t>(std::strtol(value.c_str(), nullptr, 10));
    } else if (section == 1) {
      if (key == "name")
        c.input_name = value;
      else if (key == "data_type")
        c.input_datatype = value;
      else if (key == "dims")
        c.input_dims = parse_int_list(value);
    } else if (section == 2) {
      if (key == "name")
        c.output_name = value;
      else if (key == "data_type")
        c.output_datatype = value;
      else if (key == "dims")
        c.output_dims = parse_int_list(value);
    } else if (section == 3) {
      if (key == "preferred_batch_size") {
        for (int64_t v : parse_int_list(value))
          c.preferred_batch_size.push_back(static_cast<int32_t>(v));
      } else if (key == "max_queue_delay_microseconds") {
        c.max_queue_delay_microseconds = std::strtoll(value.c_str(), nullptr, 10);
      } else if (key == "max_queue_size") {
        c.max_queue_size = static_cast<int32_t>(std::strtol(value.c_str(), nullptr, 10));
      }
    } else if (section == 4) {
      if (key == "kind")
        pending_instance_kind = value;
      else if (key == "count")
        pending_instance_count = static_cast<int32_t>(std::strtol(value.c_str(), nullptr, 10));
    }
  }
  if (section == 4 && !pending_instance_kind.empty())
    c.instance_groups.emplace_back(pending_instance_kind, pending_instance_count);

  // Triton reports the data type without the TYPE_ prefix in ModelConfig
  // (`TYPE_UINT64`), so keep the pinned text form as-is and normalize when
  // comparing.
  return c;
}

namespace {

std::string normalize_dtype(const std::string &s) {
  if (s.rfind("TYPE_", 0) == 0)
    return s.substr(5);
  return s;
}

} // namespace

void assert_triton_config_matches(const TritonModelConfigProjection &live,
                                  const TritonModelConfigProjection &pinned,
                                  const TritonExecutionExpectation &expected,
                                  const BundleManifest &bundle, const StartupEnvelope &env) {
  auto fail = [](const std::string &what) {
    throw error::Exception(error::Code::kIncompatibleContract, "triton config mismatch: " + what);
  };

  // 1. live vs pinned on-disk config.pbtxt (proves Triton serves this closure).
  if (live.name != pinned.name)
    fail("name live=" + live.name + " pinned=" + pinned.name);
  if (!pinned.backend.empty() && live.backend != pinned.backend)
    fail("backend live=" + live.backend + " pinned=" + pinned.backend);
  if (live.max_batch_size != pinned.max_batch_size)
    fail("max_batch_size live=" + std::to_string(live.max_batch_size) +
         " pinned=" + std::to_string(pinned.max_batch_size));
  if (live.dynamic_batching != pinned.dynamic_batching)
    fail("dynamic_batching presence differs from the pinned config");
  if (live.preferred_batch_size != pinned.preferred_batch_size)
    fail("preferred_batch_size live=" + join_i32(live.preferred_batch_size) +
         " pinned=" + join_i32(pinned.preferred_batch_size));
  if (live.max_queue_delay_microseconds != pinned.max_queue_delay_microseconds)
    fail("max_queue_delay_microseconds live=" + std::to_string(live.max_queue_delay_microseconds) +
         " pinned=" + std::to_string(pinned.max_queue_delay_microseconds));
  if (live.max_queue_size != pinned.max_queue_size)
    fail("max_queue_size live=" + std::to_string(live.max_queue_size) +
         " pinned=" + std::to_string(pinned.max_queue_size));
  if (live.instance_groups.size() != pinned.instance_groups.size())
    fail("instance_group count differs from the pinned config");
  for (size_t i = 0; i < live.instance_groups.size(); ++i) {
    if (live.instance_groups[i].first != pinned.instance_groups[i].first ||
        live.instance_groups[i].second != pinned.instance_groups[i].second)
      fail("instance_group[" + std::to_string(i) + "] differs from the pinned config");
  }
  if (live.input_name != pinned.input_name || live.output_name != pinned.output_name)
    fail("tensor names differ from the pinned config");
  if (normalize_dtype(live.input_datatype) != normalize_dtype(pinned.input_datatype) ||
      normalize_dtype(live.output_datatype) != normalize_dtype(pinned.output_datatype))
    fail("tensor datatypes differ from the pinned config");
  if (live.input_dims != pinned.input_dims || live.output_dims != pinned.output_dims)
    fail("tensor dims live=" + join_i64(live.input_dims) + "/" + join_i64(live.output_dims) +
         " pinned=" + join_i64(pinned.input_dims) + "/" + join_i64(pinned.output_dims));

  // 2. live vs the frozen wire profile expectations.
  if (!expected.dynamic_batching || !live.dynamic_batching)
    fail("frozen profile requires dynamic batching to be the sole delayed batcher");
  if (live.max_batch_size != expected.max_batch_size)
    fail("max_batch_size != frozen profile " + std::to_string(expected.max_batch_size));
  if (live.preferred_batch_size != expected.preferred_batch_size)
    fail("preferred_batch_size != frozen profile " + join_i32(expected.preferred_batch_size));
  if (live.max_queue_delay_microseconds != expected.max_queue_delay_microseconds)
    fail("max_queue_delay_microseconds != frozen profile " +
         std::to_string(expected.max_queue_delay_microseconds));
  if (live.max_queue_size != expected.max_queue_size)
    fail("max_queue_size != frozen profile " + std::to_string(expected.max_queue_size));
  if (live.backend != expected.backend)
    fail("backend != frozen profile " + expected.backend);

  // 3. live vs the bundle manifest's declared Triton expectations.
  if (bundle.triton_max_batch_size != live.max_batch_size)
    fail("max_batch_size != bundle manifest " + std::to_string(bundle.triton_max_batch_size));
  if (bundle.triton_max_queue_size != live.max_queue_size)
    fail("max_queue_size != bundle manifest " + std::to_string(bundle.triton_max_queue_size));
  if (bundle.triton_max_queue_delay_microseconds != live.max_queue_delay_microseconds)
    fail("max_queue_delay_microseconds != bundle manifest");
  if (bundle.triton_preferred_batch_size != live.preferred_batch_size)
    fail("preferred_batch_size != bundle manifest");

  // 4. explicit instance group, and it must match the operator selection in the
  // envelope. An empty or implicit instance group is rejected.
  if (live.instance_groups.empty())
    throw error::Exception(error::Code::kInstanceGroupImplicit,
                           "triton reports no explicit instance_group");
  if (live.instance_groups.size() != 1)
    throw error::Exception(error::Code::kInstanceGroupImplicit,
                           "first release qualifies exactly one instance_group");
  if (live.instance_groups[0].first != env.instance_group.kind)
    throw error::Exception(error::Code::kProviderPartitionDrift,
                           "instance_group kind observed=" + live.instance_groups[0].first +
                               " selected=" + env.instance_group.kind);
  if (live.instance_groups[0].second != env.instance_group.count)
    throw error::Exception(
        error::Code::kProviderPartitionDrift,
        "instance_group count observed=" + std::to_string(live.instance_groups[0].second) +
            " selected=" + std::to_string(env.instance_group.count));
  if (bundle.triton_instance_group_kind != live.instance_groups[0].first ||
      bundle.triton_instance_group_count != live.instance_groups[0].second)
    fail("instance_group != bundle manifest declaration");

  // 5. runtime profile: the CPU profile requires CPU placement, and the CUDA
  // profile is not qualified in this build.
  if (env.runtime_profile_id == "model-runtime-central-cpu/v1") {
    if (live.instance_groups[0].first != "KIND_CPU")
      throw error::Exception(error::Code::kProviderPartitionDrift,
                             "cpu runtime profile requires KIND_CPU placement, observed " +
                                 live.instance_groups[0].first);
  } else {
    throw error::Exception(error::Code::kRuntimeProfileUnsupported,
                           "runtime profile not qualified in this build: " +
                               env.runtime_profile_id);
  }
}

StartupResult run_startup(const Config &cfg, TritonClient &triton, int64_t now_unix_ms) {
  (void)now_unix_ms;
  StartupResult r;
  r.started_at_unix_ms = now_ms();
  const TritonExecutionExpectation expected;

  // Stage 1: read-profile.
  {
    auto s = make_stage("read-profile");
    std::ostringstream oss;
    oss << cfg.runtime_profile << '\n'
        << cfg.availability_profile << '\n'
        << cfg.max_records_per_batch << '\n'
        << cfg.request_deadline_ms << '\n'
        << cfg.max_in_flight << '\n';
    if (cfg.runtime_profile != "model-runtime-central-cpu/v1")
      fail_stage(s, "runtime_profile not qualified in this build: " + cfg.runtime_profile);
    ok_stage(s, digest_of(oss.str()));
    r.stages.push_back(s);
  }

  // Stage 2: verify-envelope.
  StartupEnvelope env;
  {
    auto s = make_stage("verify-envelope");
    try {
      env = parse_startup_envelope(cfg.startup_envelope_path, now_ms());
    } catch (const error::Exception &e) {
      fail_stage(s, e.what());
    }
    if (env.runtime_profile_id != cfg.runtime_profile)
      fail_stage(s, "envelope runtime_profile_id != config runtime_profile");
    ok_stage(s, env.envelope_digest);
    r.stages.push_back(s);
  }

  // Stage 3: verify-repository closure.
  {
    auto s = make_stage("verify-repository");
    ClosureVerification cv;
    try {
      cv = verify_repository_closure(cfg.model_repository_path, env.repository_snapshot.identity,
                                     env.repository_snapshot.closure_digest);
      assert_closure_ok(cv);
    } catch (const error::Exception &e) {
      fail_stage(s, e.what());
    }
    ok_stage(s, cv.observed_closure_digest);
    r.stages.push_back(s);
  }

  // Stage 4: verify-bundle-manifest (adapter data configuration).
  {
    auto s = make_stage("verify-bundle-manifest");
    try {
      r.bundle = load_bundle_manifest(cfg.model_repository_path);
      // The adapter implementation is selected by identity, never injected.
      if (r.bundle.adapter_id != "masi-window-adapter-v1")
        throw error::Exception(error::Code::kIncompatibleContract,
                               "no qualified adapter implementation for adapter_id " +
                                   r.bundle.adapter_id);
      // Envelope cross-check: the operator-declared binding must equal the
      // digest-pinned bundle data.
      if (!digest_match(env.feature_contract_digest, r.bundle.feature_contract_digest))
        throw error::Exception(error::Code::kReadbackMismatch,
                               "envelope feature_contract_digest != bundle manifest");
      if (!digest_match(env.label_contract_digest, r.bundle.label_contract_digest))
        throw error::Exception(error::Code::kReadbackMismatch,
                               "envelope label_contract_digest != bundle manifest");
      if (!digest_match(env.output_adapter_digest, r.bundle.output_adapter_digest))
        throw error::Exception(error::Code::kReadbackMismatch,
                               "envelope output_adapter_digest != bundle manifest");
      if (!digest_match(env.model_revision_digest, r.bundle.model_revision_digest) ||
          !digest_match(compute_model_revision_digest(r.bundle), r.bundle.model_revision_digest))
        throw error::Exception(error::Code::kReadbackMismatch,
                               "envelope model_revision_digest != verified bundle preimage");
      if (r.bundle.runtime_profile_id != env.runtime_profile_id ||
          !digest_match(r.bundle.runtime_profile_digest, env.runtime_profile_digest))
        throw error::Exception(error::Code::kReadbackMismatch,
                               "runtime profile identity/digest != verified bundle preimage");
      if (r.bundle.optimization_profile_id != "optimization-profile-central-cpu/v1" ||
          !digest_match(r.bundle.optimization_profile_digest, env.optimization_profile_digest))
        throw error::Exception(error::Code::kReadbackMismatch,
                               "optimization profile identity/digest != verified bundle preimage");
      r.numeric = make_numeric_profile(r.bundle);
    } catch (const error::Exception &e) {
      fail_stage(s, e.what());
    }
    ok_stage(s, r.bundle.output_adapter_digest);
    r.stages.push_back(s);
  }

  // Stage 5: triton-none-load. Everything here is observed, never assumed.
  {
    auto s = make_stage("triton-none-load");
    try {
      if (!triton.available())
        throw error::Exception(error::Code::kPoolUnavailable, "triton client not connected");
      r.triton_model_name = resolve_triton_model_name(cfg.model_repository_path);
      r.triton_model_version = "1";

      r.observed.model_name = r.triton_model_name;
      r.observed.model_version = r.triton_model_version;
      r.observed.server_ready = triton.is_server_ready();
      if (!r.observed.server_ready)
        throw error::Exception(error::Code::kPoolUnavailable, "triton server not ready");
      r.observed.model_ready = triton.is_model_ready(r.triton_model_name, r.triton_model_version);
      if (!r.observed.model_ready)
        throw error::Exception(error::Code::kPoolUnavailable,
                               "triton model not ready: " + r.triton_model_name);

      r.observed.server = triton.server_metadata();
      if (r.observed.server.version != env.triton_server_version)
        throw error::Exception(error::Code::kReadbackMismatch,
                               "triton server version observed=" + r.observed.server.version +
                                   " declared=" + env.triton_server_version);

      r.observed.model = triton.model_metadata(r.triton_model_name, r.triton_model_version);
      if (r.observed.model.name != r.triton_model_name)
        throw error::Exception(error::Code::kReadbackMismatch, "triton model name mismatch");
      if (r.observed.model.version != r.triton_model_version)
        throw error::Exception(error::Code::kReadbackMismatch, "triton model version mismatch");
      if (r.observed.model.inputs.size() != 1 || r.observed.model.outputs.size() != 1)
        throw error::Exception(error::Code::kIncompatibleContract,
                               "qualified binding has exactly one input and one output tensor");

      r.observed.config = triton.model_config(r.triton_model_name, r.triton_model_version);
      const auto pinned =
          parse_pinned_config_text(read_closure_config_text(cfg.model_repository_path));
      assert_triton_config_matches(r.observed.config, pinned, expected, r.bundle, env);

      std::ostringstream proj;
      proj << "server_name=" << r.observed.server.name << '\n'
           << "server_version=" << r.observed.server.version << '\n'
           << "model_ready=" << (r.observed.model_ready ? "1" : "0") << '\n'
           << r.observed.model.raw_metadata << r.observed.config.canonical_projection;
      r.observed.canonical_projection = proj.str();
    } catch (const error::Exception &e) {
      fail_stage(s, e.what());
    }
    ok_stage(s, digest_of(r.observed.canonical_projection));
    r.stages.push_back(s);
  }

  // Stage 6: warmup-numeric-self-test through Triton (the only execution
  // plane). Determinism, tensor shape, adapter applicability and the batched
  // path are all exercised before readiness. Expected golden values live in the
  // test harness, not in the production startup path.
  {
    auto s = make_stage("warmup-numeric-self-test");
    std::string warm_digest;
    try {
      const size_t feature_count = 6;
      const size_t class_count = r.bundle.class_order.size();
      std::vector<uint8_t> one(feature_count * sizeof(uint64_t), 0);
      const std::vector<int64_t> one_shape{1, static_cast<int64_t>(feature_count)};

      auto first = triton.model_infer(r.triton_model_name, r.triton_model_version,
                                      r.observed.config.input_name, "UINT64", one, one_shape,
                                      cfg.request_deadline_ms);
      auto second = triton.model_infer(r.triton_model_name, r.triton_model_version,
                                       r.observed.config.input_name, "UINT64", one, one_shape,
                                       cfg.request_deadline_ms);
      if (first.values.size() != class_count)
        throw error::Exception(error::Code::kIncompatibleContract,
                               "warmup output element count != class count");
      if (first.values != second.values)
        throw error::Exception(error::Code::kReadbackMismatch,
                               "warmup is not deterministic across repeated calls");
      assert_output_finite(first.values);

      // Batched equivalence: two identical rows must produce identical rows.
      std::vector<uint8_t> two(one);
      two.insert(two.end(), one.begin(), one.end());
      const std::vector<int64_t> two_shape{2, static_cast<int64_t>(feature_count)};
      auto batched = triton.model_infer(r.triton_model_name, r.triton_model_version,
                                        r.observed.config.input_name, "UINT64", two, two_shape,
                                        cfg.request_deadline_ms);
      if (batched.values.size() != class_count * 2)
        throw error::Exception(error::Code::kIncompatibleContract,
                               "batched warmup output element count mismatch");
      for (size_t i = 0; i < class_count; ++i) {
        if (batched.values[i] != first.values[i] ||
            batched.values[class_count + i] != first.values[i])
          throw error::Exception(error::Code::kReadbackMismatch,
                                 "batched warmup rows differ from the single-record result");
      }

      // The adapter must accept the observed output shape.
      const auto adapted = apply_output_adapter(first.values, r.numeric);
      if (adapted.scores.size() != class_count)
        throw error::Exception(error::Code::kIncompatibleContract,
                               "adapter produced an unexpected score count");

      std::ostringstream w;
      for (float v : first.values)
        w << v << ';';
      w << adapted.decision << ';' << adapted.predicted_label;
      warm_digest = digest_of(w.str());
    } catch (const error::Exception &e) {
      fail_stage(s, e.what());
    }
    ok_stage(s, warm_digest);
    r.stages.push_back(s);
  }

  // Stage 7: gateway-readback.
  {
    auto s = make_stage("gateway-readback");
    PoolReadback rb = build_pool_readback(env, r.bundle, r.observed, now_ms());
    rb.stages = r.stages;
    r.readback = rb;
    ok_stage(s, rb.pool_observation_digest);
    r.stages.push_back(s);
  }

  // Stage 8: readiness.
  {
    auto s = make_stage("readiness");
    if (!triton.is_server_ready())
      fail_stage(s, "triton not ready at final gate");
    if (!triton.is_model_ready(r.triton_model_name, r.triton_model_version))
      fail_stage(s, "triton model not ready at final gate");
    ok_stage(s, "ready");
    r.stages.push_back(s);
  }

  r.readback.stages = r.stages;
  r.ready = true;
  r.completed_at_unix_ms = now_ms();
  return r;
}

} // namespace masi::inf
