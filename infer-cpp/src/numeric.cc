#include "numeric.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <sstream>

namespace masi::inf {

namespace {

union FloatBits {
  float f;
  uint32_t u;
};

uint32_t float_to_ulp(float f) {
  FloatBits b;
  b.f = f;
  uint32_t u = b.u;
  if ((u & 0x80000000u) != 0) u = 0x80000000u - u;  // map -x to symmetric
  return u;
}

}  // namespace

void assert_input_finite(const std::vector<uint8_t>& bytes, const std::string& dtype) {
  if (dtype == "uint64-le") {
    // uint64 cannot represent NaN/Inf by construction; the tensor is integer.
    if (bytes.size() % 8 != 0)
      throw error::Exception(error::Code::kBufferOverflow, "uint64 tensor length not multiple of 8");
    return;
  }
  if (dtype == "float32-le") {
    if (bytes.size() % 4 != 0)
      throw error::Exception(error::Code::kBufferOverflow, "float32 tensor length not multiple of 4");
    size_t n = bytes.size() / 4;
    for (size_t i = 0; i < n; ++i) {
      float v;
      std::memcpy(&v, bytes.data() + i * 4, 4);
      if (std::isnan(v) || std::isinf(v))
        throw error::Exception(error::Code::kBufferOverflow, "input tensor NaN/Inf at index " + std::to_string(i));
    }
    return;
  }
  if (dtype == "float64-le") {
    if (bytes.size() % 8 != 0)
      throw error::Exception(error::Code::kBufferOverflow, "float64 tensor length not multiple of 8");
    size_t n = bytes.size() / 8;
    for (size_t i = 0; i < n; ++i) {
      double v;
      std::memcpy(&v, bytes.data() + i * 8, 8);
      if (std::isnan(v) || std::isinf(v))
        throw error::Exception(error::Code::kBufferOverflow, "input tensor NaN/Inf at index " + std::to_string(i));
    }
    return;
  }
  throw error::Exception(error::Code::kIncompatibleContract, "assert_input_finite: unknown dtype " + dtype);
}

void assert_output_finite(const std::vector<float>& scores) {
  for (size_t i = 0; i < scores.size(); ++i) {
    if (std::isnan(scores[i]) || std::isinf(scores[i]))
      throw error::Exception(error::Code::kBufferOverflow, "output tensor NaN/Inf at index " + std::to_string(i));
  }
}

void assert_class_order(const std::vector<float>& scores, const NumericProfile& p) {
  if (p.class_order.empty())
    throw error::Exception(error::Code::kIncompatibleContract, "adapter class_order empty");
  if (scores.size() != p.class_order.size())
    throw error::Exception(error::Code::kIncompatibleContract, "output class count mismatch");
  // The raw model output index equals the label id, so every label in the
  // canonical order must be a unique, in-range index into the raw scores.
  std::vector<uint32_t> seen;
  seen.reserve(p.class_order.size());
  for (uint32_t label : p.class_order) {
    if (label >= scores.size())
      throw error::Exception(error::Code::kIncompatibleContract, "class label out of range");
    for (uint32_t s : seen)
      if (s == label)
        throw error::Exception(error::Code::kIncompatibleContract, "duplicate class label in class_order");
    seen.push_back(label);
  }
}

NumericResult apply_output_adapter(const std::vector<float>& raw_scores,
                                   const NumericProfile& p) {
  assert_output_finite(raw_scores);
  assert_class_order(raw_scores, p);

  // Normalization is selected by the declared score domain, never guessed.
  std::vector<float> normalized(raw_scores.size());
  if (p.score_domain == "logit") {
    const float maxv = *std::max_element(raw_scores.begin(), raw_scores.end());
    double sum = 0.0;
    for (float s : raw_scores) sum += std::exp(static_cast<double>(s) - maxv);
    if (!(sum > 0.0) || !std::isfinite(sum))
      throw error::Exception(error::Code::kBufferOverflow, "softmax sum not finite");
    for (size_t i = 0; i < raw_scores.size(); ++i)
      normalized[i] = static_cast<float>(std::exp(static_cast<double>(raw_scores[i]) - maxv) / sum);
  } else if (p.score_domain == "probability") {
    normalized = raw_scores;
  } else {
    throw error::Exception(error::Code::kIncompatibleContract,
                           "score_domain not supported by adapter: " + p.score_domain);
  }
  assert_output_finite(normalized);

  NumericResult r;
  // Canonical order: position i holds the score of label class_order[i].
  r.scores.resize(p.class_order.size());
  for (size_t i = 0; i < p.class_order.size(); ++i)
    r.scores[i] = normalized[p.class_order[i]];

  // top-1 over canonical scores (top_k is contract-fixed to 1).
  size_t top_index = 0;
  float top_score = r.scores[0];
  for (size_t i = 1; i < r.scores.size(); ++i) {
    if (r.scores[i] > top_score) {
      top_score = r.scores[i];
      top_index = i;
    }
  }
  r.predicted_label = p.class_order[top_index];
  const uint32_t baseline_label = p.class_order[0];

  // This adapter does not compute OOD; the frozen profile declares
  // ood_rule = not-computed-by-this-adapter-always-false.
  r.out_of_distribution = false;

  r.abstain = (static_cast<double>(top_score) < p.abstain_below);
  if (r.abstain) {
    r.decision = "abstain";
  } else if (r.predicted_label != baseline_label &&
             static_cast<double>(top_score) >= p.alert_threshold) {
    r.decision = "alert";
  } else {
    r.decision = "benign";
  }

  r.quality = "valid";
  return r;
}

bool float_within_tolerance(float a, float b, const NumericProfile& p) {
  if (std::isnan(a) || std::isnan(b)) return false;
  double abs_diff = std::fabs(static_cast<double>(a) - static_cast<double>(b));
  if (abs_diff <= p.abs_tol) return true;
  double rel = p.rel_tol * std::max(std::fabs(static_cast<double>(a)),
                                    std::fabs(static_cast<double>(b)));
  if (abs_diff <= rel) return true;
  uint64_t ulp = static_cast<uint64_t>(
      std::abs(static_cast<int64_t>(float_to_ulp(a)) -
               static_cast<int64_t>(float_to_ulp(b))));
  return ulp <= p.ulp_tol;
}

}  // namespace masi::inf