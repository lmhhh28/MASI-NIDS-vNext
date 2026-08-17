# Central Inference Gateway Dockerfile (CPU-only first release).
#
# Builder: ubuntu:22.04 + the supply-chain registered gRPC/Protobuf built from
# the pinned commit. Runtime: distroless/cc-debian12 non-root 65532.
#
# The Gateway does not link ONNX Runtime: the CPU execution provider runs inside
# the pinned Triton `onnxruntime` backend, which is the module's only execution
# plane. Triton is NOT bundled; the Gateway connects to an external Triton with
# model-control-mode=none.

# Builder base pinned by its registry manifest-list digest (linux/amd64 child
# is selected by BuildKit via --platform). The builder image is a build-time
# input only; it is not shipped and is not registered as a runtime supply-chain
# component, but its digest is recorded below as a builder-base provenance label.
ARG BUILDER_IMAGE=docker.io/library/ubuntu:22.04@sha256:3b06811b2afd352be909dd088a004166d665dc76d38b13eada33522a9d915c6f
ARG RUNTIME_IMAGE=gcr.io/distroless/cc-debian12@sha256:adcd20c7b4c988b73cbfbddb26d2eee574571e6d7c9ffea29b3821e0690efb77
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST

FROM ${BUILDER_IMAGE} AS builder

# contracts/supply-chain/v1/central-inference-cpu-components.json
ARG GRPC_VERSION=1.82.1
ARG GRPC_COMMIT=acccf84c0df20487d64101f528e5d426541ca4e5

ENV DEBIAN_FRONTEND=noninteractive

ARG REGISTERED_GCC_VERSION=11.4.0
ARG REGISTERED_CMAKE_VERSION=3.22.1

RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake g++ git make python3 python3-pip \
    libssl-dev zlib1g-dev ca-certificates curl xz-utils \
  && rm -rf /var/lib/apt/lists/*

# The Gateway binary must be byte-identical to the host offline rebuild, so the
# builder toolchain cannot drift from the versions registered in
# contracts/supply-chain/v1/central-inference-cpu-components.json. A drifted
# base image fails the build here instead of silently producing a binary that
# cannot be reproduced.
RUN set -eu; \
    observed_gcc="$(g++ -dumpfullversion)"; \
    observed_cmake="$(cmake --version | head -1 | awk '{print $3}')"; \
    if [ "${observed_gcc}" != "${REGISTERED_GCC_VERSION}" ]; then \
      echo "builder g++ drift: registered=${REGISTERED_GCC_VERSION} observed=${observed_gcc}" >&2; exit 1; \
    fi; \
    if [ "${observed_cmake}" != "${REGISTERED_CMAKE_VERSION}" ]; then \
      echo "builder cmake drift: registered=${REGISTERED_CMAKE_VERSION} observed=${observed_cmake}" >&2; exit 1; \
    fi

# Build the registered gRPC/Protobuf from the pinned commit. The Gateway build
# refuses any other version, so the image cannot silently link a different
# gRPC than the one bound to the module's wire evidence.
# The clone directory is part of the reproducibility contract: gRPC bakes
# __FILE__ into 488 assertion/log strings, so building at a different absolute
# path yields a different Gateway binary. GRPC_SOURCE_DIR therefore matches the
# host toolchain source path exactly (see infer-cpp/README.md). Do not change it
# on one side only.
ARG GRPC_SOURCE_DIR=/opt/masi-toolchain/grpc-src

RUN git clone --depth 1 -b v${GRPC_VERSION} --recurse-submodules --shallow-submodules \
      https://github.com/grpc/grpc "${GRPC_SOURCE_DIR}" && \
    test "$(git -C "${GRPC_SOURCE_DIR}" rev-parse HEAD)" = "${GRPC_COMMIT}" && \
    cmake -S "${GRPC_SOURCE_DIR}" -B /tmp/grpc-build \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_CXX_STANDARD=20 \
      -DgRPC_INSTALL=ON \
      -DCMAKE_INSTALL_PREFIX=/opt/masi-toolchain/grpc-${GRPC_VERSION} \
      -DgRPC_BUILD_TESTS=OFF \
      -DgRPC_BUILD_CSHARP_EXT=OFF \
      -DgRPC_SSL_PROVIDER=package \
      -DgRPC_ZLIB_PROVIDER=package \
      -DBUILD_SHARED_LIBS=OFF && \
    cmake --build /tmp/grpc-build -- -j"$(nproc)" && \
    cmake --install /tmp/grpc-build && \
    rm -rf "${GRPC_SOURCE_DIR}" /tmp/grpc-build

# nlohmann/json is NOT installed from apt. ubuntu:22.04 ships 3.10.5 while the
# host toolchain carried 3.11.3, and the inline ABI namespace introduced in 3.11
# made the two builds produce different code. The exact header is vendored at
# infer-cpp/third_party/nlohmann/json.hpp, registered in
# contracts/supply-chain/v1/central-inference-vendored-sources.json, verified by
# digest at cmake configure time and asserted again at compile time.

WORKDIR /workspace

# Copy contracts (protobuf source) and infer-cpp source.
COPY contracts /workspace/contracts
COPY infer-cpp /workspace/infer-cpp

# SOURCE_REVISION / SOURCE_TREE_DIGEST are declared HERE, deliberately below the
# apt-get + gRPC + nlohmann layers. They change on every build (the smoke/gate
# recomputes SOURCE_TREE_DIGEST from the working tree), so declaring them above
# those layers would bust their cache on every source-tree change and force a
# full ~25min gRPC rebuild each run. Declared here, only the cheap test/cmake
# layers below are cache-busted by a digest change; the expensive toolchain
# layers stay cached. These args are used only by the `test -n` below and by the
# runtime-stage LABELs (re-declared in that stage).
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST
RUN test -n "${SOURCE_REVISION}" && test -n "${SOURCE_TREE_DIGEST}"

WORKDIR /workspace/infer-cpp

# A stale host build/ (e.g. CMakeCache.txt referencing the host source path)
# can leak into the builder via the COPY layer cache even with .dockerignore.
# Remove any copied build tree so cmake configures from a clean directory.
RUN rm -rf build && \
    cmake -B build/cpu-release \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CXX_STANDARD=20 \
    -DMASI_INF_BUILD_TESTS=OFF \
    -DMASI_INF_OFFLINE=ON \
    -DMASI_INF_GRPC_PREFIX=/opt/masi-toolchain/grpc-${GRPC_VERSION} && \
    cmake --build build/cpu-release --target masi_inference_gateway -- -j$(nproc)

# Stage the runtime shared libraries the Gateway dynamically links but the
# distroless cc-debian12 runtime does NOT ship: libssl/libcrypto (OpenSSL 3,
# via gRPC_SSL_PROVIDER=package) and libz (via gRPC_ZLIB_PROVIDER=package).
# The builder (ubuntu:22.04) has them from libssl-dev/zlib1g-dev. These are the
# EXACT libs the Gateway was built against, so there is no symbol/version drift.
# cp -L dereferences libz.so.1's soname symlink so the real object is staged.
# Placed AFTER the Gateway build so the expensive toolchain + gateway build
# layers stay cached when this staging step changes.
RUN mkdir -p /staging/libs && \
    cp -L /usr/lib/x86_64-linux-gnu/libssl.so.3   /staging/libs/libssl.so.3 && \
    cp -L /usr/lib/x86_64-linux-gnu/libcrypto.so.3 /staging/libs/libcrypto.so.3 && \
    cp -L /usr/lib/x86_64-linux-gnu/libz.so.1      /staging/libs/libz.so.1

FROM ${RUNTIME_IMAGE}

ARG SOURCE_DATE_EPOCH=1786233600
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST
LABEL org.opencontainers.image.title="MASI-NIDS-vNext Central Inference Gateway" \
      org.opencontainers.image.description="Stateless mTLS C++ Gateway; Triton model-control-mode=none; CPU-only first release" \
      org.opencontainers.image.source="https://masi-nids.example/MASI-NIDS-vNext" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.masi-nids.source-tree.digest="${SOURCE_TREE_DIGEST}" \
      io.masi-nids.source-date-epoch="${SOURCE_DATE_EPOCH}" \
      io.masi-nids.runtime-base.digest="sha256:adcd20c7b4c988b73cbfbddb26d2eee574571e6d7c9ffea29b3821e0690efb77" \
      io.masi-nids.builder-base.digest="sha256:3b06811b2afd352be909dd088a004166d665dc76d38b13eada33522a9d915c6f" \
      io.masi-nids.module="MOD-INF-001" \
      io.masi-nids.qualification="HOLD_DEC_001"

COPY --from=builder --chmod=0555 /workspace/infer-cpp/build/cpu-release/masi_inference_gateway /usr/local/bin/masi_inference_gateway

# Runtime shared libs the Gateway dynamically links; distroless cc-debian12 ships
# libc/libstdc++/libgcc but NOT libssl/libcrypto/libz. Copied from the builder
# staging dir into /usr/lib/x86_64-linux-gnu (the default multiarch ld.so search
# path where distroless's own libstdc++ already lives; no ldconfig needed).
COPY --from=builder /staging/libs/ /usr/lib/x86_64-linux-gnu/

USER 65532:65532
EXPOSE 7443
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/masi_inference_gateway"]