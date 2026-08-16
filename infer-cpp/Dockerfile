# Central Inference Gateway Dockerfile (CPU-only first release).
#
# Builder: ubuntu:22.04 + pinned ONNX Runtime 1.19.0 CPU archive + gRPC/Protobuf.
# Runtime: distroless/cc-debian12 non-root 65532.
#
# This image builds the Gateway binary only. Triton is NOT bundled; the
# Gateway connects to an external Triton server with model-control-mode=none.

ARG BUILDER_IMAGE=docker.io/library/ubuntu:22.04@sha256:0bced47fffa3361d98fe3e3c0e5b0b9c75b3f4e1c3d4e5f6a7b8c9d0e1f2a3b4
ARG RUNTIME_IMAGE=gcr.io/distroless/cc-debian12@sha256:adcd20c7b4c988b73cbfbddb26d2eee574571e6d7c9ffea29b3821e0690efb77
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST

FROM ${BUILDER_IMAGE} AS builder

ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST
ARG ORT_VERSION=1.19.0

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake g++ g++-12 git make python3 python3-pip \
    libssl-dev ca-certificates curl xz-utils \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Copy contracts (protobuf source) and infer-cpp source.
COPY contracts /workspace/contracts
COPY infer-cpp /workspace/infer-cpp

# Fetch and pin ONNX Runtime CPU archive.
RUN mkdir -p /opt/ort && \
    curl -fsSL "https://github.com/microsoft/onnxruntime/releases/download/v${ORT_VERSION}/onnxruntime-linux-x64-${ORT_VERSION}.tgz" \
      -o /tmp/ort.tgz && \
    tar -xzf /tmp/ort.tgz -C /opt/ort && \
    mv /opt/ort/onnxruntime-linux-x64-${ORT_VERSION} /opt/ort/root && \
    rm /tmp/ort.tgz

# Build the Gateway. We need gRPC + Protobuf from system packages or vcpkg.
# For the first release we rely on the system protobuf-compiler and libgrpc-dev.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libprotobuf-dev protobuf-compiler libgrpc++-dev libgrpc-dev protobuf-compiler-grpc \
  && rm -rf /var/lib/apt/lists/*

RUN test -n "${SOURCE_REVISION}" && test -n "${SOURCE_TREE_DIGEST}"

WORKDIR /workspace/infer-cpp

# Configure and build with the vendored ORT.
RUN cmake -B build/cpu-release \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CXX_STANDARD=20 \
    -DMASI_INF_BUILD_TESTS=OFF \
    -DMASI_INF_OFFLINE=ON \
    -DCMAKE_FIND_PACKAGE_PREFER_CONFIG=OFF && \
    cmake --build build/cpu-release --target masi_inference_gateway -- -j$(nproc)

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
      io.masi-nids.module="MOD-INF-001" \
      io.masi-nids.qualification="HOLD_DEC_001"

COPY --from=builder --chmod=0555 /workspace/infer-cpp/build/cpu-release/masi_inference_gateway /usr/local/bin/masi_inference_gateway

USER 65532:65532
EXPOSE 7443
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/masi_inference_gateway"]