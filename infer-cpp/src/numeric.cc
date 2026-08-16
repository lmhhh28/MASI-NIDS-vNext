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
  if (scores.size() != p.class_order.size())
    throw error::Exception(error::Code::kIncompatibleContract, "output class count mismatch");
  // Class order is fixed in the profile and the model output; we additionally
  // verify here that every label has a unique output_index and is a dense
  // 0..N-1 index into the canonical score vector.
  std::vector<uint32_t> seen;
  seen.reserve(p.class_order.size());
  for (const auto& c : p.class_order) {
    if (c.label >= p.class_order.size())
      throw error::Exception(error::Code::kIncompatibleContract, "class label out of range");
    if (c.output_index >= scores.size())
      throw error::Exception(error::Code::kIncompatibleContract, "class output_index out of range");
    for (uint32_t s : seen)
      if (s == c.output_index)
        throw error::Exception(error::Code::kIncompatibleContract, "duplicate class output_index");
    seen.push_back(c.output_index);
  }
}

NumericResult apply_output_adapter(const std::vector<float>& raw_scores,
                                   const NumericProfile& p) {
  assert_output_finite(raw_scores);
  assert_class_order(raw_scores, p);

  NumericResult r;
  r.scores.resize(p.class_order.size());

  // Reorder raw_scores into canonical class order.
  for (const auto& c : p.class_order) r.scores[c.label] = raw_scores[c.output_index];

  // argmax over canonical scores.
  uint32_t argmax = 0;
  float best = r.scores[0];
  for (uint32_t i = 1; i < r.scores.size(); ++i) {
    if (r.scores[i] > best) {
      best = r.scores[i];
      argmax = i;
    }
  }
  r.predicted_label = argmax;

  // Deterministic OOD: any single class score exceeds the explicit threshold.
  r.out_of_distribution = false;
  for (float s : r.scores) {
    if (s > p.ood_threshold) { r.out_of_distribution = true; break; }
  }

  // Deterministic abstain: max score below the abstain threshold.
  r.abstain = (best < p.abstain_threshold);

  if (r.abstain) {
    r.decision = "ABSTAIN";
  } else if (!r.out_of_distribution && best >= p.alert_threshold) {
    r.decision = "ALERT";
  } else if (!r.out_of_distribution) {
    r.decision = "BENIGN";
  } else {
    // OOD but above abstain: deterministic abstain-equivalent.
    r.decision = "ABSTAIN";
    r.abstain = true;
  }

  r.quality = r.out_of_distribution ? "INVALID" : "VALID";
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