#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "config.h"
#include "envelope.h"
#include "ort_session.h"
#include "readback.h"
#include "repository_closure.h"
#include "triton_client.h"

namespace masi::inf {

struct StartupResult {
  bool ready = false;
  PoolReadback readback;
  std::vector<StageResult> stages;
  std::string failure_reason;
  int64_t started_at_unix_ms = 0;
  int64_t completed_at_unix_ms = 0;
};

// Fixed startup sequence:
//   1. read-profile
//   2. probe-hardware (ORT CPU EP available, exactly one provider)
//   3. verify-envelope
//   4. verify-repository
//   5. triton-none-load (load model with model-control-mode=none)
//   6. warmup-numeric-self-test (run a golden input, verify deterministic output)
//   7. gateway-readback
//   8. readiness
// Each stage has a deadline. Fail closed on any mismatch.
StartupResult run_startup(const Config& cfg, int64_t now_unix_ms);

}  // namespace masi::inf