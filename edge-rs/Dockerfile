ARG RUST_IMAGE=docker.io/library/rust@sha256:408fe88047cef61a2087653b0c5255fa51c0f2d6d94ddedd7a2562a9b91a46f6
ARG RUNTIME_IMAGE=gcr.io/distroless/cc-debian12@sha256:adcd20c7b4c988b73cbfbddb26d2eee574571e6d7c9ffea29b3821e0690efb77
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST
FROM ${RUST_IMAGE} AS builder

ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST

WORKDIR /workspace/edge-rs
ENV RUSTUP_TOOLCHAIN=1.97.1-x86_64-unknown-linux-gnu

COPY edge-rs/Cargo.toml edge-rs/Cargo.lock edge-rs/build.rs edge-rs/rust-toolchain.toml ./
COPY edge-rs/src ./src
COPY contracts /workspace/contracts

RUN test -n "${SOURCE_REVISION}" && test -n "${SOURCE_TREE_DIGEST}"
RUN --network=none --mount=type=bind,from=cargo_vendor,target=/vendor,readonly \
    CARGO_NET_OFFLINE=true cargo build --offline --locked --release --bin masi-edge \
      --config 'source.crates-io.replace-with="vendored-sources"' \
      --config 'source.vendored-sources.directory="/vendor"'

FROM ${RUNTIME_IMAGE}

ARG SOURCE_DATE_EPOCH=1786233600
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST
LABEL org.opencontainers.image.title="MASI-NIDS-vNext Rust Edge Agent" \
      org.opencontainers.image.description="Sole per-target P4Runtime owner and canonical telemetry/inference router" \
      org.opencontainers.image.source="https://masi-nids.example/MASI-NIDS-vNext" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.masi-nids.source-tree.digest="${SOURCE_TREE_DIGEST}" \
      io.masi-nids.source-date-epoch="${SOURCE_DATE_EPOCH}" \
      io.masi-nids.rust-builder.digest="sha256:408fe88047cef61a2087653b0c5255fa51c0f2d6d94ddedd7a2562a9b91a46f6" \
      io.masi-nids.runtime-base.digest="sha256:adcd20c7b4c988b73cbfbddb26d2eee574571e6d7c9ffea29b3821e0690efb77" \
      io.masi-nids.module="MOD-EDGE-001" \
      io.masi-nids.qualification="HOLD_DEC_001"

COPY --from=builder --chmod=0555 /workspace/edge-rs/target/release/masi-edge /usr/local/bin/masi-edge

USER 65532:65532
EXPOSE 7444
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/masi-edge"]
