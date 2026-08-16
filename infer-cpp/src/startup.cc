#include "startup.h"

#include <cmath>
#include <chrono>
#include <fstream>
#include <sstream>

#include "digest.h"
#include "numeric.h"
#include "ort_session.h"

namespace masi::inf {

namespace {

int64_t now_ms() {
  using namespace std::chrono;
  return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

StageResult make_stage(const std::string& name) {
  StageResult s;
  s.stage = name;
  s.started_at_unix_ms = now_ms();
  return s;
}

void finish_stage(StageResult& s, bool ok, std::string digest = "", std::string reason = "") {
  s.completed_at_unix_ms = now_ms();
  s.status = ok ? "OK" : "FAIL";
  s.observed_digest = std::move(digest);
  s.reason_code = std::move(reason);
  if (!ok) throw error::Exception(error::Code::kStartupFailed, "stage " + s.stage + " failed: " + s.reason_code);
}

bool within_deadline(int64_t started_ms, int64_t deadline_ms) {
  return (now_ms() - started_ms) <= deadline_ms;
}

}  // namespace

StartupResult run_startup(const Config& cfg, int64_t now_unix_ms) {
  StartupResult r;
  r.started_at_unix_ms = now_ms();

  // Stage 1: read-profile. Config is already loaded by main; we just record
  // the profile identity here.
  {
    auto s = make_stage("read-profile");
    std::ostringstream oss;
    oss << cfg.runtime_profile << '\n' << cfg.availability_profile << '\n'
        << cfg.max_records_per_batch << '\n' << cfg.request_deadline_ms << '\n';
    std::string d = sha256_hex(oss.str().data(), oss.str().size());
    if (cfg.runtime_profile != "model-runtime-central-cpu/v1" &&
        cfg.runtime_profile != "model-runtime-central-cuda/v1")
      finish_stage(s, false, "", "runtime_profile unsupported: " + cfg.runtime_profile);
    finish_stage(s, true, d);
    r.stages.push_back(s);
  }

  // Stage 2: probe-hardware. Assert ORT CPU EP is the only available provider.
  {
    auto s = make_stage("probe-hardware");
    try {
      assert_cpu_ep_only();
    } catch (const error::Exception& e) {
      finish_stage(s, false, "", e.what());
    }
    std::string d = sha256_hex("CPUExecutionProvider", 19);
    finish_stage(s, true, d);
    r.stages.push_back(s);
  }

  // Stage 3: verify-envelope.
  StartupEnvelope env;
  {
    auto s = make_stage("verify-envelope");
    try {
      env = parse_startup_envelope(cfg.startup_envelope_path, now_ms());
    } catch (const error::Exception& e) {
      finish_stage(s, false, "", e.what());
    }
    finish_stage(s, true, env.envelope_digest);
    r.stages.push_back(s);
  }

  // Stage 4: verify-repository.
  {
    auto s = make_stage("verify-repository");
    ClosureVerification cv;
    try {
      cv = verify_repository_closure(cfg.model_repository_path,
                                      env.repository_snapshot.identity,
                                      env.repository_snapshot.closure_digest);
      assert_closure_ok(cv);
    } catch (const error::Exception& e) {
      finish_stage(s, false, "", e.what());
    }
    finish_stage(s, true, cv.observed_closure_digest);
    r.stages.push_back(s);
  }

  // Stage 5: triton-none-load.
  TritonClient triton;
  TritonModelMetadata triton_meta;
  {
    auto s = make_stage("triton-none-load");
    TritonClient::ConnectOptions to;
    to.endpoint = cfg.triton_endpoint;
    to.deadline_ms = cfg.request_deadline_ms;
    // Triton runs on an isolated loopback/inference network without TLS in
    // the CPU E2E test. Do NOT pass the gateway's edge-facing TLS certs to
    // Triton. A separate triton_tls_* config would be used for production
    // Triton mTLS.
    try {
      triton.connect(to);
      if (!triton.is_server_ready())
        finish_stage(s, false, "", "triton server not ready");
      // The exact Triton model name comes from the digest-pinned closure
      // (model directory name), never from request/envelope data. Fail
      // closed unless the pinned model is actually loaded with metadata.
      const std::string model_name = resolve_triton_model_name(cfg.model_repository_path);
      triton_meta = triton.model_metadata(model_name, "1");
      if (triton_meta.name != model_name || triton_meta.version != "1" ||
          triton_meta.inputs.empty() || triton_meta.outputs.empty())
        finish_stage(s, false, "", "triton model metadata mismatch for " + model_name);
    } catch (const error::Exception& e) {
      finish_stage(s, false, "", e.what());
    }
    std::string d = sha256_hex(triton_meta.raw_metadata.data(), triton_meta.raw_metadata.size());
    finish_stage(s, true, d);
    r.stages.push_back(s);
  }

  // Stage 6: warmup-numeric-self-test.
  OrtSession session;
  {
    auto s = make_stage("warmup-numeric-self-test");
    OrtSessionConfig oc;
    oc.model_path = resolve_model_path(cfg.model_repository_path);
    oc.intra_op_num_threads = cfg.intra_op_num_threads;
    oc.inter_op_num_threads = cfg.inter_op_num_threads;
    oc.intra_op_affinity = cfg.intra_op_affinity;
    oc.enable_cpu_arena = cfg.enable_cpu_arena;
    try {
      session.open(oc);
      // Golden warmup: 48-byte zero input (one record, 6 x uint64).
      std::vector<uint8_t> warmup(48, 0);
      auto out = session.run(warmup, 0);
      // Numeric self-test: output must be finite.
      for (float v : out) {
        if (std::isnan(v) || std::isinf(v))
          finish_stage(s, false, "", "warmup output NaN/Inf");
      }
    } catch (const error::Exception& e) {
      finish_stage(s, false, "", e.what());
    }
    std::ostringstream oss;
    oss << session.provider_identity() << '\n' << session.session_options_identity();
    std::string d = sha256_hex(oss.str().data(), oss.str().size());
    finish_stage(s, true, d);
    r.stages.push_back(s);
  }

  // Stage 7: gateway-readback.
  {
    auto s = make_stage("gateway-readback");
    PoolReadback rb = build_pool_readback(env, session, triton, now_ms());
    rb.stages = r.stages;
    r.readback = rb;
    std::ostringstream oss;
    oss << rb.worker.worker_id << '\n' << rb.worker.worker_digest << '\n'
        << rb.startup_envelope_digest << '\n' << rb.repository_closure_digest;
    std::string d = sha256_hex(oss.str().data(), oss.str().size());
    finish_stage(s, true, d);
    r.stages.push_back(s);
  }

  // Stage 8: readiness.
  {
    auto s = make_stage("readiness");
    bool triton_ready = triton.is_server_ready();
    if (!triton_ready)
      finish_stage(s, false, "", "triton not ready at final gate");
    finish_stage(s, true, "ready");
    r.stages.push_back(s);
  }

  r.ready = true;
  r.completed_at_unix_ms = now_ms();
  return r;
}

}  // namespace masi::inf