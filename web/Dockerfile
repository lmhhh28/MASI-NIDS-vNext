ARG NODE_IMAGE=docker.io/library/node:22.22.3-alpine3.23@sha256:968df39aedcea65eeb078fb336ed7191baf48f972b4479711397108be0966920
ARG NGINX_IMAGE=docker.io/nginxinc/nginx-unprivileged:1.29.5-alpine@sha256:42a7d7f2ee23e9f5a1dcdf3647ba5c585bbd18f79e79cd817e70e8cd61c55779
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST

FROM ${NODE_IMAGE} AS build
WORKDIR /workspace/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY contracts/generated/typescript/control-api /workspace/contracts/generated/typescript/control-api
COPY web/ ./
RUN npm run build && npm run verify:bundle && npm run verify:security

FROM ${NGINX_IMAGE}
ARG SOURCE_DATE_EPOCH=1787760000
ARG SOURCE_REVISION
ARG SOURCE_TREE_DIGEST
LABEL org.opencontainers.image.title="MASI-NIDS-vNext SOC Web" \
      org.opencontainers.image.description="Static Vue SOC SPA; same-origin Control API and SSE only" \
      org.opencontainers.image.source="https://masi-nids.example/MASI-NIDS-vNext" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.masi-nids.source-tree.digest="${SOURCE_TREE_DIGEST}" \
      io.masi-nids.source-date-epoch="${SOURCE_DATE_EPOCH}" \
      io.masi-nids.builder-base.digest="sha256:968df39aedcea65eeb078fb336ed7191baf48f972b4479711397108be0966920" \
      io.masi-nids.runtime-base.digest="sha256:42a7d7f2ee23e9f5a1dcdf3647ba5c585bbd18f79e79cd817e70e8cd61c55779" \
      io.masi-nids.runtime-profile="web-spa/v1" \
      io.masi-nids.browser-profile="web-browser/v1" \
      io.masi-nids.module="MOD-WEB-001" \
      io.masi-nids.qualification="HOLD_NOT_QUALIFIED"
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build --chown=101:101 /workspace/web/dist/ /usr/share/nginx/html/
USER 101:101
EXPOSE 8080
STOPSIGNAL SIGQUIT
HEALTHCHECK --interval=5s --timeout=2s --start-period=5s --retries=3 \
  CMD ["wget", "-q", "-O", "-", "http://127.0.0.1:8080/healthz"]
