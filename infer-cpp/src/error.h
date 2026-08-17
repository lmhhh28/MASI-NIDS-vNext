#pragma once

#include <stdexcept>
#include <string>
#include <string_view>

namespace masi::inf::error {

enum class Code {
  kInvalidManifest,
  kIncompatibleContract,
  kUnqualified,
  kResourceExhausted,
  kStartupFailed,
  kReadbackMismatch,
  kCasConflict,
  kBufferOverflow,
  kPoolUnavailable,
  kFenced,
  kRouteFenceMismatch,
  kResultIdentityMismatch,
  kResultDigestConflict,
  kDeadlineExceeded,
  kInferenceMessageTooLarge,
  kRuntimeProfileUnsupported,
  kRepositoryClosureViolation,
  kInstanceGroupImplicit,
  kProviderPartitionDrift,
  kAborted,
};

const char *to_string(Code c);
bool retryable(Code c);

class Exception : public std::runtime_error {
public:
  explicit Exception(Code c, std::string_view msg)
      : std::runtime_error(std::string(to_string(c)) + ": " + std::string(msg)), code_(c) {}
  Code code() const { return code_; }

private:
  Code code_;
};

} // namespace masi::inf::error
