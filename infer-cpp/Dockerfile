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

RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake g++ g++-12 git make python3 python3-pip \
    libssl-dev zlib1g-dev ca-certificates curl xz-utils \
  && rm -rf /var/lib/apt/lists/*

# Build the registered gRPC/Protobuf from the pinned commit. The Gateway build
# refuses any other version, so the image cannot silently link a different
# gRPC than the one bound to the module's wire evidence.
RUN git clone --depth 1 -b v${GRPC_VERSION} --recurse-submodules --shallow-submodules \
      https://github.com/grpc/grpc /tmp/grpc && \
    test "$(git -C /tmp/grpc rev-parse HEAD)" = "${GRPC_COMMIT}" && \
    cmake -S /tmp/grpc -B /tmp/grpc-build \
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
    rm -rf /tmp/grpc /tmp/grpc-build

# nlohmann/json is a header-only dependency the Gateway sources include
# directly (envelope/startup parsing); there is no find_package for it, the
# compiler just needs <nlohmann/json.hpp> on the default include path. It is
# installed in its OWN layer AFTER the expensive gRPC build so that the gRPC
# layer cache is preserved when this dependency is added/changed. (ubuntu:22.04
# ships nlohmann-json3-dev; the host toolchain has the same header at
# /usr/include/nlohmann/json.hpp.)
RUN apt-get update && apt-get install -y --no-install-recommends nlohmann-json3-dev \
  && rm -rf /var/lib/apt/lists/*

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