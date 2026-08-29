FROM golang:1.26.6-alpine@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/control-core ./cmd/control-core

FROM alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce
RUN addgroup -S masi && adduser -S -G masi -u 65532 masi
COPY --from=build /out/control-core /usr/local/bin/control-core
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/control-core"]
