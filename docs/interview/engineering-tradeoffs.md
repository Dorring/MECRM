# Engineering Trade-offs

## Why agent workflows instead of one chat completion?

The CRM has stateful, multi-step work: retrieve tenant-owned knowledge, assess
risk, propose an action, obtain approval when necessary, and emit auditable
results. A graph makes the transitions explicit and testable. Straight-line
chat is still appropriate for small, read-only tasks.

## Why Kafka?

Kafka decouples durable domain events from agent consumers, projections, replay,
and audit handling. It adds operational complexity, so it is justified here only
because the project demonstrates asynchronous workflow boundaries and replay;
a small CRUD-only application would not need it.

## Why both OPA and PostgreSQL RLS?

OPA decides whether an actor or workflow may attempt an action. RLS is the
database-level enforcement point that prevents a query from returning another
tenant's data even if application code has a defect. They protect different
layers and are deliberately not substitutes.

## Why is Ollama opt-in?

Local models add a multi-gigabyte image, hardware/runtime requirements, model
downloads, and non-deterministic latency. Default local and CI paths therefore
remain model-independent. A local model is enabled only when a user explicitly
wants live inference.

## Why deterministic evaluation first?

Security and state-machine behavior require repeatable evidence. Deterministic
providers and rule-based evaluators make tenant leakage, invalid schemas, wrong
tool selection, and unsafe side effects testable in CI. Qualitative LLM judging
can be added later for relevance and writing quality, but must not be the only
safety gate.

## Why Docker Desktop rather than Kubernetes now?

The portfolio's immediate objective is reproducible local execution and clear
engineering reasoning. A real cluster requires hosting, secrets, networking,
observability, and operational ownership. The architecture documents a migration
path without pretending that a cluster has already been validated.
