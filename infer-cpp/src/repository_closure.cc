#include "repository_closure.h"

#include <algorithm>
#include <fstream>
#include <nlohmann/json.hpp>
#include <set>
#include <sstream>

#include "digest.h"

namespace masi::inf {

namespace fs = std::filesystem;

namespace {

bool is_symlink_path(const fs::path& p) {
  std::error_code ec;
  if (fs::is_symlink(p, ec)) return true;
  // Also reject symlinks encountered during iteration.
  fs::path resolved = p;
  ec.clear();
  auto st = fs::symlink_status(p, ec);
  if (!ec && st.type() == fs::file_type::symlink) return true;
  return false;
}

bool is_readonly(const fs::path& p) {
  std::error_code ec;
  auto perms = fs::status(p, ec).permissions();
  if (ec) return false;
  using std::filesystem::perms;
  if ((perms & perms::owner_write) != perms::none) return false;
  if ((perms & perms::group_write) != perms::none) return false;
  if ((perms & perms::others_write) != perms::none) return false;
  return true;
}

std::string read_file_string(const fs::path& p) {
  std::ifstream f(p, std::ios::binary);
  if (!f) return "";
  return std::string((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

}  // namespace

std::vector<ClosureMember> load_closure_manifest(const std::string& repository_root) {
  fs::path root(repository_root);
  fs::path manifest = root / "closure-manifest.json";
  std::error_code ec;
  if (!fs::exists(manifest, ec) || is_symlink_path(manifest) || !is_readonly(manifest))
    throw error::Exception(error::Code::kRepositoryClosureViolation, "closure-manifest.json missing/symlink/writable");
  if (fs::file_size(manifest, ec) > 1048576)
    throw error::Exception(error::Code::kRepositoryClosureViolation, "closure-manifest.json exceeds 1MiB");

  nlohmann::json j;
  try {
    j = nlohmann::json::parse(read_file_string(manifest));
  } catch (const std::exception& e) {
    throw error::Exception(error::Code::kRepositoryClosureViolation, std::string("closure-manifest json: ") + e.what());
  }
  if (!j.is_object())
    throw error::Exception(error::Code::kRepositoryClosureViolation, "closure-manifest not object");

  std::vector<ClosureMember> out;
  if (!j.contains("identity") || !j.contains("closure_digest") || !j.contains("members"))
    throw error::Exception(error::Code::kRepositoryClosureViolation, "closure-manifest missing fields");
  if (!j.at("members").is_array())
    throw error::Exception(error::Code::kRepositoryClosureViolation, "closure-manifest members not array");
  for (const auto& m : j.at("members")) {
    ClosureMember cm;
    cm.rel_path = m.at("rel_path").get<std::string>();
    cm.member_digest = m.at("member_digest").get<std::string>();
    cm.role = m.at("role").get<std::string>();
    if (cm.rel_path.empty() || cm.rel_path[0] == '/' || cm.rel_path.find("..") != std::string::npos)
      throw error::Exception(error::Code::kRepositoryClosureViolation, "closure member path invalid: " + cm.rel_path);
    if (cm.member_digest.rfind("sha256:", 0) != 0)
      throw error::Exception(error::Code::kRepositoryClosureViolation, "closure member digest not sha256: " + cm.rel_path);
    if (cm.role != "model" && cm.role != "config" && cm.role != "version" && cm.role != "backend")
      throw error::Exception(error::Code::kRepositoryClosureViolation, "closure member role unsupported: " + cm.role);
    out.push_back(std::move(cm));
  }
  return out;
}

// Internal: rel_path of the unique `role == "model"` closure member.
// Throws kRepositoryClosureViolation unless exactly one is declared.
std::string model_member_rel_path(const std::string& repository_root) {
  std::string model_rel;
  for (const auto& m : load_closure_manifest(repository_root)) {
    if (m.role != "model") continue;
    if (!model_rel.empty())
      throw error::Exception(error::Code::kRepositoryClosureViolation,
                             "closure declares multiple model members");
    model_rel = m.rel_path;
  }
  if (model_rel.empty())
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure declares no model member");
  return model_rel;
}

std::string resolve_model_path(const std::string& repository_root) {
  return (fs::path(repository_root) / model_member_rel_path(repository_root)).string();
}

std::string resolve_triton_model_name(const std::string& repository_root) {
  // Triton repository layout: <model_name>/<version>/model.onnx. The model
  // name is the first path component of the closure model member's rel_path
  // (already validated: relative, no "..", no leading '/').
  const std::string model_rel = model_member_rel_path(repository_root);
  const size_t slash = model_rel.find('/');
  if (slash == std::string::npos)
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure model member lacks <name>/<version> dirs: " + model_rel);
  const std::string name = model_rel.substr(0, slash);
  if (name.empty() || name == "." || name == "..")
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure model member name invalid: " + name);
  return name;
}

ClosureVerification verify_repository_closure(const std::string& repository_root,
                                              const std::string& expected_identity,
                                              const std::string& expected_closure_digest) {
  ClosureVerification v;
  v.repository_identity = expected_identity;

  fs::path root(repository_root);
  std::error_code ec;
  if (!fs::is_directory(root, ec) || is_symlink_path(root) || !is_readonly(root))
    throw error::Exception(error::Code::kRepositoryClosureViolation, "repository root not readonly dir or is symlink");

  // 1. manifest must declare exact identity + closure digest.
  fs::path manifest = root / "closure-manifest.json";
  nlohmann::json mj;
  try {
    mj = nlohmann::json::parse(read_file_string(manifest));
  } catch (...) {
    throw error::Exception(error::Code::kRepositoryClosureViolation, "closure-manifest unreadable");
  }
  const std::string manifest_identity = mj.at("identity").get<std::string>();
  const std::string manifest_digest = mj.at("closure_digest").get<std::string>();
  if (!digest_match(manifest_identity, expected_identity))
    throw error::Exception(error::Code::kRepositoryClosureViolation, "manifest identity mismatch");
  if (!digest_match(manifest_digest, expected_closure_digest))
    throw error::Exception(error::Code::kRepositoryClosureViolation, "manifest closure_digest mismatch");

  // 2. expected member set.
  auto expected = load_closure_manifest(repository_root);

  // 3. observe actual tree, no symlink, readonly, collect files.
  std::set<std::string> actual_rel;
  for (auto it = fs::recursive_directory_iterator(root, fs::directory_options::none, ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    const auto& entry = *it;
    if (is_symlink_path(entry.path()))
      throw error::Exception(error::Code::kRepositoryClosureViolation, "symlink in repository: " + entry.path().string());
    if (entry.is_regular_file(ec)) {
      if (!is_readonly(entry.path()))
        throw error::Exception(error::Code::kRepositoryClosureViolation, "writable file in repository: " + entry.path().string());
      std::string rel = fs::relative(entry.path(), root, ec).string();
      actual_rel.insert(rel);
    }
  }
  // The manifest file itself is allowed but not part of the closure digest set.
  actual_rel.erase("closure-manifest.json");

  // 4. per-member digest check + presence.
  std::vector<std::string> member_digests;
  for (const auto& m : expected) {
    fs::path p = root / m.rel_path;
    if (!fs::is_regular_file(p, ec)) {
      v.missing.push_back(m.rel_path);
      continue;
    }
    if (is_symlink_path(p) || !is_readonly(p))
      throw error::Exception(error::Code::kRepositoryClosureViolation, "member not readonly/symlink: " + m.rel_path);
    const std::string d = sha256_file(p.string());
    member_digests.push_back(d);
    if (!digest_match(d, m.member_digest))
      v.digest_mismatches.push_back(m.rel_path);
    actual_rel.erase(m.rel_path);
  }
  // 5. anything left in actual_rel is an extra model/version/config/backend.
  for (const auto& e : actual_rel) v.extra.push_back(e);

  // 6. observed closure digest over sorted member digests must match.
  std::sort(member_digests.begin(), member_digests.end());
  std::ostringstream oss;
  for (const auto& d : member_digests) oss << d << '\n';
  v.observed_closure_digest = sha256_hex(oss.str().data(), oss.str().size());

  v.ok = v.missing.empty() && v.extra.empty() && v.digest_mismatches.empty() &&
         digest_match(v.observed_closure_digest, expected_closure_digest);
  return v;
}

void assert_closure_ok(const ClosureVerification& v) {
  if (v.ok) return;
  std::ostringstream oss;
  oss << "closure verification failed";
  for (const auto& m : v.missing) oss << " missing=" << m;
  for (const auto& e : v.extra) oss << " extra=" << e;
  for (const auto& d : v.digest_mismatches) oss << " digest_mismatch=" << d;
  if (!digest_match(v.observed_closure_digest, std::string()))
    oss << " observed_closure_digest=" << v.observed_closure_digest;
  throw error::Exception(error::Code::kRepositoryClosureViolation, oss.str());
}

}  // namespace masi::inf