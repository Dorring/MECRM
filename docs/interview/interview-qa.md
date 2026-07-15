# Interview Q&A

## Why LangGraph?

The workflow has explicit state transitions: retrieval, policy evaluation,
approval, execution, and failure/degraded outcomes. A graph makes those paths
visible and testable. It is not used as a substitute for ordinary application
code.

## How do you prevent an LLM from executing unsafe actions?

Model output is validated into a structured contract, tools are tenant-scoped,
OPA evaluates policy, high-risk actions require approval, and PostgreSQL RLS
enforces tenant isolation at the data boundary. The model proposes; it does not
authorize itself.

## How will you evaluate the AI behavior?

The H2 evaluation suite uses curated inputs for routing, retrieval,
groundedness, governance, and resilience. It starts with deterministic checks
for schema validity, tenant filtering, tool selection, and unsafe actions. The
report records dataset and commit versions. LLM-as-judge can supplement
qualitative review but does not replace hard security gates.

## Why RLS and OPA together?

OPA is a policy decision point before action. RLS is enforcement where data is
queried. The dual layer protects against different classes of mistakes.

## Why use Kafka in a CRM portfolio?

It separates transactional API work from asynchronous agent processing,
auditing, replay, and consumers. It is intentionally a trade-off: it makes the
system more complex and would not be chosen for every small CRM.

## What happens if the vector store or model is down?

The workflow must surface a degraded result and avoid claiming retrieved
evidence. Sensitive actions fail closed where policy is unavailable. The default
demo does not depend on a live model so its operational evidence stays
repeatable.

## What would you change for production?

Use managed secrets, database, broker, centralized logs/traces, provider
routing, rate/cost controls, alerting, and a managed container runtime. The
first goal would be operational ownership, not merely moving the same Compose
file to Kubernetes.
