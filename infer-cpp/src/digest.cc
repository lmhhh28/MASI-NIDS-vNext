#include "digest.h"

#include <openssl/sha.h>
#include <fstream>
#include <sstream>
#include <iomanip>

namespace masi::inf {

std::string sha256_hex(const void* data, size_t len) {
  SHA256_CTX ctx;
  SHA256_Init(&ctx);
  SHA256_Update(&ctx, data, len);
  unsigned char hash[SHA256_DIGEST_LENGTH];
  SHA256_Final(hash, &ctx);
  std::ostringstream oss;
  oss << "sha256:";
  for (int i = 0; i < SHA256_DIGEST_LENGTH; ++i) {
    oss << std::hex << std::setfill('0') << std::setw(2) << static_cast<int>(hash[i]);
  }
  return oss.str();
}

std::string sha256_file(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) return "";
  std::string content((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
  return sha256_hex(content.data(), content.size());
}

bool digest_match(const std::string& a, const std::string& b) {
  if (a.size() != b.size()) return false;
  // constant-time compare
  volatile unsigned char d = 0;
  for (size_t i = 0; i < a.size(); ++i) d |= static_cast<unsigned char>(a[i] ^ b[i]);
  return d == 0;
}

}  // namespace masi::inf