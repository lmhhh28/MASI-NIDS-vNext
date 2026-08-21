FROM golang:1.26.6-alpine@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS build
WORKDIR /src/db
COPY db/go.mod db/go.sum ./
RUN go mod download
COPY db/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/masi-dbctl ./cmd/masi-dbctl && \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/masi-dbload ./cmd/masi-dbload

FROM alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce
LABEL org.opencontainers.image.title="MASI-NIDS PostgreSQL State migration job" \
      org.opencontainers.image.description="Independent, fail-closed PostgreSQL 18 schema migration and state inspection tool"
RUN apk add --no-cache ca-certificates=20260611-r0 && \
    addgroup -S -g 70 masi-state && \
    adduser -S -D -H -u 70 -G masi-state masi-state
COPY --from=build --chmod=0555 /out/masi-dbctl /usr/local/bin/masi-dbctl
COPY --from=build --chmod=0555 /out/masi-dbload /usr/local/bin/masi-dbload
COPY --chown=70:70 --chmod=0555 db/migrations/ /opt/masi/db/migrations/
USER 70:70
ENTRYPOINT ["/usr/local/bin/masi-dbctl"]
