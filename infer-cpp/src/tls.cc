#include "tls.h"

#include <grpc/grpc_security_constants.h>
#include <grpcpp/security/auth_context.h>

#include <fstream>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

namespace masi::inf {

namespace {

void fail_bad_pem(const std::string& path, const std::string& why) {
  throw error::Exception(error::Code::kInvalidManifest, "pem " + path + ": " + why);
}

void require_regular_no_symlink(const std::string& path) {
  struct stat st;
  // lstat, not stat: stat() follows the link, so S_ISLNK could never be true.
  if (lstat(path.c_str(), &st) != 0) fail_bad_pem(path, "lstat failed");
  if (S_ISLNK(st.st_mode)) fail_bad_pem(path, "is symlink");
  if (!S_ISREG(st.st_mode)) fail_bad_pem(path, "not regular file");
}

}  // namespace

std::string read_pem_file(const std::string& path, bool is_private_key) {
  require_regular_no_symlink(path);
  struct stat st;
  if (stat(path.c_str(), &st) != 0) fail_bad_pem(path, "stat failed");
  if (st.st_size > 1048576) fail_bad_pem(path, "exceeds 1MiB");
  if (is_private_key) {
    if ((st.st_mode & 077) != 0)
      fail_bad_pem(path, "private key group/other readable");
  }
  std::ifstream f(path, std::ios::binary);
  if (!f) fail_bad_pem(path, "open failed");
  std::string content((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
  if (content.empty()) fail_bad_pem(path, "empty");
  if (content.find("BEGIN ") == std::string::npos)
    fail_bad_pem(path, "not a PEM");
  return content;
}

grpc::SslServerCredentialsOptions build_server_mtls_options(const std::string& ca_path,
                                                             const std::string& cert_path,
                                                             const std::string& key_path) {
  if (ca_path.empty() || cert_path.empty() || key_path.empty())
    throw error::Exception(error::Code::kInvalidManifest, "mtls paths empty");
  grpc::SslServerCredentialsOptions opts;
  opts.force_client_auth = true;
  opts.pem_root_certs = read_pem_file(ca_path, false);
  opts.pem_key_cert_pairs.push_back(
      grpc::SslServerCredentialsOptions::PemKeyCertPair{
          read_pem_file(key_path, true),
          read_pem_file(cert_path, false)});
  return opts;
}

grpc::SslCredentialsOptions build_client_mtls_options(const std::string& ca_path,
                                                       const std::string& cert_path,
                                                       const std::string& key_path) {
  if (ca_path.empty() || cert_path.empty() || key_path.empty())
    throw error::Exception(error::Code::kInvalidManifest, "mtls paths empty");
  grpc::SslCredentialsOptions opts;
  opts.pem_root_certs = read_pem_file(ca_path, false);
  opts.pem_cert_chain = read_pem_file(cert_path, false);
  opts.pem_private_key = read_pem_file(key_path, true);
  return opts;
}

std::shared_ptr<grpc::ServerCredentials> make_server_credentials(const std::string& ca_path,
                                                                 const std::string& cert_path,
                                                                 const std::string& key_path) {
  auto opts = build_server_mtls_options(ca_path, cert_path, key_path);
  return grpc::SslServerCredentials(opts);
}

std::shared_ptr<grpc::ChannelCredentials> make_client_credentials(const std::string& ca_path,
                                                                   const std::string& cert_path,
                                                                   const std::string& key_path) {
  auto opts = build_client_mtls_options(ca_path, cert_path, key_path);
  return grpc::SslCredentials(opts);
}

PeerIdentityCheck check_peer_identity(const grpc::ServerContext& ctx,
                                      const std::vector<std::string>& san_allowlist) {
  PeerIdentityCheck r;
  if (san_allowlist.empty()) {
    r.reason = "CLIENT_SAN_ALLOWLIST_EMPTY";
    return r;
  }
  auto auth = ctx.auth_context();
  if (!auth) {
    r.reason = "PEER_AUTH_CONTEXT_MISSING";
    return r;
  }
  if (!auth->IsPeerAuthenticated()) {
    r.reason = "PEER_NOT_AUTHENTICATED";
    return r;
  }
  const auto sans = auth->FindPropertyValues(GRPC_X509_SAN_PROPERTY_NAME);
  if (sans.empty()) {
    // A CA-signed certificate without any SAN is not an identity.
    r.reason = "PEER_CERTIFICATE_HAS_NO_SAN";
    return r;
  }
  for (const auto& san : sans) {
    const std::string value(san.data(), san.size());
    for (const auto& allowed : san_allowlist) {
      if (value == allowed) {
        r.allowed = true;
        r.matched_identity = value;
        return r;
      }
    }
  }
  r.reason = "PEER_SAN_NOT_ALLOWLISTED";
  return r;
}

}  // namespace masi::inf