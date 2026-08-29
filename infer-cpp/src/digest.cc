#include "digest.h"

#include <algorithm>
#include <array>
#include <fstream>
#include <iomanip>
#include <memory>
#include <openssl/evp.h>
#include <sstream>

namespace masi::inf {

namespace {

using EvpContext = std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)>;

std::string render_sha256(const unsigned char *hash, unsigned int length) {
  if (length != 32)
    return "";
  std::ostringstream oss;
  oss << "sha256:";
  for (unsigned int i = 0; i < length; ++i) {
    oss << std::hex << std::setfill('0') << std::setw(2) << static_cast<int>(hash[i]);
  }
  return oss.str();
}

EvpContext new_sha256_context() {
  EvpContext context(EVP_MD_CTX_new(), &EVP_MD_CTX_free);
  if (!context || EVP_DigestInit_ex(context.get(), EVP_sha256(), nullptr) != 1)
    return EvpContext(nullptr, &EVP_MD_CTX_free);
  return context;
}

} // namespace

std::string sha256_hex(const void *data, size_t len) {
  auto context = new_sha256_context();
  if (!context || EVP_DigestUpdate(context.get(), data, len) != 1)
    return "";
  std::array<unsigned char, EVP_MAX_MD_SIZE> hash{};
  unsigned int length = 0;
  if (EVP_DigestFinal_ex(context.get(), hash.data(), &length) != 1)
    return "";
  return render_sha256(hash.data(), length);
}

std::string sha256_file(const std::string &path) {
  std::ifstream f(path, std::ios::binary);
  if (!f)
    return "";
  auto context = new_sha256_context();
  if (!context)
    return "";
  std::array<char, 65536> buffer{};
  while (f) {
    f.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
    const auto count = f.gcount();
    if (count > 0 &&
        EVP_DigestUpdate(context.get(), buffer.data(), static_cast<size_t>(count)) != 1)
      return "";
  }
  if (!f.eof())
    return "";
  std::array<unsigned char, EVP_MAX_MD_SIZE> hash{};
  unsigned int length = 0;
  if (EVP_DigestFinal_ex(context.get(), hash.data(), &length) != 1)
    return "";
  return render_sha256(hash.data(), length);
}

bool is_sha256_digest(std::string_view value) {
  if (value.size() != 71 || value.substr(0, 7) != "sha256:")
    return false;
  const auto body = value.substr(7);
  return std::any_of(body.begin(), body.end(), [](char c) { return c != '0'; }) &&
         std::all_of(body.begin(), body.end(),
                     [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); });
}

bool digest_match(const std::string &a, const std::string &b) {
  if (a.size() != b.size())
    return false;
  // constant-time compare
  volatile unsigned char d = 0;
  for (size_t i = 0; i < a.size(); ++i)
    d |= static_cast<unsigned char>(a[i] ^ b[i]);
  return d == 0;
}

} // namespace masi::inf
