#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

#include "envelope.h"
#include "error.h"

namespace masi::inf {

// One expected member of the digest-pinned repository closure.
struct ClosureMember {
  std::string rel_path;      // path relative to repository root, no '..'
  std::string member_digest; // "sha256:" over file bytes
  std::string role;          // "model" | "config" | "version" | "backend"
                             // | "bundle-manifest"
};

// Output adapter / label taxonomy data configuration carried by the closure
// member with `role == "bundle-manifest"`. Data only: the deterministic
// mapping implementation lives in qualified Gateway code and is selected by
// `adapter_id`, never injected by the bundle.
// See contracts/inference/v1/profile.json#output_adapter_binding.
struct BundleManifest {
  std::string schema_version;
  std::string model_id;
  std::string revision;
  std::string model_digest;
  std::string model_revision_digest;
  std::string runtime_profile_id;
  std::string runtime_profile_digest;
  std::string optimization_profile_id;
  std::string optimization_profile_digest;

  // label_taxonomy
  std::vector<uint32_t> label_ids;
  std::string mode;             // "single-label"
  std::string score_domain;     // "logit" | "probability"
  std::string threshold_kind;   // label_taxonomy.threshold.kind
  double abstain_below = 0.0;   // label_taxonomy.threshold.value
  std::string calibration_kind; // "none" | ...

  // output_adapter
  std::string adapter_id;
  std::string adapter_version;
  std::string adapter_digest;
  std::string mapping_kind;
  std::vector<uint32_t> class_order;
  uint32_t axis = 1;
  uint32_t top_k = 1;
  double alert_threshold = 0.0; // output_adapter.threshold
  std::string ood_mode;
  double ood_below = 0.0;
  std::string executable_policy;

  // derived contract digests declared by the bundle
  std::string feature_contract_digest;
  std::string label_contract_digest;
  std::string output_adapter_digest;

  // Triton expectations declared by the bundle (compared with the frozen
  // wire profile and the live Triton ModelConfig at startup).
  int32_t triton_max_batch_size = 0;
  std::vector<int32_t> triton_preferred_batch_size;
  int32_t triton_max_queue_delay_microseconds = 0;
  int32_t triton_max_queue_size = 0;
  std::string triton_instance_group_kind;
  int32_t triton_instance_group_count = 0;
};

// Result of a closure verification pass.
struct ClosureVerification {
  bool ok = false;
  std::string repository_identity;
  std::string observed_closure_digest; // "sha256:" over sorted member digests
  std::vector<std::string> missing;
  std::vector<std::string> extra; // unexpected model/version/config/backend
  std::vector<std::string> digest_mismatches;
};

// Read the closure manifest embedded beside the model repository
// (closure-manifest.json) and return the expected exact member set.
std::vector<ClosureMember> load_closure_manifest(const std::string &repository_root);

// Resolve the absolute path of the ONNX model inside the digest-pinned
// repository: the unique closure member with `role == "model"` joined to the
// repository root. Throws kRepositoryClosureViolation unless exactly one
// model member is declared. Callers must have already verified the closure
// (verify_repository_closure) so the returned path points at a digest-pinned,
// read-only, non-symlink regular file.
std::string resolve_model_path(const std::string &repository_root);

// Resolve the Triton model name for the digest-pinned repository: the first
// path component of the unique `role == "model"` closure member, i.e. the
// model directory name in Triton repository layout
// (`<model_name>/<version>/model.onnx`). Throws kRepositoryClosureViolation
// unless exactly one model member with a `<name>/<version>/...` path is
// declared.
std::string resolve_triton_model_name(const std::string &repository_root);

// Load and self-verify the adapter/taxonomy data configuration from the
// unique `role == "bundle-manifest"` closure member. Rejects:
//   - missing/duplicate bundle-manifest member
//   - unsupported schema_version
//   - recomputed feature/label/adapter digests that do not match the declared
//     contract_digests
//   - mapping_kind != "deterministic-implementation"
//   - executable_policy != "no-executable-code-injected-from-bundle"
//   - score_domain outside {logit, probability}
//   - class_order that is not a permutation of label_ids
//   - top_k != 1 (only top-1 is qualified in the first release)
BundleManifest load_bundle_manifest(const std::string &repository_root);

// Canonical model-revision identity: sha256 over the bundle manifest's
// `binding_identity` object (sorted keys, no whitespace).
std::string compute_model_revision_digest(const BundleManifest &bundle);

// Read the raw bytes of the unique `role == "config"` closure member (the
// Triton `config.pbtxt`). Used to compare the pinned on-disk configuration
// with the configuration Triton actually loaded.
std::string read_closure_config_text(const std::string &repository_root);

// Verify the on-disk repository directory:
//   - directory is read-only (no write bit for owner/group/other on entries)
//   - no symlink anywhere in the tree
//   - every expected member present with exact digest
//   - no extra model/version/config/backend file outside the manifest
// `expected_identity` / `expected_closure_digest` come from the startup
// envelope and must match the manifest exactly.
ClosureVerification verify_repository_closure(const std::string &repository_root,
                                              const std::string &expected_identity,
                                              const std::string &expected_closure_digest);

// Throw a kRepositoryClosureViolation if verification did not pass.
void assert_closure_ok(const ClosureVerification &v);

} // namespace masi::inf
