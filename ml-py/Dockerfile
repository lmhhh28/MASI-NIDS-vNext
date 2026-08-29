ARG PYTHON_BUILDER_IMAGE=docker.io/library/python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG RUNTIME_LIBRARIES_IMAGE=cgr.dev/chainguard/python@sha256:d812438658b47b73cb4c089f4cca09bca1ba50f6cd1843133864ee074d9ec49b
ARG RUNTIME_IMAGE=gcr.io/distroless/cc-debian12@sha256:adcd20c7b4c988b73cbfbddb26d2eee574571e6d7c9ffea29b3821e0690efb77
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST

FROM ${RUNTIME_LIBRARIES_IMAGE} AS runtime-libraries

FROM ${PYTHON_BUILDER_IMAGE} AS runtime-builder

ARG SOURCE_DATE_EPOCH=1787334400

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INDEX=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    XGBOOST_BUILD_DOC=0

COPY --from=wheelhouse / /wheelhouse
RUN --network=none python -m pip install --no-index --find-links=/wheelhouse --no-cache-dir \
      masi-offline-ml==1.0.0 \
    && rm -rf /wheelhouse /root/.cache \
    && find /usr/local/lib/python3.12/site-packages -type d -name '__pycache__' -prune -exec rm -rf '{}' +

COPY --chown=0:0 contracts/dataset /opt/masi/repo/contracts/dataset
COPY --chown=0:0 contracts/model-explanation /opt/masi/repo/contracts/model-explanation
COPY --chown=0:0 contracts/profiles/v1/dataset-p4-window-binary.json /opt/masi/repo/contracts/profiles/v1/dataset-p4-window-binary.json
COPY --chown=0:0 contracts/profiles/v1/model-explanation-evidence.json /opt/masi/repo/contracts/profiles/v1/model-explanation-evidence.json
COPY --chown=0:0 contracts/profiles/v1/edge-feature-window.json /opt/masi/repo/contracts/profiles/v1/edge-feature-window.json
COPY --chown=0:0 contracts/profiles/v1/p4-stateless-firewall-bmv2.json /opt/masi/repo/contracts/profiles/v1/p4-stateless-firewall-bmv2.json
COPY --chown=0:0 contracts/golden/telemetry/snapshot-v1.json /opt/masi/repo/contracts/golden/telemetry/snapshot-v1.json
COPY --chown=0:0 contracts/inference/v1/profile.json /opt/masi/repo/contracts/inference/v1/profile.json
COPY --chown=0:0 contracts/inference/v1/model-runtime-central-cpu.json /opt/masi/repo/contracts/inference/v1/model-runtime-central-cpu.json
COPY --chown=0:0 contracts/inference/v1/optimization-profile-central-cpu.json /opt/masi/repo/contracts/inference/v1/optimization-profile-central-cpu.json
COPY --chown=0:0 ml-py/uv.lock /opt/masi/repo/ml-py/uv.lock
COPY --chown=0:0 ml-py/src/masi_offline_ml/dataset.py /opt/masi/repo/ml-py/src/masi_offline_ml/dataset.py
COPY --chown=0:0 p4/src/masi_switch.p4 /opt/masi/repo/p4/src/masi_switch.p4
RUN mkdir -p /work \
    && chmod -R a-w /opt/masi/repo /usr/local/lib/python3.12/site-packages/masi_offline_ml \
    && chmod 0555 /usr/local/bin/masi-offline-ml /work

FROM ${RUNTIME_IMAGE}

ARG SOURCE_DATE_EPOCH=1787334400
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST

LABEL org.opencontainers.image.title="MASI-NIDS-vNext Offline ML Pipeline" \
      org.opencontainers.image.description="Deterministic bounded P4-window training and immutable ONNX repository job" \
      org.opencontainers.image.source="https://masi-nids.example/MASI-NIDS-vNext" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.masi-nids.source-tree.digest="${SOURCE_TREE_DIGEST}" \
      io.masi-nids.source-date-epoch="${SOURCE_DATE_EPOCH}" \
      io.masi-nids.python-builder.digest="sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2" \
      io.masi-nids.runtime-libraries.image.digest="sha256:d812438658b47b73cb4c089f4cca09bca1ba50f6cd1843133864ee074d9ec49b" \
      io.masi-nids.runtime-library.zlib.digest="sha256:cd0d4cbe528e1d83875605c2aa10f0a9ea2d34a055d24db96642e6f78ad7206d" \
      io.masi-nids.runtime-library.libffi.digest="sha256:baae077ca48493f45210ce490bb281403b1d68362adef95495f5e0bc28e70eaa" \
      io.masi-nids.runtime-base.digest="sha256:adcd20c7b4c988b73cbfbddb26d2eee574571e6d7c9ffea29b3821e0690efb77" \
      io.masi-nids.runtime-base.python.fallback="false" \
      io.masi-nids.python.version="3.12.13" \
      io.masi-nids.python.path="/usr/local/bin/python3.12" \
      io.masi-nids.runtime-profile="offline-ml-runtime/v1" \
      io.masi-nids.module="MOD-ML-001" \
      io.masi-nids.qualification="HOLD_NOT_QUALIFIED"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    XGBOOST_BUILD_DOC=0

COPY --from=runtime-builder --chown=0:0 /usr/local /usr/local
COPY --from=runtime-builder --chown=0:0 /opt/masi/repo /opt/masi/repo
COPY --from=runtime-builder --chown=65532:65532 /work /work
COPY --from=runtime-libraries --chown=0:0 /usr/lib/libz.so.1.3.2 /usr/lib/libz.so.1.3.2
COPY --from=runtime-libraries --chown=0:0 /usr/lib/libz.so.1.3.2 /usr/lib/libz.so.1
COPY --from=runtime-libraries --chown=0:0 /usr/lib/libffi.so.8.5.0 /usr/lib/libffi.so.8.5.0
COPY --from=runtime-libraries --chown=0:0 /usr/lib/libffi.so.8.5.0 /usr/lib/libffi.so.8

USER 65532:65532
WORKDIR /work
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/masi-offline-ml"]
CMD ["--version"]
