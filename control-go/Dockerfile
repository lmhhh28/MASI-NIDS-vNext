FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/control-core ./cmd/control-core && \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/control-migrate ./cmd/migrate

FROM alpine:3.22
RUN addgroup -S masi && adduser -S -G masi -u 65532 masi
COPY --from=build /out/control-core /usr/local/bin/control-core
COPY --from=build /out/control-migrate /usr/local/bin/control-migrate
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/control-core"]
