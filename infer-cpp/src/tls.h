#pragma once

#include <grpcpp/grpcpp.h>

#include <string>

#include "error.h"

namespace masi::inf {

// Read a PEM file. Rejects symlinks, missing files, and (for private keys)
// group/other-readable permissions.
std::string read_pem_file(const std::string& path, bool is_private_key);

// Build mTLS server credentials options:
//   - force_client_auth = true
//   - pem_root_certs    = CA bundle used to verify clients
//   - pem_key_cert_pairs = server key+cert
grpc::SslServerCredentialsOptions build_server_mtls_options(const std::string& ca_path,
                                                             const std::string& cert_path,
                                                             const std::string& key_path);

// Build mTLS client credentials options (all three PEM fields populated).
grpc::SslCredentialsOptions build_client_mtls_options(const std::string& ca_path,
                                                       const std::string& cert_path,
                                                       const std::string& key_path);

// Build the actual server credentials object. Throws if any PEM is empty.
std::shared_ptr<grpc::ServerCredentials> make_server_credentials(const std::string& ca_path,
                                                                 const std::string& cert_path,
                                                                 const std::string& key_path);

// Build the actual client credentials object for the probe binary / Gateway
// outbound channels. Throws if any PEM is empty.
std::shared_ptr<grpc::ChannelCredentials> make_client_credentials(const std::string& ca_path,
                                                                   const std::string& cert_path,
                                                                   const std::string& key_path);

}  // namespace masi::inf