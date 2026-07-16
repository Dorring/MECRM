# H2-5 Preflight: AI Evaluation Expansion

Date: 2026-07-16

Baseline: `main@94087fa`

Status: Implementation preflight.

## Objective

Turn the H2 structured-retrieval baseline into an interview-ready evaluation
artifact with repeatable metrics, explicit safety gates, and a concise report.
The evaluation must run in CI without Ollama, NVIDIA credentials, or a model
download.

## Current evidence

The existing `evals/run_structured_retrieval_eval.py` uses real PostgreSQL,
the runtime database role, RLS, and `HybridRetriever.structured_search`. It
currently has 10 synthetic cases and reports Recall@5, Precision@5, pass rate,
and tenant leakage. This is valid retrieval evidence, but it is not an LLM
quality benchmark.

## H2-5 implementation slices

### Slice A: Retrieval evaluation expansion

- Expand the real PostgreSQL/RLS dataset from 10 to at least 35 cases.
- Retain deterministic, exact-match queries so CI gates are stable.
- Include positive matches, multi-entity matches, tenant denial, empty-result,
  and limit behavior cases.
- Preserve `tenant_leak_count == 0` as a hard gate.

### Slice B: Deterministic AI safety contracts

- Add versioned fixtures for route selection, citation/evidence shape,
  prompt-injection blocking, and dependency degradation.
- Score the fixtures with provider-free evaluators.
- Mark these metrics as contract coverage, not live-model quality.
- Treat tenant leaks, unsafe execution, malformed output, and missed injection
  blocks as hard failures.

### Slice C: Evidence artifact

- Emit a single JSON report and concise Markdown summary.
- Include commit, dataset version, evaluator version, thresholds, elapsed time,
  categories, and pass/fail status.
- Upload both files from GitHub Actions.

## Metric policy

| Metric | Initial target | Gate |
| --- | ---: | --- |
| Structured output pass rate | 1.00 | hard |
| Tenant leak count | 0 | hard |
| Unsafe execution count | 0 | hard |
| Prompt injection block rate | 1.00 | hard |
| Retrieval Recall@5 | 1.00 | hard for synthetic corpus |
| Tool routing accuracy | 0.90 | report-only until runtime routing fixture exists |
| Citation coverage | 0.90 | report-only until grounded-answer fixture exists |

## Boundaries

- Real retrieval metrics must continue to use PostgreSQL and RLS in CI.
- Deterministic safety fixtures cannot claim semantic answer quality.
- NVIDIA NIM remains an opt-in runtime provider; no evaluation calls it.
- Evaluation data stays synthetic and is removed after each CI run.
- No seed/reset command is added for normal application use.

## Acceptance

1. At least 35 versioned cases are validated before database setup.
2. CI produces JSON and Markdown artifacts for every evaluation run.
3. Reports distinguish `real_structured_retrieval` from
   `deterministic_safety_contract` results.
4. A pull request fails on leakage, unsafe execution, malformed output, missed
   injection block, or synthetic retrieval regression.
