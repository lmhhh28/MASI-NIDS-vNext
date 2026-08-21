ARG RUST_IMAGE=docker.io/library/rust@sha256:408fe88047cef61a2087653b0c5255fa51c0f2d6d94ddedd7a2562a9b91a46f6
ARG RUNTIME_IMAGE=gcr.io/distroless/cc-debian12@sha256:adcd20c7b4c988b73cbfbddb26d2eee574571e6d7c9ffea29b3821e0690efb77
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST
FROM ${RUST_IMAGE} AS builder

WORKDIR /workspace/plugin-host-rs
ENV RUSTUP_TOOLCHAIN=1.97.1-x86_64-unknown-linux-gnu

COPY plugin-host-rs/Cargo.toml plugin-host-rs/Cargo.lock plugin-host-rs/build.rs plugin-host-rs/rust-toolchain.toml ./
COPY plugin-host-rs/src ./src
COPY contracts /workspace/contracts

RUN --network=none --mount=type=bind,from=cargo_vendor,target=/vendor,readonly \
    CARGO_NET_OFFLINE=true cargo build --offline --locked --release --bin masi-plugin-host \
      --config 'source.crates-io.replace-with="vendored-sources"' \
      --config 'source.vendored-sources.directory="/vendor"'

FROM ${RUNTIME_IMAGE}

ARG SOURCE_DATE_EPOCH=1787270400
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST
LABEL org.opencontainers.image.title="MASI-NIDS-vNext Plugin Runtime Host" \
      org.opencontainers.image.description="Deny-by-default Wasm Component and explicitly Host-managed service runtime" \
      org.opencontainers.image.source="https://masi-nids.example/MASI-NIDS-vNext" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.masi-nids.source-tree.digest="${SOURCE_TREE_DIGEST}" \
      io.masi-nids.source-date-epoch="${SOURCE_DATE_EPOCH}" \
      io.masi-nids.rust-builder.digest="sha256:408fe88047cef61a2087653b0c5255fa51c0f2d6d94ddedd7a2562a9b91a46f6" \
      io.masi-nids.runtime-base.digest="sha256:adcd20c7b4c988b73cbfbddb26d2eee574571e6d7c9ffea29b3821e0690efb77" \
      io.masi-nids.runtime-profile="plugin-runtime-host/v1" \
      io.masi-nids.wasmtime.version="47.0.3" \
      io.masi-nids.module="MOD-PLUGIN-001" \
      io.masi-nids.qualification="HOLD_NOT_QUALIFIED"

COPY --from=builder --chmod=0555 /workspace/plugin-host-rs/target/release/masi-plugin-host /usr/local/bin/masi-plugin-host

USER 65532:65532
EXPOSE 7445 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=5s --timeout=2s --start-period=10s --retries=3 \
  CMD ["/usr/local/bin/masi-plugin-host", "--config", "/run/plugin-host-config/config.json", "--probe", "ready"]
ENTRYPOINT ["/usr/local/bin/masi-plugin-host"]
