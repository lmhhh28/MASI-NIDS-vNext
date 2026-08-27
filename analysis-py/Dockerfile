ARG PYTHON_BUILDER_IMAGE=docker.io/library/python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG RUNTIME_IMAGE=cgr.dev/chainguard/python@sha256:d812438658b47b73cb4c089f4cca09bca1ba50f6cd1843133864ee074d9ec49b
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST

FROM ${PYTHON_BUILDER_IMAGE} AS runtime-builder

ARG SOURCE_DATE_EPOCH=1787334400

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INDEX=1

COPY --from=wheelhouse / /wheelhouse
RUN --network=none python -m pip install --no-index --find-links=/wheelhouse --no-cache-dir \
      masi-analysis-plugin==1.0.0 \
    && rm -rf /wheelhouse /root/.cache \
    && find /usr/local/lib/python3.12/site-packages -type d -name '__pycache__' -prune -exec rm -rf '{}' +

COPY --chown=0:0 contracts/analysis/config /opt/masi/contracts/analysis/config
COPY --chown=0:0 contracts/analysis/binding /opt/masi/contracts/analysis/binding
COPY --chown=0:0 contracts/plugin/manifest /opt/masi/contracts/plugin/manifest
RUN chmod -R a-w /opt/masi/contracts /usr/local/lib/python3.12/site-packages/masi_analysis \
    && chmod 0555 /usr/local/bin/masi-analysis

FROM ${RUNTIME_IMAGE}

ARG SOURCE_DATE_EPOCH=1787334400
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST

LABEL org.opencontainers.image.title="MASI-NIDS-vNext Analysis Plugin" \
      org.opencontainers.image.description="Bounded grounded non-executable LangGraph Analysis Agent" \
      org.opencontainers.image.source="https://masi-nids.example/MASI-NIDS-vNext" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.masi-nids.source-tree.digest="${SOURCE_TREE_DIGEST}" \
      io.masi-nids.source-date-epoch="${SOURCE_DATE_EPOCH}" \
      io.masi-nids.python-builder.digest="sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2" \
      io.masi-nids.runtime-base.digest="sha256:d812438658b47b73cb4c089f4cca09bca1ba50f6cd1843133864ee074d9ec49b" \
      io.masi-nids.runtime-library.libcrypto.digest="sha256:72db1b3de8b7dfbaba4c056135f408da555f9d5e137c82129478e07e769f8070" \
      io.masi-nids.runtime-library.libssl.digest="sha256:9aec161fdbc82d3e4280f5084843118939f1f4acc53c98ec963de03cfe812fad" \
      io.masi-nids.runtime-base.python.fallback="false" \
      io.masi-nids.python.version="3.12.13" \
      io.masi-nids.python.path="/usr/local/bin/python3.12" \
      io.masi-nids.runtime-profile="analysis-agent-runtime/v1" \
      io.masi-nids.module="MOD-AGENT-001" \
      io.masi-nids.qualification="HOLD_NOT_QUALIFIED"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INDEX=1 \
    LD_LIBRARY_PATH=/opt/masi/runtime-libs

COPY --from=runtime-builder --chown=0:0 /usr/local /usr/local
COPY --from=runtime-builder --chown=0:0 /opt/masi/contracts /opt/masi/contracts
COPY --from=runtime-builder --chown=0:0 /usr/lib/x86_64-linux-gnu/libcrypto.so.3 /opt/masi/runtime-libs/libcrypto.so.3
COPY --from=runtime-builder --chown=0:0 /usr/lib/x86_64-linux-gnu/libssl.so.3 /opt/masi/runtime-libs/libssl.so.3

USER 65532:65532
EXPOSE 7446
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=5s --timeout=2s --start-period=10s --retries=3 \
  CMD ["/usr/local/bin/masi-analysis", "--config", "/run/masi-analysis-config/config.json", "--probe", "ready"]
ENTRYPOINT ["/usr/local/bin/masi-analysis"]
CMD ["--config", "/run/masi-analysis-config/config.json"]
