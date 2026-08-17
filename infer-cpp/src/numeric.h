#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "error.h"

namespace masi::inf {

// Deterministic output-adapter parameters. Every field is data supplied by the
// digest-pinned repository closure `role=bundle-manifest` member; the mapping
// implementation itself is qualified Gateway code selected by `adapter_id`.
// See contracts/inference/v1/profile.json#output_adapter_binding.
struct NumericProfile {
  // Tolerances for float comparison (golden numeric test / readback).
  double abs_tol = 1e-6;
  double rel_tol = 1e-6;
  uint64_t ulp_tol = 4;

  // Qualified adapter identity (verified against the frozen profile).
  std::string adapter_id;
  std::string adapter_digest;

  // label_taxonomy.score_domain: "logit" => stable softmax before thresholds,
  // "probability" => no normalization. Anything else is rejected earlier.
  std::string score_domain = "probability";

  // Canonical class order: position i holds label class_order[i]; the raw
  // model output index equals the label id. class_order[0] is the baseline
  // (no-alert) label.
  std::vector<uint32_t> class_order;

  // output_adapter.threshold: alert floor in the normalized score domain.
  double alert_threshold = 0.0;
  // label_taxonomy.threshold.value: abstain floor in the normalized domain.
  double abstain_below = 0.0;
  // Explicit OOD rule from the digest-pinned output adapter. The first
  // qualified adapter flags OOD when the maximum normalized class score is
  // below this threshold and always abstains on such a record.
  std::string ood_mode = "max-probability-below-threshold";
  double ood_below = 0.0;
};

struct NumericResult {
  std::vector<float> scores; // canonical class-ordered float32
  uint32_t predicted_label = 0;
  std::string decision; // "benign" | "alert" | "abstain"
  bool out_of_distribution = false;
  bool abstain = false;
  std::string quality; // "valid" | ... (contracts/model/v1 enum)
};

// Reject NaN/Inf in the input tensor (checked before model execution).
void assert_input_finite(const std::vector<uint8_t> &bytes, const std::string &dtype);

// Reject NaN/Inf in a raw float output buffer.
void assert_output_finite(const std::vector<float> &scores);

// Verify the raw output shape and class order matches the profile.
void assert_class_order(const std::vector<float> &scores, const NumericProfile &p);

// Deterministic abstain/decision computation for `masi-window-adapter-v1`:
// normalize by score_domain, reorder into canonical class order, take top-1,
// abstain below `abstain_below`, alert when the predicted label is not the
// baseline label and the top score reaches `alert_threshold`. OOD is computed
// by the explicit max-probability threshold and always forces abstention.
NumericResult apply_output_adapter(const std::vector<float> &raw_scores, const NumericProfile &p);

// Float tolerance check: absolute / relative / ULP.
bool float_within_tolerance(float a, float b, const NumericProfile &p);

} // namespace masi::inf
