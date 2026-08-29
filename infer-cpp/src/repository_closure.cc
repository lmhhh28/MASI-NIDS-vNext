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

bool is_symlink_path(const fs::path &p) {
  std::error_code ec;
  if (fs::is_symlink(p, ec))
    return true;
  // Also reject symlinks encountered during iteration.
  ec.clear();
  auto st = fs::symlink_status(p, ec);
  if (!ec && st.type() == fs::file_type::symlink)
    return true;
  return false;
}

bool valid_revision(const std::string &value) {
  return value.size() == 40 &&
         std::all_of(value.begin(), value.end(),
                     [](unsigned char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); }) &&
         std::any_of(value.begin(), value.end(), [](char c) { return c != '0'; });
}

bool is_readonly(const fs::path &p) {
  std::error_code ec;
  auto perms = fs::status(p, ec).permissions();
  if (ec)
    return false;
  using std::filesystem::perms;
  if ((perms & perms::owner_write) != perms::none)
    return false;
  if ((perms & perms::group_write) != perms::none)
    return false;
  if ((perms & perms::others_write) != perms::none)
    return false;
  return true;
}

std::string read_file_string(const fs::path &p) {
  std::ifstream f(p, std::ios::binary);
  if (!f)
    return "";
  return std::string((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

} // namespace

std::vector<ClosureMember> load_closure_manifest(const std::string &repository_root) {
  fs::path root(repository_root);
  fs::path manifest = root / "closure-manifest.json";
  std::error_code ec;
  if (!fs::exists(manifest, ec) || is_symlink_path(manifest) || !is_readonly(manifest))
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure-manifest.json missing/symlink/writable");
  if (fs::file_size(manifest, ec) > 1048576)
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure-manifest.json exceeds 1MiB");

  nlohmann::json j;
  try {
    j = nlohmann::json::parse(read_file_string(manifest));
  } catch (const std::exception &e) {
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           std::string("closure-manifest json: ") + e.what());
  }
  if (!j.is_object())
    throw error::Exception(error::Code::kRepositoryClosureViolation, "closure-manifest not object");

  std::vector<ClosureMember> out;
  if (!j.contains("identity") || !j.contains("closure_digest") || !j.contains("members"))
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure-manifest missing fields");
  if (!j.at("members").is_array())
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure-manifest members not array");
  for (const auto &m : j.at("members")) {
    ClosureMember cm;
    cm.rel_path = m.at("rel_path").get<std::string>();
    cm.member_digest = m.at("member_digest").get<std::string>();
    cm.role = m.at("role").get<std::string>();
    if (cm.rel_path.empty() || cm.rel_path[0] == '/' || cm.rel_path.find("..") != std::string::npos)
      throw error::Exception(error::Code::kRepositoryClosureViolation,
                             "closure member path invalid: " + cm.rel_path);
    if (!is_sha256_digest(cm.member_digest))
      throw error::Exception(error::Code::kRepositoryClosureViolation,
                             "closure member digest not sha256: " + cm.rel_path);
    if (cm.role != "model" && cm.role != "config" && cm.role != "version" && cm.role != "backend" &&
        cm.role != "bundle-manifest")
      throw error::Exception(error::Code::kRepositoryClosureViolation,
                             "closure member role unsupported: " + cm.role);
    out.push_back(std::move(cm));
  }
  return out;
}

// Internal: rel_path of the unique member with the requested role.
// Throws kRepositoryClosureViolation unless exactly one is declared.
static std::string unique_member_rel_path(const std::string &repository_root,
                                          const std::string &role) {
  std::string found;
  for (const auto &m : load_closure_manifest(repository_root)) {
    if (m.role != role)
      continue;
    if (!found.empty())
      throw error::Exception(error::Code::kRepositoryClosureViolation,
                             "closure declares multiple " + role + " members");
    found = m.rel_path;
  }
  if (found.empty())
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure declares no " + role + " member");
  return found;
}

// Internal: rel_path of the unique `role == "model"` closure member.
// Throws kRepositoryClosureViolation unless exactly one is declared.
std::string model_member_rel_path(const std::string &repository_root) {
  return unique_member_rel_path(repository_root, "model");
}

std::string resolve_model_path(const std::string &repository_root) {
  return (fs::path(repository_root) / model_member_rel_path(repository_root)).string();
}

std::string read_closure_config_text(const std::string &repository_root) {
  const fs::path p = fs::path(repository_root) / unique_member_rel_path(repository_root, "config");
  std::error_code ec;
  if (!fs::is_regular_file(p, ec) || is_symlink_path(p))
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure config member missing or symlink");
  if (fs::file_size(p, ec) > 1048576)
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure config member exceeds 1MiB");
  return read_file_string(p);
}

namespace {

std::vector<uint32_t> u32_array(const nlohmann::json &j, const std::string &what) {
  if (!j.is_array() || j.empty())
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest " + what + " not a non-empty array");
  std::vector<uint32_t> out;
  for (const auto &v : j) {
    if (!v.is_number_unsigned())
      throw error::Exception(error::Code::kIncompatibleContract,
                             "bundle-manifest " + what + " element not an unsigned integer");
    out.push_back(v.get<uint32_t>());
  }
  return out;
}

// Canonical digest of a JSON sub-object: sorted keys, no whitespace.
// nlohmann::json keeps object keys sorted, so dump() is already canonical and
// byte-identical to the Python generator's canonical_json().
std::string canonical_digest(const nlohmann::json &j) {
  const std::string s = j.dump();
  return sha256_hex(s.data(), s.size());
}

} // namespace

BundleManifest load_bundle_manifest(const std::string &repository_root) {
  const fs::path p =
      fs::path(repository_root) / unique_member_rel_path(repository_root, "bundle-manifest");
  std::error_code ec;
  if (!fs::is_regular_file(p, ec) || is_symlink_path(p))
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "bundle-manifest member missing or symlink");
  if (fs::file_size(p, ec) > 1048576)
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "bundle-manifest exceeds 1MiB");

  nlohmann::json j;
  try {
    j = nlohmann::json::parse(read_file_string(p));
  } catch (const std::exception &e) {
    throw error::Exception(error::Code::kInvalidManifest,
                           std::string("bundle-manifest json: ") + e.what());
  }
  if (!j.is_object())
    throw error::Exception(error::Code::kInvalidManifest, "bundle-manifest not object");

  BundleManifest b;
  b.schema_version = j.value("schema_version", "");
  if (b.schema_version != "masi-model-bundle-manifest/v1")
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest schema_version unsupported: " + b.schema_version);
  for (const char *required :
       {"model_id", "revision", "model_digest", "model_revision_digest", "binding_identity",
        "feature_schema", "label_taxonomy", "output_adapter", "contract_digests", "triton"}) {
    if (!j.contains(required))
      throw error::Exception(error::Code::kInvalidManifest,
                             std::string("bundle-manifest missing field: ") + required);
  }
  b.model_id = j.at("model_id").get<std::string>();
  b.revision = j.at("revision").get<std::string>();
  if (!valid_revision(b.revision))
    throw error::Exception(error::Code::kInvalidManifest,
                           "bundle-manifest revision must be non-zero 40-hex");
  b.model_digest = j.at("model_digest").get<std::string>();
  b.model_revision_digest = j.at("model_revision_digest").get<std::string>();

  const auto &bi = j.at("binding_identity");
  if (!bi.is_object())
    throw error::Exception(error::Code::kInvalidManifest,
                           "bundle-manifest binding_identity not object");
  b.runtime_profile_id = bi.value("runtime_profile_id", "");
  b.runtime_profile_digest = bi.value("runtime_profile_digest", "");
  b.optimization_profile_id = bi.value("optimization_profile_id", "");
  b.optimization_profile_digest = bi.value("optimization_profile_digest", "");

  const auto &tax = j.at("label_taxonomy");
  const auto &ad = j.at("output_adapter");
  const auto &cd = j.at("contract_digests");
  const auto &tr = j.at("triton");
  if (!tax.is_object() || !ad.is_object() || !cd.is_object() || !tr.is_object())
    throw error::Exception(
        error::Code::kInvalidManifest,
        "bundle-manifest label_taxonomy/output_adapter/contract_digests/triton not object");

  b.label_ids = u32_array(tax.at("label_ids"), "label_ids");
  b.mode = tax.value("mode", "");
  b.score_domain = tax.value("score_domain", "");
  b.threshold_kind = tax.at("threshold").value("kind", "");
  b.abstain_below = tax.at("threshold").at("value").get<double>();
  b.calibration_kind = tax.at("calibration").value("kind", "");

  b.adapter_id = ad.value("adapter_id", "");
  b.adapter_version = ad.value("version", "");
  b.adapter_digest = ad.value("adapter_digest", "");
  b.mapping_kind = ad.value("mapping_kind", "");
  b.class_order = u32_array(ad.at("class_order"), "output_adapter.class_order");
  b.axis = ad.at("axis").get<uint32_t>();
  b.top_k = ad.at("top_k").get<uint32_t>();
  b.alert_threshold = ad.at("threshold").get<double>();
  if (!ad.contains("ood_policy") || !ad.at("ood_policy").is_object())
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest output_adapter.ood_policy missing");
  b.ood_mode = ad.at("ood_policy").value("mode", "");
  b.ood_below = ad.at("ood_policy").value("threshold", -1.0);
  b.executable_policy = ad.value("executable_policy", "");

  b.feature_contract_digest = cd.value("feature_contract_digest", "");
  b.label_contract_digest = cd.value("label_contract_digest", "");
  b.output_adapter_digest = cd.value("output_adapter_digest", "");

  b.triton_max_batch_size = tr.value("max_batch_size", 0);
  for (const auto &v : tr.value("preferred_batch_size", nlohmann::json::array()))
    b.triton_preferred_batch_size.push_back(v.get<int32_t>());
  b.triton_max_queue_delay_microseconds = tr.value("max_queue_delay_microseconds", 0);
  b.triton_max_queue_size = tr.value("max_queue_size", 0);
  if (tr.contains("instance_group")) {
    b.triton_instance_group_kind = tr.at("instance_group").value("kind", "");
    b.triton_instance_group_count = tr.at("instance_group").value("count", 0);
  }

  // Executable-content and mapping policy: data configuration only.
  if (b.mapping_kind != "deterministic-implementation")
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest mapping_kind unsupported: " + b.mapping_kind);
  if (b.executable_policy != "no-executable-code-injected-from-bundle")
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest executable_policy rejected: " + b.executable_policy);
  if (b.score_domain != "logit" && b.score_domain != "probability")
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest score_domain unsupported: " + b.score_domain);
  if (b.mode != "single-label")
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest label mode unsupported: " + b.mode);
  if (b.threshold_kind != "fixed")
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest threshold kind unsupported: " + b.threshold_kind);
  if (b.calibration_kind != "none")
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest calibration kind not qualified: " + b.calibration_kind);
  if (b.top_k != 1)
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest top_k != 1 is not qualified");
  if (b.axis != 1)
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest axis != 1 is not qualified");
  if (b.ood_mode != "max-probability-below-threshold" || b.ood_below < 0.0 || b.ood_below > 1.0 ||
      ad.at("ood_policy").value("decision", "") != "abstain")
    throw error::Exception(error::Code::kIncompatibleContract,
                           "bundle-manifest OOD policy not qualified");

  // class_order must be a permutation of label_ids.
  {
    std::vector<uint32_t> a = b.class_order, c = b.label_ids;
    std::sort(a.begin(), a.end());
    std::sort(c.begin(), c.end());
    if (a != c)
      throw error::Exception(error::Code::kIncompatibleContract,
                             "bundle-manifest class_order is not a permutation of label_ids");
  }

  // Self-verify the declared contract digests against recomputed canonical
  // digests, so a tampered data block cannot keep a stale digest.
  const std::string feature_recomputed = canonical_digest(j.at("feature_schema"));
  const std::string label_recomputed = canonical_digest(tax);
  nlohmann::json adapter_body = ad;
  adapter_body.erase("adapter_digest");
  const std::string adapter_recomputed = canonical_digest(adapter_body);

  if (!digest_match(feature_recomputed, b.feature_contract_digest))
    throw error::Exception(error::Code::kIncompatibleContract,
                           "feature_contract_digest mismatch: observed " + feature_recomputed);
  if (!digest_match(label_recomputed, b.label_contract_digest))
    throw error::Exception(error::Code::kIncompatibleContract,
                           "label_contract_digest mismatch: observed " + label_recomputed);
  if (!digest_match(adapter_recomputed, b.adapter_digest))
    throw error::Exception(error::Code::kIncompatibleContract,
                           "adapter_digest mismatch: observed " + adapter_recomputed);
  if (!digest_match(b.output_adapter_digest, b.adapter_digest))
    throw error::Exception(error::Code::kIncompatibleContract,
                           "output_adapter_digest must equal adapter_digest");

  for (const auto *value : {&b.model_digest, &b.model_revision_digest, &b.runtime_profile_digest,
                            &b.optimization_profile_digest}) {
    if (!is_sha256_digest(*value))
      throw error::Exception(error::Code::kInvalidManifest,
                             "bundle-manifest binding digest format invalid");
  }
  if (!digest_match(canonical_digest(bi), b.model_revision_digest))
    throw error::Exception(error::Code::kReadbackMismatch,
                           "model_revision_digest preimage mismatch");

  return b;
}

std::string compute_model_revision_digest(const BundleManifest &bundle) {
  nlohmann::json identity = {
      {"feature_contract_digest", bundle.feature_contract_digest},
      {"inference_wire_profile_digest", MASI_INF_WIRE_PROFILE_DIGEST},
      {"label_contract_digest", bundle.label_contract_digest},
      {"model_bundle_digest", bundle.model_digest},
      {"model_digest", bundle.model_digest},
      {"model_id", bundle.model_id},
      {"optimization_profile_digest", bundle.optimization_profile_digest},
      {"optimization_profile_id", bundle.optimization_profile_id},
      {"output_adapter_digest", bundle.output_adapter_digest},
      {"revision", bundle.revision},
      {"runtime_profile_digest", bundle.runtime_profile_digest},
      {"runtime_profile_id", bundle.runtime_profile_id},
  };
  return canonical_digest(identity);
}

std::string resolve_triton_model_name(const std::string &repository_root) {
  // Triton repository layout: <model_name>/<version>/model.onnx. The model
  // name is the first path component of the closure model member's rel_path
  // (already validated: relative, no "..", no leading '/').
  const std::string model_rel = model_member_rel_path(repository_root);
  const size_t slash = model_rel.find('/');
  if (slash == std::string::npos)
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure model member lacks <name>/<version> dirs: " + model_rel);
  std::string name = model_rel.substr(0, slash);
  if (name.empty() || name == "." || name == "..")
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "closure model member name invalid: " + name);
  return name;
}

ClosureVerification verify_repository_closure(const std::string &repository_root,
                                              const std::string &expected_identity,
                                              const std::string &expected_closure_digest) {
  ClosureVerification v;
  v.repository_identity = expected_identity;

  fs::path root(repository_root);
  std::error_code ec;
  if (!fs::is_directory(root, ec) || is_symlink_path(root) || !is_readonly(root))
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "repository root not readonly dir or is symlink");

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
    throw error::Exception(error::Code::kRepositoryClosureViolation,
                           "manifest closure_digest mismatch");

  // 2. expected member set.
  auto expected = load_closure_manifest(repository_root);

  // 3. observe actual tree, no symlink, readonly, collect files.
  std::set<std::string> actual_rel;
  for (auto it = fs::recursive_directory_iterator(root, fs::directory_options::none, ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    const auto &entry = *it;
    if (is_symlink_path(entry.path()))
      throw error::Exception(error::Code::kRepositoryClosureViolation,
                             "symlink in repository: " + entry.path().string());
    if (entry.is_regular_file(ec)) {
      if (!is_readonly(entry.path()))
        throw error::Exception(error::Code::kRepositoryClosureViolation,
                               "writable file in repository: " + entry.path().string());
      std::string rel = fs::relative(entry.path(), root, ec).string();
      actual_rel.insert(rel);
    }
  }
  // The manifest file itself is allowed but not part of the closure digest set.
  actual_rel.erase("closure-manifest.json");

  // 4. per-member digest check + presence.
  std::vector<std::string> preimage_lines;
  for (const auto &m : expected) {
    fs::path p = root / m.rel_path;
    if (!fs::is_regular_file(p, ec)) {
      v.missing.push_back(m.rel_path);
      continue;
    }
    if (is_symlink_path(p) || !is_readonly(p))
      throw error::Exception(error::Code::kRepositoryClosureViolation,
                             "member not readonly/symlink: " + m.rel_path);
    const std::string d = sha256_file(p.string());
    // Preimage binds role and path, not only content, so relocating or
    // re-roling a member changes the closure digest.
    preimage_lines.push_back(m.role + "\t" + m.rel_path + "\t" + d);
    if (!digest_match(d, m.member_digest))
      v.digest_mismatches.push_back(m.rel_path);
    actual_rel.erase(m.rel_path);
  }
  // 5. anything left in actual_rel is an extra model/version/config/backend.
  for (const auto &e : actual_rel)
    v.extra.push_back(e);

  // 6. observed closure digest over the sorted role/path/digest preimage.
  std::sort(preimage_lines.begin(), preimage_lines.end());
  std::ostringstream oss;
  for (const auto &line : preimage_lines)
    oss << line << '\n';
  v.observed_closure_digest = sha256_hex(oss.str().data(), oss.str().size());

  v.ok = v.missing.empty() && v.extra.empty() && v.digest_mismatches.empty() &&
         digest_match(v.observed_closure_digest, expected_closure_digest);
  return v;
}

void assert_closure_ok(const ClosureVerification &v) {
  if (v.ok)
    return;
  std::ostringstream oss;
  oss << "closure verification failed";
  for (const auto &m : v.missing)
    oss << " missing=" << m;
  for (const auto &e : v.extra)
    oss << " extra=" << e;
  for (const auto &d : v.digest_mismatches)
    oss << " digest_mismatch=" << d;
  if (!digest_match(v.observed_closure_digest, std::string()))
    oss << " observed_closure_digest=" << v.observed_closure_digest;
  throw error::Exception(error::Code::kRepositoryClosureViolation, oss.str());
}

} // namespace masi::inf
