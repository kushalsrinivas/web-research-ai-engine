# ADR-0009: LLM provider abstraction at the gateway, not in every service

- Status: Accepted
- Date: 2026-11-22
- Deciders: Platform engineering, finance

## Context

Model choice changes monthly: price, context window, structured-output reliability, latency,
and availability differ per provider and per task. The system needs:

- Failover when a provider degrades.
- Hard budget enforcement per document, investigation, tenant, and day.
- A single place where prompts are versioned, cached, redacted, and audited.
- Structured outputs validated against schemas, with one bounded repair attempt.
- Model pinning so a provider's silent model update cannot change extraction quality overnight.
- No provider credentials in four different services.

## Decision

Build a **thin internal abstraction inside `llm-gateway`**, and make it the only egress for
model calls.

Contract:

```python
class LLMRequest:
    task: Literal["summary", "claims", "entities", "adjudicate", "embed", "research_step"]
    class_: Literal["interactive", "batch"]
    messages: list[Message]
    json_schema: dict | None
    max_tokens: int
    temperature: float          # 0 for extraction
    budget: Budget              # max_usd + scope
    tenant_id: str
    investigation_id: str | None
    document: DocumentRef | None
```

The gateway owns:

| Concern | Behavior |
|---|---|
| Routing | Per-task ordered model chain, capability + cost aware; batch and interactive classes have separate chains |
| Pinning | Exact model version recorded per response; no floating aliases in production |
| Retries | Provider errors retried twice with jitter; `429` honors `Retry-After` |
| Failover | Next model in chain on 5xx/timeout; never on content refusal |
| Structured output | JSON Schema validation; one repair prompt; then `502 schema_invalid` |
| Budget | Row-level ledger, checked and reserved before the call; `402` if exceeded |
| Cache | SHA-256 of `(model, messages, params, schema)`; TTL per task; cache hits return `cached=true` |
| Redaction | Secret patterns stripped; tenant policy applied before egress |
| Logging | Prompt hash + token counts by default; raw prompts only with `no_prompt_log=false` and 7-day retention |
| Embeddings | Batch up to 256 texts; returns `model_version` required by the indexer |

Rules for callers:

- No provider SDK outside `llm-gateway`.
- No prompt strings outside the service that owns the task, registered in
  `contracts/prompts/{task}/{version}.md` and reviewed like code.
- Extraction uses `temperature=0` and a JSON Schema; free-form output is only for the final
  research report.
- A caller must pass a budget; there is no unlimited default.

## Consequences

Positive:

- One place to change providers, models, and prices.
- Cost is enforceable, attributable, and auditable per investigation and document.
- Prompt injection defenses, redaction, and logging policy are centralized.
- Provider failover does not require changes in four services.
- Swapping a provider is a config change plus a prompt regression run.

Negative:

- The lowest common denominator: provider-specific features (tool APIs, caching primitives,
  multimodal) are harder to use. Accepted for text extraction in Phase 1.
- The gateway is a critical path and a potential single point of failure. Mitigation: 2+
  replicas, per-caller timeouts, and degraded modes (`enricher` falls back to local extraction;
  research fails the step, not the investigation).
- A custom gateway is code to maintain. Kept deliberately small (routing, ledger, validation,
  cache); no prompt framework, no agent framework.
- Prompt portability requires a regression suite per model in the chain.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Provider SDK directly in each service | Credential sprawl, no unified budget, inconsistent retries, impossible failover |
| LangChain / LlamaIndex | Large surface, opaque prompt assembly, frequent breaking changes; debugging a failed claim becomes guesswork |
| LiteLLM proxy as the abstraction | Close fit; could replace our internals later, but it does not own our tenant budgets, ledger semantics, or task-level policy |
| OpenRouter / aggregator only | Single vendor dependency and data-egress terms; useful as one provider in the chain, not the abstraction |
| No abstraction (pick one provider) | Cheapest today, most expensive at the first outage or price change |

## Revisit trigger

- Gateway maintenance exceeds ~2 days/month → evaluate LiteLLM or a managed gateway behind the
  same internal interface.
- Provider-specific features become essential (e.g., native structured tool use with citations)
  and cannot be expressed in the neutral contract.
- Volume makes a self-hosted model cheaper for a task; the gateway routes to it like any other
  provider, no caller changes.
