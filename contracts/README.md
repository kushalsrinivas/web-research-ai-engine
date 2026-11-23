# Contracts

Executable shared contracts live here. They are the authority for cross-service payloads;
Go and Python must validate against the same files.

```
events/v1/envelope.schema.json          common doc.* envelope
events/v1/payloads/*.schema.json        event-specific payloads
fixtures/events/*.valid.json            shared valid fixtures
fixtures/events/invalid/*.json          fixtures that must be rejected
```

## Validation

```bash
make contracts
```

This runs the Go schema/fixture test and the Python schema/fixture test. Both sides load the
files in this directory; a mismatch fails CI.

## Evolution

- Additive optional fields do not bump `schema_version`.
- Removing/renaming a field, changing a type, or changing semantics bumps the schema version
  and needs a dual-publish/dual-read window.
- Consumers must tolerate unknown fields; the envelope and payloads use
  `additionalProperties: true`.
- Deploy consumers before producers.
- Every event type needs a valid fixture. Invalid fixtures are required for the envelope's
  required fields and core constraints.

The human-readable contract remains `docs/data/event-schema.md`. This directory is its
executable form.
