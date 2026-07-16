# AI Evaluation Baselines

This directory contains versioned, reproducible evaluation data and runners for
the AI-facing parts of the CRM.

## H2 structured retrieval baseline

`run_structured_retrieval_eval.py` seeds a temporary two-tenant corpus into the
real PostgreSQL database, calls the production
`HybridRetriever.structured_search` path using the runtime database role, and
removes the corpus when it finishes.

It reports:

- `recall_at_5`: fraction of expected synthetic records returned per positive
  case, averaged across cases.
- `precision_at_5`: fraction of returned records that are expected for each
  positive case, averaged across cases.
- `case_pass_rate`: cases that returned all expected IDs (or no result for a
  negative cross-tenant case).
- `cross_tenant_denial_pass_rate`: negative cases where a tenant cannot read a
  record seeded only for the other tenant.
- `tenant_leak_count`: returned records whose tenant differs from the query
  tenant. This is a hard gate and must remain zero.

This is intentionally **not** a semantic-retrieval or LLM-answer-quality
benchmark. The default stack does not start Ollama or require model downloads,
so vector/answer evaluations are expanded only when a deterministic provider or
explicit live-model environment is available.

## Run in CI-compatible environment

The GitHub Actions workflow provisions Postgres and runs migrations first. To
run manually in an environment with the agents dependencies installed:

```bash
PYTHONPATH=agents/src python evals/run_structured_retrieval_eval.py \
  --output reports/ai-evals/structured-retrieval.json
```

The report records the evaluator version, commit, dataset file names,
thresholds, metrics, and per-case result identifiers. It never records database
URLs or secrets.
