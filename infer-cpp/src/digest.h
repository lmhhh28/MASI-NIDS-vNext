#pragma once

#include <cstddef>
#include <string>
#include <string_view>

namespace masi::inf {

std::string sha256_hex(const void *data, size_t len);
std::string sha256_file(const std::string &path);
bool is_sha256_digest(std::string_view value);
bool digest_match(const std::string &a, const std::string &b);

} // namespace masi::inf
