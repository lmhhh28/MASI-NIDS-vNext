#!/usr/bin/env bash
# Build the registered gRPC/Protobuf toolchain the Gateway links against.
#
# This recipe is part of the module's supply-chain identity, not a convenience
# wrapper. It MUST stay byte-for-byte equivalent to the gRPC build in
# infer-cpp/Dockerfile, because the supply-chain gate compares the Gateway
# binary produced by the host offline rebuild with the one inside the OCI image
# and requires them to be identical (ISSUE-INF-001).
#
# Two properties are load-bearing and must not be changed on one side only:
#
#   1. SOURCE_DIR. gRPC bakes __FILE__ into hundreds of assertion/log strings,
#      so the absolute path of the source tree ends up inside the Gateway
#      binary. The host build and the image build must clone to the same path.
#   2. INSTALL_PREFIX. The prefix also appears in the binary, so both sides must
#      install to the same location.
#
# The script mutates /opt and is therefore explicit and idempotent: it refuses
# to overwrite an existing prefix unless --force is passed.
set -euo pipefail

grpc_version="1.82.1"
grpc_commit="acccf84c0df20487d64101f528e5d426541ca4e5"
source_dir="${MASI_INF_GRPC_SOURCE_DIR:-/opt/masi-toolchain/grpc-src}"
build_dir="${MASI_INF_GRPC_BUILD_DIR:-/tmp/grpc-build}"
install_prefix="${MASI_INF_GRPC_PREFIX:-/opt/masi-toolchain/grpc-${grpc_version}}"
force=false
for argument in "$@"; do
  case "${argument}" in
    --force) force=true ;;
    *) echo "unknown argument: ${argument}" >&2; exit 2 ;;
  esac
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
registry="${repo_root}/contracts/supply-chain/v1/central-inference-cpu-components.json"

# The version/commit come from the registry, never from this script's defaults,
# so a re-pin cannot silently disagree with the registered component.
registered_version="$(jq -r '.components[] | select(.name=="gRPC C++") | .version' \
  "${registry}" | sed 's/^v//')"
registered_commit="$(jq -r '.components[] | select(.name=="gRPC C++") | .commit' \
  "${registry}")"
if [[ "${registered_version}" != "${grpc_version}" \
  || "${registered_commit}" != "${grpc_commit}" ]]; then
  echo "toolchain recipe disagrees with the component registry:" >&2
  echo "  script:   ${grpc_version} ${grpc_commit}" >&2
  echo "  registry: ${registered_version} ${registered_commit}" >&2
  exit 1
fi

if [[ -d "${install_prefix}" && "${force}" != true ]]; then
  echo "install prefix already exists: ${install_prefix}" >&2
  echo "the existing toolchain is what current evidence was built against;" >&2
  echo "pass --force only when deliberately re-creating it." >&2
  exit 2
fi

for command in git cmake jq make; do
  command -v "${command}" >/dev/null 2>&1 || { echo "missing ${command}" >&2; exit 1; }
done

if [[ -d "${source_dir}/.git" ]]; then
  observed_commit="$(git -C "${source_dir}" rev-parse HEAD)"
  if [[ "${observed_commit}" != "${grpc_commit}" ]]; then
    echo "existing source tree is at ${observed_commit}, expected ${grpc_commit}" >&2
    exit 1
  fi
else
  mkdir -p -- "$(dirname -- "${source_dir}")"
  git clone --depth 1 -b "v${grpc_version}" --recurse-submodules \
    --shallow-submodules https://github.com/grpc/grpc "${source_dir}"
  observed_commit="$(git -C "${source_dir}" rev-parse HEAD)"
  [[ "${observed_commit}" == "${grpc_commit}" ]] || {
    echo "cloned commit ${observed_commit} != registered ${grpc_commit}" >&2
    exit 1
  }
fi

# Flags mirror infer-cpp/Dockerfile exactly.
cmake -S "${source_dir}" -B "${build_dir}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_STANDARD=20 \
  -DgRPC_INSTALL=ON \
  -DCMAKE_INSTALL_PREFIX="${install_prefix}" \
  -DgRPC_BUILD_TESTS=OFF \
  -DgRPC_BUILD_CSHARP_EXT=OFF \
  -DgRPC_SSL_PROVIDER=package \
  -DgRPC_ZLIB_PROVIDER=package \
  -DBUILD_SHARED_LIBS=OFF
cmake --build "${build_dir}" -- -j"$(nproc)"
cmake --install "${build_dir}"

printf 'installed gRPC %s (%s) to %s from %s\n' \
  "${grpc_version}" "${grpc_commit}" "${install_prefix}" "${source_dir}"
printf 'export MASI_INF_GRPC_PREFIX=%s\n' "${install_prefix}"
