# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-Agent Enterprise CRM -- an AI-native CRM where LangGraph/Ollama agents collaborate with humans across sales, support, and compliance workflows. Built on event-driven CQRS with Kafka, multi-tenant RLS isolation, and an AI governance framework (kill switch, explainability, approval workflows).

## Tech Stack

- **Frontend:** Next.js 14 (App Router), React 18, Tailwind CSS, Zustand, TanStack Query
- **API Gateway:** Express.js, TypeScript, Prisma ORM, KafkaJS, ioredis, Helmet
- **AI Agents:** Python 3.11+, LangGraph, LangChain, Ollama (Llama 3.1), FastAPI, asyncpg
- **Infrastructure:** PostgreSQL 16 (RLS), Redis 7, Apache Kafka (KRaft), Keycloak, OPA/Rego, Weaviate, Docker Compose, Prometheus/Grafana/Loki

## Build & Run Commands

### Full Stack (Docker)
```bash
cp .env.example .env
docker-compose up -d
docker-compose exec gateway npx prisma migrate deploy
docker-compose exec postgres psql -U crm_user -d enterprise_crm -f /docker-entrypoint-initdb.d/02-rls-policies.sql
```

### Local Development (three terminals)
```bash
# Terminal 1 - Gateway (port 4000)
cd gateway && npm install && npm run dev

# Terminal 2 - Frontend (port 3000)
cd frontend && npm install && npm run dev

# Terminal 3 - Agents (port 5010)
cd agents && pip install -r requirements.txt && python -m src.orchestrator.main
```

### Build
```bash
cd frontend && npm run build          # Next.js production build
cd gateway && npm run build           # prisma generate && tsc
```

### Lint
```bash
cd frontend && npm run lint           # ESLint (next/core-web-vitals)
cd gateway && npm run lint            # ESLint (TypeScript rules)
```

### Test
```bash
cd gateway && npm test                                # Jest unit/integration
cd agents && pytest tests -v                          # pytest (asyncio_mode=auto)
pytest tests/test_gdpr_forget.py tests/test_data_export.py -v   # GDPR integration
CHAOS_TESTS_ENABLED=true CHAOS_ENVIRONMENT=local pytest agents/tests/chaos -v   # chaos
python .agent/scripts/checklist.py .                  # validation checks
```

### Prisma (Gateway)
```bash
cd gateway && npx prisma generate     # regenerate client after schema change
cd gateway && npx prisma migrate dev  # create new migration
cd gateway && npm run seed            # seed database
```

## Architecture

### Layers

**Frontend** (`frontend/src/`) -- Next.js App Router. Root layout provides sidebar, header, persistent chat panel. All API calls go through `frontend/src/lib/api.ts` (typed `ApiClient` with auth injection). Connects to gateway via REST (`/api/v1/*`) and WebSocket (`/ws`).

**API Gateway** (`gateway/src/`) -- Express middleware stack on every request: Helmet -> CORS -> rate limit -> correlation ID -> request logging -> `authMiddleware` (JWT) -> `tenantMiddleware` (sets PostgreSQL RLS context via `SET LOCAL app.tenant_id`) -> `opaMiddleware` (policy check) -> `auditMiddleware`. Exposes 20+ route modules. Runs 11 Kafka consumers for event-driven read model updates.

**AI Agents** (`agents/src/`) -- Orchestrator (`orchestrator/main.py`) consumes Kafka events, routes to specialized agents via `AgentRouter`. Kill switch integration pauses/resumes Kafka partitions per tenant. Core agents: sales, support, compliance, analytics. Intelligence modules under `agents/src/intelligence/`: search, chat, automation, compliance, knowledge, journey, productivity, predictions, twins, DevX, i18n, governance.

**Core Services** (`core_services/src/`) -- Shared Python services: secure caching, DR (backup/restore), governance (data erasure, export, retention, PII registry), event store, transactional outbox, circuit breaker/retry.

**Event Backbone** -- Kafka (KRaft mode). Gateway publishes via transactional outbox pattern (`core_services/src/write/outbox.py` + `services/src/outbox_publisher.py`). Event schemas versioned in `schemas/events/`. Exactly-once semantics via idempotent processing and manual offset commits.

**Data Layer** -- PostgreSQL with RLS for multi-tenant isolation. Prisma schema in `gateway/prisma/schema.prisma` (30+ models). SQL migrations in `database/migrations/`. OPA policies in `policies/*.rego` gate RBAC/ABAC/tenant actions.

### Cross-Cutting Concerns

- **Multi-tenancy:** enforced at DB (RLS), middleware (context propagation), OPA (tenant policies), agents (partition pausing)
- **AI Governance:** kill switch (global/tenant/agent scope), explainability engine, human approval workflows, agent telemetry
- **GDPR:** data erasure, export, retention policies, PII classification
- **Observability:** OpenTelemetry traces, Prometheus metrics, Grafana dashboards, Loki logs

### Key Ports

| Service | Port |
|---|---|
| Frontend | 3000 |
| Gateway | 4000 |
| Kafka UI | 8080 |
| Grafana | 3001 |
| Keycloak | 8081 |
| Weaviate | 8082 |
| OPA | 8181 |
| Prometheus | 9090 |

## Coding Conventions

### TypeScript (Frontend + Gateway)
- ESLint configs: `gateway/.eslintrc.js`, `frontend/.eslintrc.json`
- 2-space indent, single quotes, explicit exports
- Prefer TypeScript; avoid unused vars (underscore-prefix unused args)
- PascalCase for React components, camelCase for vars/functions

### Python (Agents + Core Services)
- PEP 8, 4-space indent, type hints
- Structured logging via `structlog`
- Test files follow `test_<module>_<condition>_<expected>()` naming

### General
- kebab-case for directories
- SCREAMING_SNAKE_CASE for environment variables
- Never commit secrets; use `.env` (copy from `.env.example`)
- RLS migrations must run after `docker-compose up`
- OPA policy changes require corresponding doc updates in `policies/`

## CI/CD

Workflows in `.github/workflows/`:
- `ci-cd.yml` -- main pipeline
- `chaos-tests.yml` -- chaos engineering suite
- `tenant-isolation.yml` -- RLS verification

## Agent Tooling

The `.agent/` directory contains an "Antigravity Kit" with 20 specialist agent personas, 36 skill modules, and 11 slash-command workflows for AI coding assistants. Validation scripts: `python .agent/scripts/checklist.py .` (quick) and `python .agent/scripts/verify_all.py . --url http://localhost:3000` (full).
