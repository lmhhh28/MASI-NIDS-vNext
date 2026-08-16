#include "tls.h"

#include <fstream>
#include <sys/stat.h>
#include <unistd.h>

namespace masi::inf {

namespace {

void fail_bad_pem(const std::string& path, const std::string& why) {
  throw error::Exception(error::Code::kInvalidManifest, "pem " + path + ": " + why);
}

void require_regular_no_symlink(const std::string& path) {
  struct stat st;
  if (stat(path.c_str(), &st) != 0) fail_bad_pem(path, "stat failed");
  if (!S_ISREG(st.st_mode)) fail_bad_pem(path, "not regular file");
  if (S_ISLNK(st.st_mode)) fail_bad_pem(path, "is symlink");
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

}  // namespace masi::inf