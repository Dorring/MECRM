# Phase 7: Permission-Aware, Tenant-Safe Caching

## Why naive caching is dangerous
Caching improves performance but can silently violate security in multi-tenant systems:
- Missing `tenant_id` in keys can cause cross-tenant data exposure.
- Missing user/role context in keys can cause permission escalation.
- Caching authorization decisions without invalidation causes stale access after role/policy changes.

This phase defines caching as a security control: cache hits must be safe and authorization-bound.

## Core safety guarantees
- Tenant isolation is absolute: tenant A never reads tenant B cached entries.
- Authorization binding: cached data is only returned when the request’s policy context matches the cached context.
- Explicit invalidation: authorization changes must take effect immediately (not “eventually”).
- Fail closed: never grant access without a successful policy evaluation.

## Cache key composition rules
All cache entries use a deterministic key that includes:
- `tenant_id`
- `resource` (type + identifier)
- `user_id` (or a stable roles/permissions hash)
- `policy_hash` (deterministic hash of OPA decision input)
- `tenant_epoch` and `policy_epoch` (monotonic version keys)

Example (logical form):
`sc:v1:t:<tenant>:tv:<tenant_epoch>:p:<policy_id>:pv:<policy_epoch>:u:<user>:ph:<policy_hash>:r:<resource>`

Rules:
- `tenant_id` is mandatory in every key.
- User context is mandatory for user-facing data.
- TTL is mandatory for every key.
- Values are serialized as JSON and validated on read; corrupt values are discarded.

## Authorization + policy binding
### Policy hash definition
`policy_hash` is computed as SHA-256 of canonical JSON built from:
- tenant_id
- user_id
- user roles/permissions hash
- action (e.g., `customers:read`)
- resource type (and optionally resource attributes for ABAC)

Canonical JSON:
- keys sorted
- UTF-8 encoding
- no whitespace differences

### Safe evaluation rule
- If the OPA call fails (timeout/network/invalid response) → deny and do not cache.
- Cache is consulted only after computing a deterministic policy context.

## Invalidation strategies
Use key versioning (epochs) so invalidation is immediate and O(1):
- `tenant_epoch(tenant_id)` increments on:
  - tenant suspension
  - tenant-wide permission model changes
  - GDPR forget operations
  - approval decisions that affect allowed actions
- `policy_epoch(policy_id)` increments on:
  - policy/bundle change (deploy or explicit admin action)
  - targeted policy rollouts
- `user_epoch(tenant_id,user_id)` increments on:
  - role change
  - permission updates affecting that user

Cache keys include epochs. When epochs bump, old entries become unreachable without scanning/deleting keys.

## Failure modes and safe defaults
- Redis down: treat as cache miss; authorization must still pass before returning data.
- Corrupt cache entry: discard; recompute after successful authorization.
- Policy mismatch: deny access.

## Observability requirements
Metrics (Prometheus):
- `cache_hit_total`
- `cache_miss_total`
- `cache_invalidation_total`
- `cache_fail_closed_total`
- `auth_recheck_latency_ms` (histogram)

Grafana panels:
- hit ratio per tenant
- invalidation frequency and reasons
- auth latency before/after caching
