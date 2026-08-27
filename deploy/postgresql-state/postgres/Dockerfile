FROM golang:1.26.6-alpine@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS gosu-build
ARG GOSU_VERSION=1.19
ARG GOSU_SHA256=cd9719b775dbfedae53923c9b0dc792b66d42c51e0b36652ed6f747fbadc0164
RUN apk add --no-cache ca-certificates=20260611-r0 curl=8.21.0-r0
WORKDIR /src
RUN curl --fail --location --proto '=https' --tlsv1.2 \
      --output gosu.tar.gz "https://github.com/tianon/gosu/archive/refs/tags/${GOSU_VERSION}.tar.gz" && \
    echo "${GOSU_SHA256}  gosu.tar.gz" | sha256sum -c - && \
    tar --extract --gzip --file gosu.tar.gz --strip-components=1 && \
    go mod download && \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/gosu .

FROM postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2
LABEL org.opencontainers.image.title="MASI-NIDS PostgreSQL State server" \
      org.opencontainers.image.version="18.6" \
      org.opencontainers.image.description="Digest-pinned PostgreSQL 18 with a security-supported rebuilt gosu"
COPY --from=gosu-build --chmod=0555 /out/gosu /usr/local/bin/gosu
RUN gosu --version | grep -F '1.19 (go1.26.6' && postgres --version | grep -F '18.6'

# The module-gate runtime prepares the PostgreSQL volume, archive, TLS, and
# secret ownership for the fixed Alpine postgres identity before startup.
# Keep the image fail-closed as that identity instead of relying on a root
# entrypoint to drop privileges at runtime.
USER 70:70
