#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "error.h"

namespace masi::inf {

// Canonical class-order label descriptor. `output_index` is the position in
// the raw model output tensor that corresponds to `label`. The output adapter
// only emits predictions in this explicit order.
struct ClassLabel {
  uint32_t label = 0;
  std::string name;
  uint32_t output_index = 0;
};

struct NumericProfile {
  // Tolerances for float comparison (golden numeric test / readback).
  double abs_tol = 1e-6;
  double rel_tol = 1e-6;
  uint64_t ulp_tol = 4;
  // Thresholds for the deterministic OOD / abstain / decision rules.
  double ood_threshold = 0.0;       // score > threshold => out_of_distribution
  double abstain_threshold = 0.0;   // max score < threshold => abstain
  double alert_threshold = 0.5;     // >= threshold => ALERT, else BENIGN
  bool softmax_output = false;      // if true, scores are post-softmax probs
  std::vector<ClassLabel> class_order;
};

struct NumericResult {
  std::vector<float> scores;
  uint32_t predicted_label = 0;
  std::string decision;        // "BENIGN" | "ALERT" | "ABSTAIN"
  bool out_of_distribution = false;
  bool abstain = false;
  std::string quality;         // "VALID" | "INVALID"
};

// Reject NaN/Inf in the input tensor (checked before model execution).
void assert_input_finite(const std::vector<uint8_t>& bytes, const std::string& dtype);

// Reject NaN/Inf in a raw float output buffer.
void assert_output_finite(const std::vector<float>& scores);

// Verify the raw output shape and class order matches the profile.
void assert_class_order(const std::vector<float>& scores, const NumericProfile& p);

// Deterministic OOD/abstain/decision computation. No randomness, no learned
// threshold beyond the explicit profile.
NumericResult apply_output_adapter(const std::vector<float>& raw_scores,
                                   const NumericProfile& p);

// Float tolerance check: absolute / relative / ULP.
bool float_within_tolerance(float a, float b, const NumericProfile& p);

}  // namespace masi::inf