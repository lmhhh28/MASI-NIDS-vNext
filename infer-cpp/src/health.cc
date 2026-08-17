#include "health.h"

#include <chrono>

namespace masi::inf {

namespace {

int64_t now_ms() {
  using namespace std::chrono;
  return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

} // namespace

HealthMonitor::HealthMonitor(std::shared_ptr<CentralInferenceServiceImpl> svc,
                             std::shared_ptr<TritonClient> triton, StartupResult startup)
    : svc_(std::move(svc)), triton_(std::move(triton)), startup_(std::move(startup)) {
  if (startup_.ready)
    state_.store(HealthState::kReady, std::memory_order_release);
}

void HealthMonitor::mark_ready() { state_.store(HealthState::kReady, std::memory_order_release); }

void HealthMonitor::begin_drain(int32_t drain_ms) {
  drain_ms_ = drain_ms > 0 ? drain_ms : 5000;
  state_.store(HealthState::kDraining, std::memory_order_release);
  if (svc_)
    svc_->drain_begin();
}

void HealthMonitor::begin_shutdown() {
  state_.store(HealthState::kShuttingDown, std::memory_order_release);
  if (svc_)
    svc_->shutdown_complete();
}

void HealthMonitor::report_incomplete_attempt() {
  incomplete_.fetch_add(1, std::memory_order_acq_rel);
}

HealthState HealthMonitor::state() const noexcept { return state_.load(std::memory_order_acquire); }

HealthSnapshot HealthMonitor::snapshot() const {
  HealthSnapshot s;
  s.state = state();
  s.process_live = (s.state != HealthState::kDown);
  s.accepting = svc_ ? svc_->accepting() : false;
  s.triton_ready = triton_ ? triton_->is_server_ready() : false;
  s.readback_consistent = startup_.ready;
  s.observed_at_unix_ms = now_ms();
  s.incomplete_attempts = incomplete_.load(std::memory_order_acquire);
  return s;
}

bool HealthMonitor::readiness_ok() const {
  if (state() != HealthState::kReady)
    return false;
  if (!svc_ || !svc_->accepting())
    return false;
  if (!triton_ || !triton_->is_server_ready())
    return false;
  if (!startup_.ready)
    return false;
  return true;
}

} // namespace masi::inf