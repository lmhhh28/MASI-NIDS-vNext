FROM alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce AS build
ARG PGBOUNCER_VERSION=1.25.2
ARG PGBOUNCER_SHA256=924ad35113fd0a71c8e2dbe85b5d03445532e2b7b37a9f8a48983beea238b332
RUN apk add --no-cache \
      autoconf=2.72-r1 \
      automake=1.17-r1 \
      build-base=0.5-r3 \
      c-ares-dev=1.34.8-r0 \
      ca-certificates=20260611-r0 \
      curl=8.14.1-r3 \
      libevent-dev=2.1.13-r0 \
      libtool=2.5.4-r1 \
      openssl-dev=3.5.7-r0 \
      pkgconf=2.4.3-r0
WORKDIR /src
RUN curl --fail --location --proto '=https' --tlsv1.2 \
      --output pgbouncer.tar.gz \
      "https://www.pgbouncer.org/downloads/files/${PGBOUNCER_VERSION}/pgbouncer-${PGBOUNCER_VERSION}.tar.gz" && \
    echo "${PGBOUNCER_SHA256}  pgbouncer.tar.gz" | sha256sum -c - && \
    tar --extract --gzip --file pgbouncer.tar.gz --strip-components=1 && \
    ./configure --prefix=/usr/local && \
    make -j"$(getconf _NPROCESSORS_ONLN)" pgbouncer && \
    install -D -m 0555 pgbouncer /out/pgbouncer

FROM alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce
ARG PGBOUNCER_VERSION=1.25.2
LABEL org.opencontainers.image.title="MASI-NIDS PgBouncer" \
      org.opencontainers.image.version="${PGBOUNCER_VERSION}" \
      org.opencontainers.image.description="Pinned transaction-pooling PostgreSQL TLS boundary"
RUN apk add --no-cache \
      ca-certificates=20260611-r0 \
      c-ares=1.34.8-r0 \
      libevent=2.1.13-r0 \
      openssl=3.5.7-r0 && \
    addgroup -S -g 10003 pgbouncer && \
    adduser -S -D -H -u 10003 -G pgbouncer pgbouncer && \
    install -d -o 10003 -g 10003 -m 0750 /var/run/pgbouncer
COPY --from=build --chmod=0555 /out/pgbouncer /usr/local/bin/pgbouncer
USER 10003:10003
ENTRYPOINT ["/usr/local/bin/pgbouncer"]
CMD ["/etc/pgbouncer/pgbouncer.ini"]
