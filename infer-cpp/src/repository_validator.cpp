#include <iostream>
#include <string>

#include <nlohmann/json.hpp>

#include "digest.h"
#include "error.h"
#include "repository_closure.h"

int main(int argc, char **argv) {
  if (argc != 4) {
    std::cerr << "usage: masi_repository_validator REPOSITORY IDENTITY CLOSURE_DIGEST\n";
    return 64;
  }
  const std::string repository = argv[1];
  const std::string identity = argv[2];
  const std::string closure_digest = argv[3];
  try {
    const auto closure = masi::inf::verify_repository_closure(repository, identity, closure_digest);
    masi::inf::assert_closure_ok(closure);
    const auto bundle = masi::inf::load_bundle_manifest(repository);
    const std::string computed_revision = masi::inf::compute_model_revision_digest(bundle);
    if (!masi::inf::digest_match(computed_revision, bundle.model_revision_digest)) {
      throw masi::inf::error::Exception(masi::inf::error::Code::kReadbackMismatch,
                                        "compiled profile model revision mismatch");
    }
    const std::string model_path = masi::inf::resolve_model_path(repository);
    const std::string observed_model_digest = masi::inf::sha256_file(model_path);
    if (!masi::inf::digest_match(observed_model_digest, bundle.model_digest)) {
      throw masi::inf::error::Exception(masi::inf::error::Code::kReadbackMismatch,
                                        "model member and bundle digest mismatch");
    }
    const std::string config = masi::inf::read_closure_config_text(repository);
    if (config.empty()) {
      throw masi::inf::error::Exception(masi::inf::error::Code::kRepositoryClosureViolation,
                                        "empty Triton config");
    }
    nlohmann::json result = {
        {"schema_version", "central-repository-consumer-validation/v1"},
        {"result", "PASS"},
        {"repository_identity", closure.repository_identity},
        {"repository_closure_digest", closure.observed_closure_digest},
        {"model_id", bundle.model_id},
        {"revision", bundle.revision},
        {"model_digest", observed_model_digest},
        {"model_revision_digest", computed_revision},
        {"runtime_profile_id", bundle.runtime_profile_id},
        {"runtime_profile_digest", bundle.runtime_profile_digest},
        {"optimization_profile_id", bundle.optimization_profile_id},
        {"optimization_profile_digest", bundle.optimization_profile_digest},
        {"feature_contract_digest", bundle.feature_contract_digest},
        {"label_contract_digest", bundle.label_contract_digest},
        {"output_adapter_digest", bundle.output_adapter_digest},
        {"triton_model_name", masi::inf::resolve_triton_model_name(repository)},
        {"config_bytes", config.size()},
    };
    std::cout << result.dump() << '\n';
    return 0;
  } catch (const masi::inf::error::Exception &error) {
    nlohmann::json result = {
        {"schema_version", "central-repository-consumer-validation/v1"},
        {"result", "FAIL"},
        {"error_code", masi::inf::error::to_string(error.code())},
        {"detail", error.what()},
    };
    std::cerr << result.dump() << '\n';
    return 2;
  } catch (const std::exception &error) {
    std::cerr << nlohmann::json({{"schema_version", "central-repository-consumer-validation/v1"},
                                 {"result", "FAIL"},
                                 {"error_code", "unexpected"},
                                 {"detail", error.what()}})
                     .dump()
              << '\n';
    return 2;
  }
}
