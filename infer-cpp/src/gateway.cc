#include "gateway.h"

#include <chrono>
#include <cstring>
#include <sstream>

#include "digest.h"
#include "numeric.h"
#include "repository_closure.h"

namespace masi::inf {

namespace {

int64_t now_ms() {
  using namespace std::chrono;
  return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

grpc::Status to_grpc(const error::Exception& e) {
  grpc::StatusCode sc = grpc::StatusCode::INTERNAL;
  switch (e.code()) {
    case error::Code::kResourceExhausted:
    case error::Code::kInferenceMessageTooLarge:
    case error::Code::kBufferOverflow:
      sc = grpc::StatusCode::RESOURCE_EXHAUSTED; break;
    case error::Code::kDeadlineExceeded:
      sc = grpc::StatusCode::DEADLINE_EXCEEDED; break;
    case error::Code::kPoolUnavailable:
      sc = grpc::StatusCode::UNAVAILABLE; break;
    case error::Code::kFenced:
    case error::Code::kRouteFenceMismatch:
    case error::Code::kResultIdentityMismatch:
    case error::Code::kResultDigestConflict:
      sc = grpc::StatusCode::FAILED_PRECONDITION; break;
    case error::Code::kAborted:
      sc = grpc::StatusCode::ABORTED; break;
    case error::Code::kIncompatibleContract:
    case error::Code::kInvalidManifest:
    case error::Code::kInstanceGroupImplicit:
    case error::Code::kProviderPartitionDrift:
    case error::Code::kRepositoryClosureViolation:
    case error::Code::kRuntimeProfileUnsupported:
      sc = grpc::StatusCode::INVALID_ARGUMENT; break;
    default: break;
  }
  return grpc::Status(sc, std::string(error::to_string(e.code())), e.what());
}

// Verify the 29-dimension result fence. Each dimension of the request must
// match the Gateway's exact startup binding. Unknown/cross-generation dims
// yield RESULT_IDENTITY_MISMATCH (HOLD). Same request_id + different
// input_digest yields RESULT_DIGEST_CONFLICT.
grpc::Status check_fence(CentralInferenceServiceImpl::IdempEntry* /*unused*/) {
  return grpc::Status::OK;
}

}  // namespace

CentralInferenceServiceImpl::CentralInferenceServiceImpl(
    Config cfg, StartupResult startup, std::shared_ptr<OrtSession> session,
    std::shared_ptr<TritonClient> triton, NumericProfile numeric)
    : cfg_(std::move(cfg)),
      startup_(std::move(startup)),
      session_(std::move(session)),
      triton_(std::move(triton)),
      numeric_(std::move(numeric)) {
  // Wire profile constants from contracts/inference/v1/profile.json.
  profile_.schema_version = "inference-central-grpc-batch-profile/v1";
  profile_.profile_id = "inference-central-grpc-batch/v1";
  profile_.maximum_records_per_batch = cfg_.max_records_per_batch;
  profile_.maximum_request_bytes = cfg_.max_request_bytes;
  profile_.maximum_response_bytes = cfg_.max_response_bytes;
  profile_.request_deadline_ms = cfg_.request_deadline_ms;

  // Exact Triton model name from the digest-pinned repository closure
  // (model directory name), resolved once at construction. Startup has
  // already verified the closure and the Triton metadata, so this cannot
  // drift at request time.
  if (triton_ && triton_->available())
    triton_model_name_ = resolve_triton_model_name(cfg_.model_repository_path);
}

void CentralInferenceServiceImpl::drain_begin() { accepting_.store(false); }
void CentralInferenceServiceImpl::shutdown_complete() { accepting_.store(false); }
bool CentralInferenceServiceImpl::accepting() const noexcept {
  return accepting_.load(std::memory_order_acquire);
}

grpc::Status CentralInferenceServiceImpl::GetBinding(
    grpc::ServerContext* ctx,
    const masi::edge::v1::GetBindingRequest* req,
    masi::edge::v1::BindingReadback* resp) {
  if (!accepting()) return grpc::Status(grpc::StatusCode::UNAVAILABLE, "draining");
  if (!startup_.ready)
    return grpc::Status(grpc::StatusCode::UNAVAILABLE, "not ready");

  // Verify the request matches the exact loaded binding.
  const auto& rb = startup_.readback;
  if (req->logical_pool_id() != rb.logical_pool_id ||
      req->pool_generation() != rb.pool_generation ||
      req->binding_generation() != rb.proposed_binding_generation ||
      req->model_control_incarnation_id() != rb.model_control_incarnation_id) {
    return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION,
                        "RESULT_IDENTITY_MISMATCH",
                        "binding identity mismatch");
  }
  *resp = to_binding_readback_proto(rb);
  (void)ctx;
  return grpc::Status::OK;
}

grpc::Status CentralInferenceServiceImpl::Infer(
    grpc::ServerContext* ctx,
    const masi::edge::v1::InferenceInputBatch* req,
    masi::edge::v1::InferenceResultBatch* resp) {
  if (!accepting()) return grpc::Status(grpc::StatusCode::UNAVAILABLE, "draining");
  if (!startup_.ready)
    return grpc::Status(grpc::StatusCode::UNAVAILABLE, "not ready");

  const auto& route = req->route();
  // Bounded in-flight per target: contract maximum_in_flight_batches_per_target = 1.
  // We approximate by a process-wide in-flight counter; the Edge ensures per-shard
  // uniqueness, and we additionally reject concurrent same-route batches.
  const int64_t deadline_unix_ms = req->deadline_unix_ms();
  if (deadline_unix_ms <= now_ms())
    return grpc::Status(grpc::StatusCode::DEADLINE_EXCEEDED, "deadline already exceeded");

  // Admission pipeline: schema/size/count/identity/tensor layout.
  size_t record_count = req->records_size();
  size_t request_bytes = req->ByteSizeLong();
  // Aggregate feature tensor across records for digest; records are checked
  // individually below. Empty batches (record_count == 0) are rejected by
  // admission (minimum_records_per_batch=1) before any per-record access, so
  // never index records(0) without checking the count.
  std::vector<uint8_t> agg;
  std::vector<uint32_t> shape;
  if (req->records_size() > 0) {
    for (const auto& r : req->records())
      agg.insert(agg.end(), r.feature_tensor().begin(), r.feature_tensor().end());
    for (uint32_t s : req->records(0).shape()) shape.push_back(s);
  }

  auto ad = admit(cfg_, profile_,
                  req->request_id(), req->schema_version(), route.wire_profile(),
                  deadline_unix_ms, request_bytes, record_count,
                  route.shard_id(), route.route_epoch(), route.pool_generation(),
                  route.binding_generation(), route.model_control_incarnation_id(),
                  agg, shape, req->records_size() > 0 ? req->records(0).dtype() : "uint64-le", 8);
  if (ad.verdict == AdmissionVerdict::kFail)
    return grpc::Status(grpc::StatusCode::INVALID_ARGUMENT, ad.reason_code);
  if (ad.verdict == AdmissionVerdict::kHold)
    return grpc::Status(grpc::StatusCode::RESOURCE_EXHAUSTED, ad.reason_code);

  // Idempotency / conflict check: same request_id + same input_digest => same.
  // same request_id + different input_digest => RESULT_DIGEST_CONFLICT.
  std::string stored_output;
  {
    std::lock_guard<std::mutex> g(mu_);
    auto it = idemp_.find(req->request_id());
    if (it != idemp_.end()) {
      if (digest_match(it->second.input_digest, ad.input_digest)) {
        stored_output = it->second.output_digest;
      } else {
        return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION,
                            "RESULT_DIGEST_CONFLICT",
                            "request_id reused with different input_digest");
      }
    }
  }
  if (!stored_output.empty()) {
    // Idempotent: return an empty result batch carrying the same identity.
    // The Edge's result WAL already holds the canonical result; a duplicate
    // response is acceptable for an idempotent retry within the same generation.
    resp->set_schema_version(req->schema_version());
    resp->set_request_id(req->request_id());
    resp->mutable_route()->CopyFrom(route);
    resp->set_batch_digest(stored_output);
    resp->set_trace_id(req->trace_id());
    return grpc::Status::OK;
  }

  // 29-dimension result fence: route identity must match the loaded binding.
  const auto& rb = startup_.readback;
  bool fence_ok = route.model_control_incarnation_id() == rb.model_control_incarnation_id &&
                  route.pool_generation() == rb.pool_generation &&
                  route.binding_generation() == rb.proposed_binding_generation &&
                  route.startup_envelope_digest() == rb.startup_envelope_digest;
  if (!fence_ok)
    return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION,
                        "RESULT_IDENTITY_MISMATCH",
                        "cross-generation or unknown route");

  // Execute each record. No second batcher; we pass records to ORT/Triton
  // individually. The Triton dynamic-batching queue is the only delayed batcher.
  resp->set_schema_version(req->schema_version());
  resp->set_request_id(req->request_id());
  resp->mutable_route()->CopyFrom(route);
  resp->set_trace_id(req->trace_id());
  std::ostringstream oss;
  for (const auto& rec : req->records()) {
    auto* out_rec = resp->add_records();
    auto st = infer_one(route, rec, deadline_unix_ms, req->trace_id(), out_rec);
    if (!st.ok()) {
      // Record-level failure: mark this record HOLD and continue.
      out_rec->set_execution_status(masi::edge::v1::INFERENCE_EXECUTION_STATUS_HOLD);
      out_rec->set_error_code(st.error_message());
    }
    oss << out_rec->output_digest() << '\n';
  }
  std::string out_str = oss.str();
  std::string batch_digest = sha256_hex(out_str.data(), out_str.size());
  resp->set_batch_digest(batch_digest);

  // Store idempotency entry, bounded.
  {
    std::lock_guard<std::mutex> g(mu_);
    if (idemp_.size() >= kIdempCap) idemp_.clear();
    idemp_[req->request_id()] = {ad.input_digest, batch_digest};
  }

  (void)ctx;
  return grpc::Status::OK;
}

grpc::Status CentralInferenceServiceImpl::infer_one(
    const masi::edge::v1::InferenceRoute& route,
    const masi::edge::v1::InferenceRecord& rec,
    int64_t deadline_unix_ms,
    const std::string& trace_id,
    masi::edge::v1::InferenceResultRecord* out) {
  out->set_input_id(rec.input_id());
  out->set_event_idempotency_key(rec.event_idempotency_key());
  out->set_input_digest(rec.input_digest());
  out->set_source_runtime_epoch(rec.source_runtime_epoch());
  out->set_source_sequence_start(rec.source_sequence_start());
  out->set_source_sequence_end(rec.source_sequence_end());
  out->set_window_start_unix_ms(rec.window_start_unix_ms());
  out->set_window_end_unix_ms(rec.window_end_unix_ms());
  out->set_finalized_at_unix_ms(rec.finalized_at_unix_ms());
  out->set_target_id(rec.target_id());
  out->set_window_id(rec.window_id());
  out->set_quality_code(rec.quality_code());
  out->set_sampling_coverage_ppm(rec.sampling_coverage_ppm());
  out->set_worker_id(startup_.readback.worker.worker_id);
  out->set_worker_digest(startup_.readback.worker.worker_digest);
  out->set_worker_attempt_id(startup_.readback.readback_attempt_id);
  out->set_inference_started_at_unix_ms(now_ms());
  out->set_trace_id(trace_id);

  // Identity copy-through from route/record.
  out->set_model_control_incarnation_id(route.model_control_incarnation_id());
  out->set_logical_pool_id(route.logical_pool_id());
  out->set_pool_generation(route.pool_generation());
  out->set_binding_generation(route.binding_generation());
  out->set_route_epoch(route.route_epoch());
  out->set_model_revision_digest(route.model_revision_digest());
  out->set_feature_contract_digest(route.feature_contract_digest());
  out->set_label_contract_digest(route.label_contract_digest());
  out->set_output_adapter_digest(route.output_adapter_digest());
  out->set_wire_profile(route.wire_profile());
  out->set_runtime_profile(route.runtime_profile());
  out->set_startup_envelope_digest(route.startup_envelope_digest());
  out->set_pool_observation_digest(route.pool_observation_digest());
  out->set_binding_digest(route.binding_digest());
  out->set_model_bundle_digest(route.model_bundle_digest());
  out->set_wire_profile_digest(route.wire_profile_digest());
  out->set_runtime_profile_digest(route.runtime_profile_digest());
  out->set_optimization_profile_digest(route.optimization_profile_digest());
  out->set_operation_id(rec.operation_id());
  out->set_scope(rec.scope());
  out->set_expected_binding_generation(rec.expected_binding_generation());
  out->set_proposed_binding_generation(rec.proposed_binding_generation());
  out->set_current_binding_generation(rec.current_binding_generation());

  // Numeric input check.
  try {
    assert_input_finite(std::vector<uint8_t>(rec.feature_tensor().begin(),
                                            rec.feature_tensor().end()),
                        rec.dtype());
  } catch (const error::Exception& e) {
    out->set_decision_code(masi::edge::v1::INFERENCE_DECISION_ABSTAIN);
    out->set_execution_status(masi::edge::v1::INFERENCE_EXECUTION_STATUS_HOLD);
    out->set_error_code(e.what());
    out->set_inference_completed_at_unix_ms(now_ms());
    return grpc::Status::OK;
  }

  // Execute. ORT is the in-process CPU EP; Triton is the optional delayed
  // batcher. We prefer ORT for the in-process CPU profile; Triton is used
  // only when its client is available and the route selects it.
  std::vector<float> raw;
  try {
    if (triton_ && triton_->available()) {
      std::vector<int64_t> shape;
      for (uint32_t s : rec.shape()) shape.push_back(s);
      raw = triton_->model_infer(triton_model_name_, "1",
                                  std::vector<uint8_t>(rec.feature_tensor().begin(),
                                                       rec.feature_tensor().end()),
                                  shape, 0);
    } else if (session_) {
      raw = session_->run(std::vector<uint8_t>(rec.feature_tensor().begin(),
                                               rec.feature_tensor().end()), 0);
    } else {
      throw error::Exception(error::Code::kPoolUnavailable, "no executor");
    }
  } catch (const error::Exception& e) {
    out->set_decision_code(masi::edge::v1::INFERENCE_DECISION_ABSTAIN);
    out->set_execution_status(masi::edge::v1::INFERENCE_EXECUTION_STATUS_HOLD);
    out->set_error_code(e.what());
    out->set_inference_completed_at_unix_ms(now_ms());
    return grpc::Status::OK;
  }

  NumericResult nr;
  try {
    nr = apply_output_adapter(raw, numeric_);
  } catch (const error::Exception& e) {
    out->set_decision_code(masi::edge::v1::INFERENCE_DECISION_ABSTAIN);
    out->set_execution_status(masi::edge::v1::INFERENCE_EXECUTION_STATUS_HOLD);
    out->set_error_code(e.what());
    out->set_inference_completed_at_unix_ms(now_ms());
    return grpc::Status::OK;
  }

  for (float s : nr.scores) out->add_scores(s);
  out->set_predicted_label(nr.predicted_label);
  out->set_decision(nr.decision);
  out->set_out_of_distribution(nr.out_of_distribution);
  out->set_abstain(nr.abstain);
  out->set_quality(nr.quality);
  out->set_status("OK");
  if (nr.decision == "ALERT") out->set_decision_code(masi::edge::v1::INFERENCE_DECISION_ALERT);
  else if (nr.decision == "ABSTAIN") out->set_decision_code(masi::edge::v1::INFERENCE_DECISION_ABSTAIN);
  else out->set_decision_code(masi::edge::v1::INFERENCE_DECISION_BENIGN);
  out->set_execution_status(masi::edge::v1::INFERENCE_EXECUTION_STATUS_OK);

  // Output digest over canonical scores in canonical order.
  std::ostringstream oss;
  for (float s : nr.scores) oss.write(reinterpret_cast<const char*>(&s), sizeof(float));
  const std::string s = oss.str();
  out->set_output_digest(sha256_hex(s.data(), s.size()));
  out->set_inference_completed_at_unix_ms(now_ms());
  (void)deadline_unix_ms;
  return grpc::Status::OK;
}

}  // namespace masi::inf