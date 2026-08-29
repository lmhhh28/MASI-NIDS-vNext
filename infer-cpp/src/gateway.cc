#include "gateway.h"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <set>
#include <sstream>
#include <string_view>

#include "digest.h"
#include "numeric.h"
#include "repository_closure.h"
#include "tls.h"

namespace masi::inf {

namespace {

constexpr std::string_view kEmptyPayloadDigest =
    "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

int64_t now_ms() {
  using namespace std::chrono;
  return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

// Canonical float32 little-endian encoding of the class-ordered scores.
// contracts/inference/v1/profile.json#output_adapter_binding.
std::string canonical_score_bytes(const std::vector<float> &scores) {
  std::string out;
  out.resize(scores.size() * sizeof(float));
  for (size_t i = 0; i < scores.size(); ++i) {
    uint32_t bits;
    std::memcpy(&bits, &scores[i], sizeof(bits));
    // Little-endian on the wire regardless of host byte order.
    out[i * 4 + 0] = static_cast<char>(bits & 0xff);
    out[i * 4 + 1] = static_cast<char>((bits >> 8) & 0xff);
    out[i * 4 + 2] = static_cast<char>((bits >> 16) & 0xff);
    out[i * 4 + 3] = static_cast<char>((bits >> 24) & 0xff);
  }
  return out;
}

grpc::Status to_grpc(const error::Exception &e) {
  grpc::StatusCode sc = grpc::StatusCode::INTERNAL;
  switch (e.code()) {
  case error::Code::kResourceExhausted:
  case error::Code::kInferenceMessageTooLarge:
  case error::Code::kBufferOverflow:
    sc = grpc::StatusCode::RESOURCE_EXHAUSTED;
    break;
  case error::Code::kDeadlineExceeded:
    sc = grpc::StatusCode::DEADLINE_EXCEEDED;
    break;
  case error::Code::kPoolUnavailable:
    sc = grpc::StatusCode::UNAVAILABLE;
    break;
  case error::Code::kFenced:
  case error::Code::kRouteFenceMismatch:
  case error::Code::kResultIdentityMismatch:
  case error::Code::kResultDigestConflict:
    sc = grpc::StatusCode::FAILED_PRECONDITION;
    break;
  case error::Code::kAborted:
    sc = grpc::StatusCode::ABORTED;
    break;
  case error::Code::kIncompatibleContract:
  case error::Code::kInvalidManifest:
  case error::Code::kInstanceGroupImplicit:
  case error::Code::kProviderPartitionDrift:
  case error::Code::kRepositoryClosureViolation:
  case error::Code::kRuntimeProfileUnsupported:
    sc = grpc::StatusCode::INVALID_ARGUMENT;
    break;
  default:
    break;
  }
  return grpc::Status(sc, std::string(error::to_string(e.code())), e.what());
}

} // namespace

grpc::Status validate_triton_output_shape(const TritonInferResult &result, size_t record_count,
                                          size_t class_count) {
  if (result.values.size() != record_count * class_count)
    return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION, "READBACK_MISMATCH",
                        "triton output element count does not match the batch");
  if (result.shape.size() != 2 || result.shape[0] != static_cast<int64_t>(record_count) ||
      result.shape[1] != static_cast<int64_t>(class_count))
    return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION, "READBACK_MISMATCH",
                        "triton output shape does not match the batch");
  return grpc::Status::OK;
}

grpc::Status validate_route_fence(const masi::edge::v1::InferenceRoute &route,
                                  const PoolReadback &rb) {
  auto mismatch = [](const std::string &what) {
    return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION, "RESULT_IDENTITY_MISMATCH", what);
  };
  if (route.schema_version() != "inference-route/v1")
    return mismatch("schema_version");
  for (const auto &identity : {
           std::pair<const char *, const std::string &>{"shard_id", route.shard_id()},
           {"model_control_incarnation_id", route.model_control_incarnation_id()},
           {"logical_pool_id", route.logical_pool_id()},
           {"operation_id", route.operation_id()},
           {"scope", route.scope()},
       }) {
    if (identity.second.empty())
      return mismatch(identity.first);
  }
  if (route.route_epoch() == 0)
    return mismatch("route_epoch");
  if (route.expected_binding_generation() >= route.proposed_binding_generation())
    return mismatch("expected_binding_generation");
  if (route.binding_generation() != route.proposed_binding_generation())
    return mismatch("proposed_binding_generation");
  if (route.binding_generation() != route.current_binding_generation())
    return mismatch("current_binding_generation");
  if (route.model_control_incarnation_id() != rb.model_control_incarnation_id)
    return mismatch("model_control_incarnation_id");
  if (route.operation_id() != rb.operation_id)
    return mismatch("operation_id");
  if (route.logical_pool_id() != rb.logical_pool_id)
    return mismatch("logical_pool_id");
  if (route.pool_generation() != rb.pool_generation)
    return mismatch("pool_generation");
  if (route.binding_generation() != rb.proposed_binding_generation)
    return mismatch("binding_generation");
  if (route.startup_envelope_digest() != rb.startup_envelope_digest)
    return mismatch("startup_envelope_digest");
  if (route.model_revision_digest() != rb.model_revision_digest)
    return mismatch("model_revision_digest");
  if (route.model_bundle_digest() != rb.model_bundle_digest)
    return mismatch("model_bundle_digest");
  if (route.feature_contract_digest() != rb.feature_contract_digest)
    return mismatch("feature_contract_digest");
  if (route.label_contract_digest() != rb.label_contract_digest)
    return mismatch("label_contract_digest");
  if (route.output_adapter_digest() != rb.output_adapter_digest)
    return mismatch("output_adapter_digest");
  if (route.wire_profile() != rb.wire_profile)
    return mismatch("wire_profile");
  if (route.wire_profile_digest() != rb.wire_profile_digest)
    return mismatch("wire_profile_digest");
  if (route.runtime_profile() != rb.runtime_profile)
    return mismatch("runtime_profile");
  if (route.runtime_profile_digest() != rb.runtime_profile_digest)
    return mismatch("runtime_profile_digest");
  if (route.optimization_profile_digest() != rb.optimization_profile_digest)
    return mismatch("optimization_profile_digest");
  if (route.pool_observation_digest() != rb.pool_observation_digest)
    return mismatch("pool_observation_digest");
  if (route.binding_digest() != rb.binding_digest)
    return mismatch("binding_digest");

  for (const auto &digest : {
           std::pair<const char *, const std::string &>{"startup_envelope_digest",
                                                        route.startup_envelope_digest()},
           {"pool_observation_digest", route.pool_observation_digest()},
           {"binding_digest", route.binding_digest()},
           {"model_revision_digest", route.model_revision_digest()},
           {"model_bundle_digest", route.model_bundle_digest()},
           {"feature_contract_digest", route.feature_contract_digest()},
           {"label_contract_digest", route.label_contract_digest()},
           {"output_adapter_digest", route.output_adapter_digest()},
           {"wire_profile_digest", route.wire_profile_digest()},
           {"runtime_profile_digest", route.runtime_profile_digest()},
           {"optimization_profile_digest", route.optimization_profile_digest()},
       }) {
    if (!is_sha256_digest(digest.second))
      return mismatch(digest.first);
  }
  return grpc::Status::OK;
}

grpc::Status validate_record_fence(const masi::edge::v1::InferenceRoute &route,
                                   const masi::edge::v1::InferenceRecord &rec) {
  auto mismatch = [](const std::string &what) {
    return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION, "RESULT_IDENTITY_MISMATCH", what);
  };
  for (const auto &identity : {
           std::pair<const char *, const std::string &>{"input_id", rec.input_id()},
           {"event_idempotency_key", rec.event_idempotency_key()},
           {"target_id", rec.target_id()},
           {"source_runtime_epoch", rec.source_runtime_epoch()},
           {"window_id", rec.window_id()},
           {"operation_id", rec.operation_id()},
           {"scope", rec.scope()},
       }) {
    if (identity.second.empty())
      return mismatch(identity.first);
  }
  if (rec.schema_version() != "edge-inference-record/v1")
    return mismatch("record.schema_version");
  if (!rec.final_window() || rec.quality() != "valid" ||
      rec.quality_code() != masi::edge::v1::DATA_QUALITY_VALID)
    return mismatch("source_window_identity");
  if (rec.source_sequence_start() > rec.source_sequence_end() ||
      rec.window_start_unix_ms() >= rec.window_end_unix_ms() ||
      rec.watermark_unix_ms() < rec.window_end_unix_ms() ||
      rec.finalized_at_unix_ms() < rec.window_end_unix_ms() ||
      rec.enqueued_at_unix_ms() < rec.finalized_at_unix_ms())
    return mismatch("source_window_identity");
  if (rec.source_wal_sequence() == 0 || rec.input_wal_sequence() == 0)
    return mismatch("source_input_result_WAL_sequence");
  if (rec.quality_reasons_size() != 1 || rec.quality_reasons(0) != "NONE" ||
      rec.sampling_coverage_ppm() > 1000000)
    return mismatch("source_window_identity");
  if (rec.target_id() != route.shard_id())
    return mismatch("shard_id");

#define MASI_RECORD_ROUTE_EQ(field)                                                                \
  if (rec.field() != route.field())                                                                \
  return mismatch(#field)
  MASI_RECORD_ROUTE_EQ(model_control_incarnation_id);
  MASI_RECORD_ROUTE_EQ(logical_pool_id);
  MASI_RECORD_ROUTE_EQ(pool_generation);
  MASI_RECORD_ROUTE_EQ(binding_generation);
  MASI_RECORD_ROUTE_EQ(route_epoch);
  MASI_RECORD_ROUTE_EQ(model_revision_digest);
  MASI_RECORD_ROUTE_EQ(feature_contract_digest);
  MASI_RECORD_ROUTE_EQ(label_contract_digest);
  MASI_RECORD_ROUTE_EQ(output_adapter_digest);
  MASI_RECORD_ROUTE_EQ(wire_profile);
  MASI_RECORD_ROUTE_EQ(runtime_profile);
  MASI_RECORD_ROUTE_EQ(operation_id);
  MASI_RECORD_ROUTE_EQ(scope);
  MASI_RECORD_ROUTE_EQ(expected_binding_generation);
  MASI_RECORD_ROUTE_EQ(proposed_binding_generation);
  MASI_RECORD_ROUTE_EQ(current_binding_generation);
  MASI_RECORD_ROUTE_EQ(startup_envelope_digest);
  MASI_RECORD_ROUTE_EQ(pool_observation_digest);
  MASI_RECORD_ROUTE_EQ(binding_digest);
  MASI_RECORD_ROUTE_EQ(model_bundle_digest);
  MASI_RECORD_ROUTE_EQ(wire_profile_digest);
  MASI_RECORD_ROUTE_EQ(runtime_profile_digest);
  MASI_RECORD_ROUTE_EQ(optimization_profile_digest);
#undef MASI_RECORD_ROUTE_EQ
  return grpc::Status::OK;
}

CentralInferenceServiceImpl::InFlightGate::InFlightGate(CentralInferenceServiceImpl *owner,
                                                        const std::string &route_key)
    : owner_(owner), route_key_(route_key) {
  std::lock_guard<std::mutex> g(owner_->mu_);
  const uint64_t current = owner_->in_flight_.load(std::memory_order_acquire);
  if (current >= static_cast<uint64_t>(owner_->cfg_.max_in_flight)) {
    reason_ = "gateway in-flight limit reached";
    return;
  }
  if (owner_->in_flight_routes_.count(route_key_) > 0) {
    reason_ = "maximum_in_flight_batches_per_target exceeded";
    return;
  }
  owner_->in_flight_routes_.insert(route_key_);
  owner_->in_flight_.fetch_add(1, std::memory_order_acq_rel);
  acquired_ = true;
}

CentralInferenceServiceImpl::InFlightGate::~InFlightGate() {
  if (!acquired_)
    return;
  std::lock_guard<std::mutex> g(owner_->mu_);
  owner_->in_flight_routes_.erase(route_key_);
  owner_->in_flight_.fetch_sub(1, std::memory_order_acq_rel);
}

CentralInferenceServiceImpl::CentralInferenceServiceImpl(Config cfg, StartupResult startup,
                                                         std::shared_ptr<TritonClient> triton)
    : cfg_(std::move(cfg)), startup_(std::move(startup)), triton_(std::move(triton)) {
  // Wire profile constants from contracts/inference/v1/profile.json.
  profile_.schema_version = "inference-central-grpc-batch-profile/v1";
  profile_.profile_id = "inference-central-grpc-batch/v1";
  profile_.maximum_records_per_batch = cfg_.max_records_per_batch;
  profile_.maximum_request_bytes = cfg_.max_request_bytes;
  profile_.maximum_response_bytes = cfg_.max_response_bytes;
  profile_.request_deadline_ms = cfg_.request_deadline_ms;

  // Adapter parameters and Triton identity come from the verified startup
  // state (digest-pinned closure), never from request data or code constants.
  numeric_ = startup_.numeric;
  triton_model_name_ = startup_.triton_model_name;
  triton_model_version_ = startup_.triton_model_version;
  triton_input_name_ = startup_.observed.config.input_name;
}

void CentralInferenceServiceImpl::drain_begin() { accepting_.store(false); }
void CentralInferenceServiceImpl::shutdown_complete() { accepting_.store(false); }
bool CentralInferenceServiceImpl::accepting() const noexcept {
  return accepting_.load(std::memory_order_acquire);
}
uint64_t CentralInferenceServiceImpl::in_flight() const noexcept {
  return in_flight_.load(std::memory_order_acquire);
}

std::string CentralInferenceServiceImpl::next_worker_attempt_id() {
  const uint64_t n = attempt_counter_.fetch_add(1, std::memory_order_acq_rel) + 1;
  std::ostringstream oss;
  oss << startup_.readback.readback_attempt_id << ':' << n;
  const std::string s = oss.str();
  return sha256_hex(s.data(), s.size());
}

grpc::Status CentralInferenceServiceImpl::GetBinding(grpc::ServerContext *ctx,
                                                     const masi::edge::v1::GetBindingRequest *req,
                                                     masi::edge::v1::BindingReadback *resp) {
  const auto peer = check_peer_identity(*ctx, cfg_.client_san_allowlist);
  if (!peer.allowed)
    return grpc::Status(grpc::StatusCode::UNAUTHENTICATED, peer.reason);
  if (!accepting())
    return grpc::Status(grpc::StatusCode::UNAVAILABLE, "draining");
  if (!startup_.ready)
    return grpc::Status(grpc::StatusCode::UNAVAILABLE, "not ready");

  // Verify the request matches the exact loaded binding.
  const auto &rb = startup_.readback;
  if (req->logical_pool_id() != rb.logical_pool_id ||
      req->pool_generation() != rb.pool_generation ||
      req->binding_generation() != rb.proposed_binding_generation ||
      req->model_control_incarnation_id() != rb.model_control_incarnation_id) {
    return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION, "RESULT_IDENTITY_MISMATCH",
                        "binding identity mismatch");
  }
  *resp = to_binding_readback_proto(rb);
  (void)ctx;
  return grpc::Status::OK;
}

grpc::Status
CentralInferenceServiceImpl::check_route_fence(const masi::edge::v1::InferenceRoute &route) const {
  return validate_route_fence(route, startup_.readback);
}

grpc::Status CentralInferenceServiceImpl::Infer(grpc::ServerContext *ctx,
                                                const masi::edge::v1::InferenceInputBatch *req,
                                                masi::edge::v1::InferenceResultBatch *resp) {
  const auto peer = check_peer_identity(*ctx, cfg_.client_san_allowlist);
  if (!peer.allowed)
    return grpc::Status(grpc::StatusCode::UNAUTHENTICATED, peer.reason);
  if (!accepting())
    return grpc::Status(grpc::StatusCode::UNAVAILABLE, "draining");
  if (!startup_.ready)
    return grpc::Status(grpc::StatusCode::UNAVAILABLE, "not ready");
  if (!triton_ || !triton_->available())
    return grpc::Status(grpc::StatusCode::UNAVAILABLE, "POOL_UNAVAILABLE",
                        "no Triton execution plane");

  const auto &route = req->route();
  const int64_t deadline_unix_ms = req->deadline_unix_ms();
  if (deadline_unix_ms <= now_ms())
    return grpc::Status(grpc::StatusCode::DEADLINE_EXCEEDED, "deadline already exceeded");
  if (deadline_unix_ms - now_ms() > cfg_.request_deadline_ms)
    return grpc::Status(grpc::StatusCode::INVALID_ARGUMENT,
                        "deadline exceeds frozen request budget");

  // Bounded in-flight: one batch per route target plus a process-wide cap.
  const std::string route_key = route.shard_id() + "|" + std::to_string(route.route_epoch()) + "|" +
                                std::to_string(route.binding_generation());
  InFlightGate gate(this, route_key);
  if (!gate.acquired())
    return grpc::Status(grpc::StatusCode::RESOURCE_EXHAUSTED, gate.reason());

  // Admission: per-record layout/dtype/digest plus batch-level bounds.
  BatchAdmissionInput ai;
  ai.request_id = req->request_id();
  ai.schema_version = req->schema_version();
  ai.wire_profile = route.wire_profile();
  ai.deadline_unix_ms = deadline_unix_ms;
  ai.request_bytes = req->ByteSizeLong();
  ai.route_shard_id = route.shard_id();
  ai.route_epoch = route.route_epoch();
  ai.pool_generation = route.pool_generation();
  ai.binding_generation = route.binding_generation();
  ai.model_control_incarnation_id = route.model_control_incarnation_id();
  ai.records.reserve(req->records_size());
  for (const auto &rec : req->records()) {
    RecordView rv;
    rv.bytes = reinterpret_cast<const uint8_t *>(rec.feature_tensor().data());
    rv.byte_count = rec.feature_tensor().size();
    for (uint32_t s : rec.shape())
      rv.shape.push_back(s);
    rv.dtype = rec.dtype();
    rv.input_digest = rec.input_digest();
    ai.records.push_back(std::move(rv));
  }

  const auto ad = admit(cfg_, profile_, ai);
  if (ad.verdict == AdmissionVerdict::kFail)
    return grpc::Status(grpc::StatusCode::INVALID_ARGUMENT, ad.reason_code);
  if (ad.verdict == AdmissionVerdict::kHold)
    return grpc::Status(grpc::StatusCode::RESOURCE_EXHAUSTED, ad.reason_code);

  // 29-dimension result fence before any execution.
  auto fence = check_route_fence(route);
  if (!fence.ok())
    return fence;
  if (req->trace_id().empty())
    return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION, "RESULT_IDENTITY_MISMATCH",
                        "trace_id");
  std::set<std::string> input_ids;
  std::set<std::string> event_ids;
  for (const auto &rec : req->records()) {
    auto record_fence = validate_record_fence(route, rec);
    if (!record_fence.ok())
      return record_fence;
    if (!input_ids.insert(rec.input_id()).second ||
        !event_ids.insert(rec.event_idempotency_key()).second)
      return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION, "RESULT_IDENTITY_MISMATCH",
                          "duplicate record identity");
  }

  // Conflict detection only. Same request_id + same batch input digest is
  // recomputed with a new attempt id (at-least-once compute semantics); the
  // Edge accepts the first fully matching result. Same request_id with a
  // different input digest is a hard conflict.
  {
    std::lock_guard<std::mutex> g(mu_);
    const int64_t now = now_ms();
    for (auto it = request_digest_.begin(); it != request_digest_.end();) {
      if (it->second.expires_at_unix_ms <= now)
        it = request_digest_.erase(it);
      else
        ++it;
    }
    auto it = request_digest_.find(req->request_id());
    if (it != request_digest_.end()) {
      if (!digest_match(it->second.input_digest, ad.input_digest))
        return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION, "RESULT_DIGEST_CONFLICT",
                            "request_id reused with a different input_digest");
    } else {
      if (request_digest_.size() >= kRequestCacheCap)
        return grpc::Status(grpc::StatusCode::RESOURCE_EXHAUSTED, "DEDUP_CAPACITY_EXHAUSTED");
      request_digest_.emplace(req->request_id(),
                              RequestDigestEntry{ad.input_digest, deadline_unix_ms});
    }
  }

  if (ctx->IsCancelled())
    return grpc::Status(grpc::StatusCode::CANCELLED, "cancelled before execution");

  // One admitted batch is exactly one Triton ModelInfer call: Triton's dynamic
  // batcher is the only delay-based batcher and the only executor.
  const size_t record_count = ai.records.size();
  const size_t feature_count =
      profile_.tensor_shape.size() == 2 ? static_cast<size_t>(profile_.tensor_shape[1]) : 6;
  std::vector<uint8_t> batch_bytes;
  batch_bytes.reserve(ad.accepted_bytes);
  for (const auto &rv : ai.records)
    batch_bytes.insert(batch_bytes.end(), rv.bytes, rv.bytes + rv.byte_count);
  const std::vector<int64_t> batch_shape{static_cast<int64_t>(record_count),
                                         static_cast<int64_t>(feature_count)};

  // Remaining monotonic budget of this request, shared by the downstream call.
  // The caller-declared deadline is an upper bound the Gateway must respect,
  // but it is not the only one: the configured request_deadline_ms is the
  // Gateway's own explicit bound, so a caller cannot make this replica wait on
  // a stalled backend for longer than its configured budget.
  int64_t remaining_ms = deadline_unix_ms - now_ms();
  if (cfg_.request_deadline_ms > 0)
    remaining_ms = std::min<int64_t>(remaining_ms, cfg_.request_deadline_ms);
  if (remaining_ms <= 0)
    return grpc::Status(grpc::StatusCode::DEADLINE_EXCEEDED, "deadline exceeded before execution");

  const size_t class_count = numeric_.class_order.size();
  if (class_count == 0)
    return grpc::Status(grpc::StatusCode::INTERNAL, "adapter class order empty");

  // Construct a conservative response skeleton before the backend RPC. This
  // uses the request's actual identity lengths plus maximum success strings,
  // score count and digest lengths, so an oversized response is rejected
  // without consuming Triton execution capacity.
  {
    masi::edge::v1::InferenceResultBatch bounded;
    bounded.set_schema_version(req->schema_version());
    bounded.set_request_id(req->request_id());
    bounded.mutable_route()->CopyFrom(route);
    bounded.set_trace_id(req->trace_id());
    bounded.set_batch_digest(std::string(kEmptyPayloadDigest));
    for (const auto &rec : req->records()) {
      auto *out = bounded.add_records();
      fill_result_record(route, rec, req->trace_id(), std::string(kEmptyPayloadDigest), out);
      for (size_t k = 0; k < class_count; ++k)
        out->add_scores(0.0f);
      out->set_decision("abstain");
      out->set_quality("invalid");
      out->set_status("OK");
      out->set_error_code("NONE");
      out->set_output_digest(std::string(kEmptyPayloadDigest));
    }
    if (bounded.ByteSizeLong() > static_cast<size_t>(cfg_.max_response_bytes))
      return grpc::Status(grpc::StatusCode::RESOURCE_EXHAUSTED, "INFERENCE_MESSAGE_TOO_LARGE",
                          "response would exceed maximum_response_bytes");
  }

  TritonInferResult inferred;
  try {
    inferred = triton_->model_infer(
        triton_model_name_, triton_model_version_, triton_input_name_, "UINT64", batch_bytes,
        batch_shape, static_cast<int32_t>(std::min<int64_t>(remaining_ms, 2147483647)), ctx);
  } catch (const error::Exception &e) {
    return to_grpc(e);
  }

  if (auto status = validate_triton_output_shape(inferred, record_count, class_count); !status.ok())
    return status;

  resp->set_schema_version(req->schema_version());
  resp->set_request_id(req->request_id());
  resp->mutable_route()->CopyFrom(route);
  resp->set_trace_id(req->trace_id());

  // Split the batched output row by row and adapt each row.
  const std::string worker_attempt_id = next_worker_attempt_id();
  std::ostringstream batch_preimage;
  for (size_t i = 0; i < record_count; ++i) {
    const auto &rec = req->records(static_cast<int>(i));
    auto *out = resp->add_records();
    fill_result_record(route, rec, req->trace_id(), worker_attempt_id, out);

    using Difference = std::vector<float>::difference_type;
    const auto row_begin = inferred.values.begin() + static_cast<Difference>(i * class_count);
    const auto row_end = inferred.values.begin() + static_cast<Difference>((i + 1) * class_count);
    const std::vector<float> row(row_begin, row_end);
    try {
      const auto adapted = apply_output_adapter(row, numeric_);
      for (float s : adapted.scores)
        out->add_scores(s);
      out->set_predicted_label(adapted.predicted_label);
      out->set_decision(adapted.decision);
      out->set_out_of_distribution(adapted.out_of_distribution);
      out->set_abstain(adapted.abstain);
      out->set_quality(adapted.quality);
      out->set_status("OK");
      out->set_error_code("NONE");
      if (adapted.decision == "alert")
        out->set_decision_code(masi::edge::v1::INFERENCE_DECISION_ALERT);
      else if (adapted.decision == "abstain")
        out->set_decision_code(masi::edge::v1::INFERENCE_DECISION_ABSTAIN);
      else
        out->set_decision_code(masi::edge::v1::INFERENCE_DECISION_BENIGN);
      out->set_execution_status(masi::edge::v1::INFERENCE_EXECUTION_STATUS_OK);
      const std::string canonical = canonical_score_bytes(adapted.scores);
      out->set_output_digest(sha256_hex(canonical.data(), canonical.size()));
    } catch (const error::Exception &e) {
      out->set_decision("abstain");
      out->set_decision_code(masi::edge::v1::INFERENCE_DECISION_ABSTAIN);
      out->set_execution_status(masi::edge::v1::INFERENCE_EXECUTION_STATUS_HOLD);
      out->set_quality("invalid");
      out->set_error_code(e.what());
    }
    out->set_inference_completed_at_unix_ms(now_ms());
    batch_preimage << out->output_digest() << '\n';
  }

  const std::string preimage = batch_preimage.str();
  resp->set_batch_digest(sha256_hex(preimage.data(), preimage.size()));
  if (resp->ByteSizeLong() > static_cast<size_t>(cfg_.max_response_bytes)) {
    resp->Clear();
    return grpc::Status(grpc::StatusCode::RESOURCE_EXHAUSTED, "INFERENCE_MESSAGE_TOO_LARGE",
                        "response exceeds maximum_response_bytes");
  }
  return grpc::Status::OK;
}

void CentralInferenceServiceImpl::fill_result_record(
    const masi::edge::v1::InferenceRoute &route, const masi::edge::v1::InferenceRecord &rec,
    const std::string &trace_id, const std::string &worker_attempt_id,
    masi::edge::v1::InferenceResultRecord *out) const {
  out->set_input_id(rec.input_id());
  out->set_event_idempotency_key(rec.event_idempotency_key());
  // Verified in admission against the recomputed tensor digest.
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
  out->set_source_wal_sequence(rec.source_wal_sequence());
  out->set_input_wal_sequence(rec.input_wal_sequence());
  out->set_result_wal_sequence(0);
  out->set_worker_id(startup_.readback.worker.worker_id);
  out->set_worker_digest(startup_.readback.worker.worker_digest);
  out->set_worker_attempt_id(worker_attempt_id);
  out->set_inference_started_at_unix_ms(now_ms());
  out->set_trace_id(trace_id);

  // Identity copy-through from the fenced route/record.
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
  out->set_pool_observation_digest(startup_.readback.pool_observation_digest);
  out->set_binding_digest(startup_.readback.binding_digest);
  out->set_model_bundle_digest(startup_.readback.model_bundle_digest);
  out->set_wire_profile_digest(startup_.readback.wire_profile_digest);
  out->set_runtime_profile_digest(startup_.readback.runtime_profile_digest);
  out->set_optimization_profile_digest(startup_.readback.optimization_profile_digest);
  out->set_operation_id(rec.operation_id());
  out->set_scope(rec.scope());
  out->set_expected_binding_generation(rec.expected_binding_generation());
  out->set_proposed_binding_generation(rec.proposed_binding_generation());
  out->set_current_binding_generation(rec.current_binding_generation());
}

} // namespace masi::inf
