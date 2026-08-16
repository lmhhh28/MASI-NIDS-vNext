#include "error.h"

namespace masi::inf::error {

const char* to_string(Code c) {
  switch (c) {
    case Code::kInvalidManifest: return "invalid_manifest";
    case Code::kIncompatibleContract: return "incompatible_contract";
    case Code::kUnqualified: return "unqualified";
    case Code::kResourceExhausted: return "resource_exhausted";
    case Code::kStartupFailed: return "startup_failed";
    case Code::kReadbackMismatch: return "readback_mismatch";
    case Code::kCasConflict: return "cas_conflict";
    case Code::kBufferOverflow: return "buffer_overflow";
    case Code::kPoolUnavailable: return "pool_unavailable";
    case Code::kFenced: return "fenced";
    case Code::kRouteFenceMismatch: return "route_fence_mismatch";
    case Code::kResultIdentityMismatch: return "result_identity_mismatch";
    case Code::kResultDigestConflict: return "result_digest_conflict";
    case Code::kDeadlineExceeded: return "deadline_exceeded";
    case Code::kInferenceMessageTooLarge: return "inference_message_too_large";
    case Code::kRuntimeProfileUnsupported: return "runtime_profile_unsupported";
    case Code::kRepositoryClosureViolation: return "repository_closure_violation";
    case Code::kInstanceGroupImplicit: return "instance_group_implicit";
    case Code::kProviderPartitionDrift: return "provider_partition_drift";
    case Code::kAborted: return "aborted";
    default: return "unknown";
  }
}

bool retryable(Code c) {
  switch (c) {
    case Code::kResourceExhausted:
    case Code::kPoolUnavailable:
    case Code::kDeadlineExceeded:
    case Code::kBufferOverflow:
      return true;
    default:
      return false;
  }
}

}  // namespace masi::inf::error