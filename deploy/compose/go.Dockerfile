# syntax=docker/dockerfile:1.7

FROM golang:1.26-alpine AS build
ARG VERSION=dev
WORKDIR /src

COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY cmd ./cmd
COPY internal ./internal
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    set -e; \
    for service in api scheduler fetcher indexer reconciler; do \
      CGO_ENABLED=0 go build -buildvcs=false -trimpath \
        -ldflags="-s -w -X github.com/kushalsrinivas/web-research-ai-engine/internal/platform/version.Version=${VERSION}" \
        -o "/out/${service}" "./cmd/${service}"; \
    done

FROM gcr.io/distroless/static-debian12:nonroot
ARG SERVICE
ARG VERSION=dev
LABEL org.opencontainers.image.title="${SERVICE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/kushalsrinivas/web-research-ai-engine"

COPY --from=build "/out/${SERVICE}" /app/service
USER 65532:65532
ENTRYPOINT ["/app/service"]
