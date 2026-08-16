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
  std::string rel_path;       // path relative to repository root, no '..'
  std::string member_digest;  // "sha256:" over file bytes
  std::string role;           // "model" | "config" | "version" | "backend"
};

// Result of a closure verification pass.
struct ClosureVerification {
  bool ok = false;
  std::string repository_identity;
  std::string observed_closure_digest;  // "sha256:" over sorted member digests
  std::vector<std::string> missing;
  std::vector<std::string> extra;       // unexpected model/version/config/backend
  std::vector<std::string> digest_mismatches;
};

// Read the closure manifest embedded beside the model repository
// (closure-manifest.json) and return the expected exact member set.
std::vector<ClosureMember> load_closure_manifest(const std::string& repository_root);

// Verify the on-disk repository directory:
//   - directory is read-only (no write bit for owner/group/other on entries)
//   - no symlink anywhere in the tree
//   - every expected member present with exact digest
//   - no extra model/version/config/backend file outside the manifest
// `expected_identity` / `expected_closure_digest` come from the startup
// envelope and must match the manifest exactly.
ClosureVerification verify_repository_closure(const std::string& repository_root,
                                               const std::string& expected_identity,
                                               const std::string& expected_closure_digest);

// Throw a kRepositoryClosureViolation if verification did not pass.
void assert_closure_ok(const ClosureVerification& v);

}  // namespace masi::inf