# ADR-0011: Silo as the local and staging S3 runtime

- Status: Accepted
- Date: 2026-11-23
- Deciders: Platform engineering

## Context

The architecture requires S3-compatible object storage and names MinIO as the local
implementation. In September 2026 MinIO removed the community `minio/minio` and `minio/mc`
images from Docker Hub, made anonymous `quay.io/minio/minio` pulls fail, archived the
repository, and stopped publishing community binaries. A fresh `docker compose up` cannot
pull the named images, so leaving the compose file unchanged would break the Phase 1 exit
criterion that the whole system starts from a clean checkout.

## Decision

Use `pgsty/silo` and `pgsty/mc` as the local and staging S3 server and client. Silo is a
maintained MinIO-compatible fork: it serves the S3 API, keeps the MinIO root-credential
environment variables, health endpoint, console, and `mc` commands. Object-store clients
continue to speak the S3 contract, not a vendor API.

Images are pinned to a release tag. The compose service is named `silo`; application
configuration still uses `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, and the bucket
names, so swapping the runtime later is a compose change, not a service change.

## Consequences

Positive:

- Clean checkouts can pull images again.
- No application code or storage contract changes; all clients use S3.
- The server, console, and client remain familiar operations.

Negative:

- This is a fork, not the archived upstream MinIO; security updates come from the fork.
- The image name differs from the Phase 0 text; `deployment-architecture.md` should be read
  as "S3-compatible", with this ADR as the current local implementation.
- A future move to managed S3 in production is still expected; staging remains on a
  container for now.

## Alternatives considered

| Alternative | Why not now |
|---|---|
| Pin an old cached `minio/minio` image | Not pullable on a clean machine, unmaintained, and misses the October 2025 security fix |
| Chainguard MinIO image | Pullable, but still a frozen community build; no upstream maintenance |
| RustFS / Garage / SeaweedFS | Viable S3-compatible stores, but more configuration churn for Phase 1; keep as candidates |
| MinIO AIStor | Different product and licence path; not needed for local development |

## Revisit trigger

- Silo stops publishing releases or has a security advisory without a timely fix.
- The platform adopts managed S3 or an object-store gateway in staging.
- A second S3-compatible runtime is needed for multi-backend testing.
