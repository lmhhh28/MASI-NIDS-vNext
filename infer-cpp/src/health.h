#pragma once

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "gateway.h"
#include "startup.h"

namespace masi::inf {

// Separated health semantics. Readiness requires Gateway + Triton exact model
// ready + readback consistent. Liveness requires the process / execution
// thread making progress. Drain stops new admission + bounded deadline
// processing + exposes incomplete attempts. Shutdown stops Gateway/Triton +
// preserves structured startup/runtime evidence.
enum class HealthState { kBooting, kReady, kDraining, kShuttingDown, kDown };

struct HealthSnapshot {
  HealthState state = HealthState::kBooting;
  bool process_live = true;
  bool accepting = false;
  bool triton_ready = false;
  bool readback_consistent = false;
  int64_t observed_at_unix_ms = 0;
  uint64_t incomplete_attempts = 0;
  std::string reason;
};

class HealthMonitor {
 public:
  explicit HealthMonitor(std::shared_ptr<CentralInferenceServiceImpl> svc,
                          std::shared_ptr<TritonClient> triton,
                          StartupResult startup);
  ~HealthMonitor() = default;

  void mark_ready();
  void begin_drain(int32_t drain_ms);
  void begin_shutdown();
  void report_incomplete_attempt();

  HealthState state() const noexcept;
  HealthSnapshot snapshot() const;

  // OCI readiness probe: returns true iff Gateway ready + Triton ready +
  // readback consistent.
  bool readiness_ok() const;

 private:
  std::shared_ptr<CentralInferenceServiceImpl> svc_;
  std::shared_ptr<TritonClient> triton_;
  StartupResult startup_;
  std::atomic<HealthState> state_{HealthState::kBooting};
  std::atomic<uint64_t> incomplete_{0};
  int32_t drain_ms_ = 5000;
};

}  // namespace masi::inf