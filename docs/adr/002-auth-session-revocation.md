# ADR-002: Tenant-Scoped Token and Session Revocation

**Status:** Implementing — Group B hardening in progress  
**Date:** 2026-07-05  
**Scope:** Hardening 1.1 Group B  
**Supersedes:** `blacklist:<full-jwt>` revocation keys

## 1. Context

The Gateway currently stores complete JWT strings in Redis, HTTP and WebSocket
authentication use different revocation paths, refresh rotation is not atomic,
and an established WebSocket is not closed when its token is revoked.

Group B must provide one revocation contract for HTTP, refresh, logout and
WebSocket paths without expanding into Group C (HttpOnly cookies, CSRF,
frontend token storage, or runtime frontend URLs).

## 2. Security invariants

The implementation is acceptable only if all of the following remain true:

1. A complete access or refresh token is never stored in Redis or written to
   logs, metrics, traces or error responses.
2. Every access and refresh token has a tenant-scoped `jti`, `sid`, token type,
   user revocation version and absolute session expiry.
3. Revocation checks fail closed. A Redis command error is not equivalent to a
   cache miss.
4. Refresh tokens are single-use across all Gateway instances.
5. Refresh-token replay revokes the entire session family.
6. User-wide revocation rejects old sessions but allows a new login.
7. A revoked session cannot become valid again after Redis restart, failover or
   memory pressure.
8. HTTP and WebSocket paths use the same injected revocation service.
9. Revocation of an established WebSocket propagates across Gateway instances.
10. No implementation uses Redis `KEYS` or `SCAN` to perform revocation.

## 3. JWT contract

Both access and refresh tokens contain:

| Claim | Type | Meaning |
|---|---|---|
| `jti` | UUID string | Unique token identifier generated with `crypto.randomUUID()` |
| `sid` | UUID string | Login-session/token-family identifier |
| `sub` | UUID string | User identifier |
| `tenantId` | UUID string | Tenant boundary |
| `type` | `access` or `refresh` | Prevents token-type confusion |
| `uv` | non-negative integer | User revocation generation at session creation |
| `sexp` | Unix seconds | Absolute session expiry; refresh cannot extend past it |
| `iat` / `exp` | Unix seconds | Standard issuance and token expiry |

Access tokens may additionally contain email and roles. Refresh tokens should
contain only claims required for refresh and revocation.

JWT verification must pin the allowed algorithm and validate all required
claims before constructing a Redis key. Refresh tokens are never accepted by
access middleware.

`sid`, `uv` and `sexp` are fixed for the lifetime of a session. Refresh rotation
creates a new `jti` but preserves those three claims. A refresh token expiry is
`min(now + configuredRefreshTtl, sexp)`.

## 4. Redis contract

All keys include the tenant boundary:

| Key | Value | TTL |
|---|---|---|
| `auth:{tenantId}:revoked:jti:{jti}` | `1` | Until token `exp` plus skew |
| `auth:{tenantId}:revoked:sid:{sid}` | `1` | Until `sexp` plus skew |
| `auth:{tenantId}:refresh:consumed:{jti}` | `1` | Until refresh `exp` plus skew |
| `auth:{tenantId}:user:{userId}:version` | integer | No TTL |

Braces above describe fields; implementations must safely validate identifiers
before interpolation. Redis Cluster hash tags may be added deliberately, but
must not remove the tenant boundary.

Token/session TTL is:

```text
max(1, min(configuredMaximum, expiry - now + clockSkew))
```

An already expired token is not written as a new revocation record.

### 4.1 User-wide revocation

User-wide revocation atomically increments the tenant/user version key. Login
reads the current version (missing means zero) and embeds it as `uv`. A token is
valid only when its `uv` equals the current version.

This avoids timestamp precision races and ensures a new login after revocation
works immediately. The version key must never be evicted or expire.

### 4.2 Atomic refresh consumption

Refresh rotation must be implemented as one Lua script (or an equivalent
single Redis transaction with identical semantics). The operation:

1. Checks jti revocation.
2. Checks sid revocation.
3. Checks that token `uv` equals the current user version.
4. Checks whether the refresh `jti` was already consumed.
5. If unused, writes the consumed key with `NX` and an expiry.
6. If already consumed, writes the sid-revoked key until `sexp` and returns
   `REPLAY`.

Only an `OK` result may mint a new token pair. `REPLAY` rejects the request and
publishes a session-revocation event. Any Redis/Lua error returns dependency
unavailable and mints no token.

This operation is the concurrency boundary. A preceding `GET` followed by a
later `SET` is not acceptable.

### 4.3 Pipeline error handling

Normal access checks may pipeline jti, sid and user-version reads. The service
must reject:

- a null pipeline result;
- an unexpected result count;
- any per-command tuple containing an error;
- malformed user-version values.

Only successful commands returning absent revocation keys and a matching user
version constitute an allow decision.

## 5. Redis durability and memory policy

Revocation and user-version data are security session state, not disposable
cache entries.

Local Compose must enable:

- AOF persistence;
- `appendfsync always` for deterministic security tests;
- `maxmemory-policy noeviction`;
- a persistent Redis volume.

Production Redis must provide equivalent or stronger durability and HA. It must
reject writes rather than evict revocation/version keys. For replicated Redis,
production configuration must define an acknowledged-write policy such as
minimum replicas and maximum replication lag.

Gateway readiness must fail when the command Redis connection cannot provide
the revocation contract. Redis write/OOM/persistence errors fail closed.

A production deployment is blocked if its managed Redis cannot guarantee these
properties. Configuration and restart tests are part of Group B because
otherwise a revoked token can become valid again.

## 6. Service construction and request lifecycle

There is no mutable module-global singleton.

At Gateway startup:

1. Construct the normal Redis command client.
2. Construct a dedicated subscriber with `duplicate()`.
3. Construct one `TokenRevocationService` with explicit dependencies.
4. Construct HTTP middleware/routes and WebSocket handling with that service.
5. Subscribe to revocation events.
6. Mark readiness only after the command connection is usable and subscription
   state is known.

At shutdown, stop accepting traffic, close WebSockets, unsubscribe, then close
both Redis connections.

HTTP middleware lifecycle:

```text
parse Authorization
→ verify JWT signature/algorithm/expiry
→ validate complete claim schema and type=access
→ check tenant-scoped revocation state
→ attach immutable auth context
→ route handler
```

Redis unavailable or an indeterminate check returns HTTP `503
AUTH_DEPENDENCY_UNAVAILABLE`. A known invalid/revoked token returns `401`.
Fail-closed means the request is denied; it does not require misclassifying an
infrastructure outage as invalid credentials.

## 7. Login, refresh and logout

### 7.1 Login/register

Login obtains the current tenant/user generation from Redis, generates one
`sid`, fixes `sexp`, and signs the access/refresh pair. Redis failure returns
503 and no tokens. `sid` is not added as a separate response field.

### 7.2 Refresh

The route verifies signature, algorithm, type and all claims, verifies the user
is active, then atomically consumes the refresh token. It mints a new pair only
after the atomic operation returns `OK`.

Replay returns 401 and revokes the full sid. Redis failure returns 503.

### 7.3 Logout

Logout uses verified authentication context; it never trusts `jwt.decode()`.
If a refresh token is supplied, it must also be fully verified and must match
the access token's tenant, user and sid.

Logout revokes the sid until `sexp`, writes any required jti revocation, closes
local matching WebSockets, and publishes the event. Repeating a successful
logout is idempotent. Redis failure returns 503 because revocation was not
durably recorded.

## 8. WebSocket contract

The current query-string token transport remains a documented Group C risk; it
must not be expanded or logged in Group B.

At connect and subscribe, the Gateway verifies claims and checks revocation
using the same service as HTTP. An invalid/revoked token closes with `4401`.
A cross-tenant subscription closes with `4403`. Redis unavailability closes
with standard code `1013` and a non-sensitive reason.

Each local socket stores only validated tenant ID, user ID, jti, sid, uv and
sexp. Socket maps are cleaned on close/error.

### 8.1 Cross-instance events

Channel: `auth:revocation:events`

Every event has a runtime-validated schema and mandatory:

```json
{
  "version": 1,
  "type": "jti|sid|user",
  "tenantId": "uuid",
  "id": "uuid-or-user-id",
  "occurredAt": 0
}
```

The instance performing revocation closes its local matching sockets
synchronously after the Redis state write. Pub/Sub is only cross-instance
notification and is not the source of truth. Publish failure is logged and
metered; it is never silently swallowed.

Subscriber messages are size-limited and schema-validated before use.
Subscriber disconnect/reconnect state is observable.

### 8.2 Heartbeat safety net

Pub/Sub is lossy, so every active socket is revalidated at least every 30
seconds. Revalidation:

- uses a batch pipeline or bounded concurrency;
- checks every per-command error;
- prevents overlapping heartbeat runs;
- closes affected sockets on revocation;
- closes checked sockets with 1013 if Redis state is indeterminate;
- catches all async rejections.

Thus a missed Pub/Sub event is bounded by subscriber recovery plus one
heartbeat interval.

## 9. Redis outage and recovery

| Path | Redis unavailable |
|---|---|
| HTTP protected request | 503; request denied |
| Login/register | 503; no token issued |
| Refresh | 503; no token issued or rotated |
| Logout | 503; do not claim success |
| WS connect/subscribe | close 1013 |
| Established WS heartbeat | close 1013 |
| Subscriber only unavailable | readiness degraded; command checks and heartbeat remain authoritative |

Recovery does not bypass revocation. Tests must cover stop/start and restart
with persisted revocation state.

## 10. Migration and rollback

Group B chooses forced reauthentication rather than storing full JWTs during a
dual-write window:

1. New tokens contain the new claims.
2. Tokens without required claims are rejected in production.
3. `AUTH_ACCEPT_LEGACY_TOKENS` may exist for isolated development tests only.
   Production startup/config validation rejects it when true.
4. Existing users reauthenticate at rollout.
5. Old `blacklist:<full-jwt>` keys expire naturally; no scan is performed.

Rolling back to code that only understands the old blacklist would make new
revocations invisible. Therefore rollback requires rotating the JWT signing
secret and forcing reauthentication. This is disruptive but preserves the
security boundary.

## 11. Observability

Add structured counters/gauges without token, jti or sid values:

- revocation checks by result/reason;
- refresh consume outcomes (`ok`, `replay`, `revoked`, `dependency_error`);
- WebSockets closed by revocation scope;
- subscriber connected/reconnect/error;
- heartbeat duration, overlap prevented and failure count;
- Redis revocation write failures.

Logs may include tenant-safe correlation IDs but not raw tokens or token
identifiers.

## 12. Consequences

The design adds one durable Redis command dependency and one subscriber
connection per Gateway instance. Normal authorization adds one pipelined Redis
round trip. Refresh becomes atomic across instances. Pub/Sub accelerates active
WebSocket closure while Redis keys and heartbeat remain authoritative.

The durable user-generation key means Redis contains bounded session state for
users who have been globally revoked. Capacity and backup policy must account
for it.

## 13. Approval gates

Implementation may begin only after reviewers accept:

- Redis durability/no-eviction deployment contract;
- atomic refresh Lua semantics;
- forced reauthentication migration;
- 503 dependency semantics;
- two-instance WebSocket and Redis-restart test design.

