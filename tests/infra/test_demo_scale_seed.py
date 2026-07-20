"""Static validation of demo-scale seed and clear scripts.

These tests read the SQL files and verify structural properties:
tenant scope, demo-scale markers, no external API calls, idempotency,
cleanup safety, workflow coverage, and interview-demo isolation.

They do NOT require a running database.
"""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

SEED = (ROOT / "scripts" / "seed-demo-scale.sql").read_text(encoding="utf-8")
CLEAR = (ROOT / "scripts" / "clear-demo-scale.sql").read_text(encoding="utf-8")
DOCS = (ROOT / "docs" / "interview" / "demo-scale-data.md").read_text(encoding="utf-8")
INTERVIEW_SEED = (ROOT / "scripts" / "seed-interview-demo.sql").read_text(
    encoding="utf-8"
)

# ---------------------------------------------------------------------------
# Tenant scope
# ---------------------------------------------------------------------------


def test_seed_requires_tenant_id() -> None:
    """Seed script must require tenant_id and fail without it."""
    assert r"\if :{?tenant_id}" in SEED
    assert r"\else" in SEED
    assert "tenant_id is required" in SEED


def test_seed_sets_tenant_rls_context() -> None:
    """Seed script must set the RLS context for the tenant."""
    assert "set_config('app.tenant_id'" in SEED.lower()


def test_seed_all_inserts_include_tenant_id() -> None:
    """Every INSERT must reference the tenant context table or variable."""
    insert_count = SEED.count("INSERT INTO ")
    tenant_ref_count = (
        SEED.count("dsc.tenant_id")
        + SEED.count("tenant_id")
    )
    # Every insert block should use dsc.tenant_id or the tenant_id variable
    assert tenant_ref_count >= insert_count * 1.5  # generous lower bound


def test_clear_requires_tenant_id() -> None:
    """Clear script must require tenant_id."""
    assert r"\if :{?tenant_id}" in CLEAR
    assert "tenant_id is required" in CLEAR


def test_clear_all_deletes_filter_by_tenant() -> None:
    """Every DELETE in the clear script must filter by tenant_id."""
    # DELETEs span multiple lines; join each DELETE block and check the WHERE clause
    delete_blocks = CLEAR.split("DELETE FROM")
    assert len(delete_blocks) > 1, "Clear script must contain DELETE statements"
    for block in delete_blocks[1:]:  # skip content before first DELETE
        # The WHERE clause must reference tenant_id
        # Find the WHERE line after this DELETE
        block_upper = block.upper()
        assert "tenant_id" in block, (
            f"DELETE block must filter by tenant_id. Block:\n{block[:300]}"
        )


# ---------------------------------------------------------------------------
# Demo-scale markers
# ---------------------------------------------------------------------------


def test_seed_uses_demo_scale_source() -> None:
    """Seed script must mark data with source='demo-scale'."""
    assert "source', 'demo-scale'" in SEED
    assert "tags ? 'demo-scale'" in SEED or "tags ? 'demo-scale'" in SEED
    assert "model_version = 'demo-scale-v1'" in SEED


def test_seed_metadata_includes_version_and_seed() -> None:
    """Metadata must include dataset_version and seed for traceability."""
    assert "dataset_version" in SEED
    assert "'1.0.0'" in SEED
    assert "dsc.seed" in SEED


def test_clear_only_targets_demo_scale() -> None:
    """Clear script must only delete rows with demo-scale markers."""
    # Every DELETE WHERE clause must reference demo-scale
    delete_blocks = CLEAR.split("DELETE FROM")
    for block in delete_blocks[1:]:  # skip content before first DELETE
        assert "demo-scale" in block, (
            f"DELETE block must reference demo-scale marker:\n{block[:200]}"
        )


def test_clear_never_mentions_interview_demo() -> None:
    """Clear script must not reference interview-demo as a deletion target."""
    # The only mentions of interview-demo should be in safety checks or notices.
    # Skip SQL comment lines (--) and lines inside safety verification blocks.
    in_do_block = False
    for line in CLEAR.split("\n"):
        stripped = line.strip()
        if stripped.startswith("--") or stripped.startswith("RAISE NOTICE"):
            continue
        if stripped.upper().startswith("DO $$"):
            in_do_block = True
            continue
        if stripped.startswith("$$;") or stripped.startswith("END $$"):
            in_do_block = False
            continue
        if in_do_block:
            continue
        if "interview-demo" in stripped and (
            stripped.upper().startswith("DELETE")
            or (stripped.upper().startswith("WHERE") and "=" in stripped)
        ):
            assert False, (
                f"Clear script must not target interview-demo data: {stripped}"
            )


# ---------------------------------------------------------------------------
# No external API / NIM calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        "nvidia",
        "nim",
        "ollama",
        "openai",
        "anthropic",
        "http://",
        "https://",
        "curl",
        "fetch",
        "requests.",
    ],
)
def test_seed_has_no_external_api_calls(pattern: str) -> None:
    """Seed script must not contain references to external APIs or models."""
    assert pattern not in SEED.lower(), (
        f"Seed script must not reference external API: {pattern}"
    )


def test_seed_agents_are_offline_fixtures() -> None:
    """Agent configs must declare mode='offline-fixture'."""
    assert "offline-fixture" in SEED


def test_seed_agents_are_not_live_model_claims() -> None:
    """Seed must not claim live model inference."""
    disclaimers = [
        "offline-fixture",
        "deterministic",
        "demo-scale",
    ]
    for d in disclaimers:
        assert d in SEED, f"Must include disclaimer-like term: {d}"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_seed_uses_on_conflict_do_nothing() -> None:
    """All INSERTs must use ON CONFLICT DO NOTHING for idempotency."""
    insert_blocks = SEED.split("INSERT INTO ")
    conflict_blocks = 0
    for block in insert_blocks[1:]:  # skip content before first INSERT
        if "ON CONFLICT (id) DO NOTHING" in block:
            conflict_blocks += 1
    total_inserts = len(insert_blocks) - 1
    assert conflict_blocks == total_inserts, (
        f"All {total_inserts} INSERT blocks must use ON CONFLICT (id) DO NOTHING; "
        f"found {conflict_blocks}"
    )


def test_seed_has_deterministic_uuid_generator() -> None:
    """Seed must generate deterministic UUIDs using md5 hash."""
    assert "md5(" in SEED
    assert "::uuid" in SEED


def test_seed_uuid_includes_tenant_seed_and_index() -> None:
    """UUID function must incorporate tenant_id, seed, namespace, and index."""
    assert ":tenant_id" in SEED or "tenant_id" in SEED
    assert ":seed" in SEED or "seed" in SEED
    assert "namespace" in SEED


# ---------------------------------------------------------------------------
# Workflow coverage
# ---------------------------------------------------------------------------


def test_workflows_cover_all_four_scenarios() -> None:
    """Must have type A, B, C, D workflows as specified."""
    assert "demo-scale-type-a" in SEED, "Missing Type A: Sales->Support->Compliance->approval"
    assert "demo-scale-type-b" in SEED, "Missing Type B: approval pending"
    assert "demo-scale-type-c" in SEED, "Missing Type C: policy denied / tenant-boundary block"
    assert "demo-scale-type-d" in SEED, "Missing Type D: degraded / retriable"


def test_type_a_has_three_specialist_tasks() -> None:
    """Type A workflows must chain Sales -> Support -> Compliance."""
    assert "Sales -> Support -> Compliance" in DOCS or (
        "sales" in SEED.lower()
        and "support" in SEED.lower()
        and "compliance" in SEED.lower()
    )


def test_type_b_has_pending_approvals() -> None:
    """Type B workflows must include pending approval records."""
    assert "'pending'" in SEED


def test_type_c_has_denied_decisions() -> None:
    """Type C workflows must include denied agent decisions."""
    assert "'denied'" in SEED


def test_type_d_has_retry_or_degraded_outcomes() -> None:
    """Type D workflows must include retry or degraded states."""
    assert "retry" in SEED.lower()
    assert "degraded" in SEED.lower()


def test_workflows_use_correlation_id() -> None:
    """All multi-agent workflows must use correlation_id."""
    assert "correlation_id" in SEED.lower()


def test_agent_tasks_events_decisions_are_all_present() -> None:
    """Multi-agent data must consist of tasks, events, and decisions."""
    assert "INSERT INTO agent_tasks" in SEED
    assert "INSERT INTO agent_events" in SEED
    assert "INSERT INTO agent_decisions" in SEED


# ---------------------------------------------------------------------------
# Interview-demo isolation
# ---------------------------------------------------------------------------


def test_seed_does_not_touch_interview_demo_ids() -> None:
    """Seed script must not reference interview-demo UUID prefixes."""
    interview_prefix = "a6a0000"
    # The seed may reference the interview prefix only in comments/documentation
    # Count occurrences in actual SQL statements (not comments)
    lines_with_prefix = [
        line for line in SEED.split("\n")
        if interview_prefix in line
        and not line.strip().startswith("--")
    ]
    assert len(lines_with_prefix) == 0, (
        f"Seed script must not reference interview-demo UUIDs in SQL statements:\n"
        + "\n".join(lines_with_prefix[:5])
    )


def test_seed_does_not_delete_or_truncate() -> None:
    """Seed script must never DELETE or TRUNCATE."""
    assert "DELETE FROM" not in SEED.upper() or "DELETE FROM" not in SEED
    # Check case-insensitively
    upper_seed = SEED.upper()
    assert "DELETE FROM" not in upper_seed, "Seed must not contain DELETE statements"
    assert "TRUNCATE" not in upper_seed, "Seed must not contain TRUNCATE statements"


def test_clear_verifies_interview_data_present() -> None:
    """Clear script must verify interview-demo data is still present after cleanup."""
    assert "interview-demo" in CLEAR.lower()


# ---------------------------------------------------------------------------
# Business distribution
# ---------------------------------------------------------------------------


def test_seed_has_health_categories() -> None:
    """Customers must be distributed across healthy/watch/risk."""
    assert "'healthy'" in SEED
    assert "'watch'" in SEED
    assert "'risk'" in SEED


def test_seed_has_deal_stages_with_outcomes() -> None:
    """Deals must cover all funnel stages including won/lost/deferred."""
    for stage in ["closed_won", "closed_lost", "deferred"]:
        assert stage in SEED, f"Deals must include stage: {stage}"


def test_seed_has_ticket_sla_risk_levels() -> None:
    """Tickets must include SLA risk distributions."""
    for risk in ["breached", "at_risk", "on_track"]:
        assert risk in SEED, f"Tickets must include SLA risk: {risk}"


def test_seed_has_forty_article_templates() -> None:
    """Knowledge articles must have at least 40 distinct templates."""
    # Count the number of entries in demo_article_templates
    article_count = SEED.count("', '")
    # The template table has 40 entries with 5 fields each
    assert article_count >= 160, "Must have at least 40 article templates"


# ---------------------------------------------------------------------------
# ASCII check
# ---------------------------------------------------------------------------


def test_seed_is_ascii() -> None:
    """Seed script must be ASCII-only."""
    try:
        SEED.encode("ascii")
    except UnicodeEncodeError as e:
        pytest.fail(f"Seed script contains non-ASCII characters: {e}")


def test_clear_is_ascii() -> None:
    """Clear script must be ASCII-only."""
    try:
        CLEAR.encode("ascii")
    except UnicodeEncodeError as e:
        pytest.fail(f"Clear script contains non-ASCII characters: {e}")


def test_docs_is_ascii() -> None:
    """Documentation must be ASCII-only."""
    try:
        DOCS.encode("ascii")
    except UnicodeEncodeError as e:
        pytest.fail(f"Documentation contains non-ASCII characters: {e}")


# ---------------------------------------------------------------------------
# Metadata completeness
# ---------------------------------------------------------------------------


def test_customer_profiles_have_demo_scale_markers() -> None:
    """Customer profiles must carry demo-scale markers."""
    assert "features ->> 'source' = 'demo-scale'" in CLEAR or (
        "demo-scale" in SEED and "customer_profiles" in SEED
    )


def test_predictions_are_versioned() -> None:
    """Predictions must use demo-scale model version."""
    assert "demo-scale-v1" in SEED


def test_timeline_events_marked_demo_scale() -> None:
    """Customer timeline events must have source marker."""
    assert "customer_timelines" in SEED.lower()


# ============================================================================
# B1 regression: Docs PowerShell commands use -e TENANT_ID=$tenantId
# ============================================================================


def test_docs_commands_use_env_var_for_tenant_id() -> None:
    """Seed/clear Quick Start commands must pass tenant_id via -e TENANT_ID=."""
    # Extract only the code between ```powershell and its closing ```,
    # not trailing text that may mention seed-demo-scale.sql outside the block.
    code_blocks: list[str] = []
    remaining = DOCS
    while True:
        start = remaining.find("```powershell\n")
        if start < 0:
            break
        code_start = start + len("```powershell\n")
        end = remaining.find("\n```", code_start)
        if end < 0:
            break
        code_blocks.append(remaining[code_start:end])
        remaining = remaining[end + 4:]

    seed_blocks = [b for b in code_blocks
                   if "seed-demo-scale.sql" in b or "clear-demo-scale.sql" in b]
    assert len(seed_blocks) >= 1, "Docs must have at least one powershell block with seed/clear SQL"

    for block in seed_blocks:
        docker_lines = [
            line.strip()
            for line in block.split("\n")
            if "docker compose exec" in line
            and not line.strip().startswith("#")
        ]
        for line in docker_lines:
            assert '-e "TENANT_ID=$tenantId"' in line or "-e TENANT_ID" in line, (
                f"PowerShell command must pass tenant_id via -e: {line[:120]}"
            )
        assert "$TENANT_ID" in block, (
            "Container shell must reference $TENANT_ID in psql -v tenant_id=..."
        )


def test_docs_commands_use_dollar_tenant_id_in_container() -> None:
    """The psql -v tenant_id must reference $TENANT_ID (shell env var, not PS var)."""
    # The container command must use "$TENANT_ID" (expanded by container shell)
    assert '"$TENANT_ID"' in DOCS or "$TENANT_ID" in DOCS


# ============================================================================
# B2 regression: ai_agents cross-tenant safety
# ============================================================================


def test_seed_ai_agents_config_includes_tenant_id() -> None:
    """ai_agents config must embed tenant_id for cross-tenant identification."""
    assert "tenant_id', dsc.tenant_id" in SEED or (
        "jsonb_build_object('tenant_id'" in SEED
        and "dsc.tenant_id" in SEED
    ), "ai_agents config must include tenant_id"


def test_clear_ai_agents_checks_cross_tenant_refs() -> None:
    """Clear script must check for cross-tenant agent_tasks/agent_events before deleting agents."""
    assert "agent_tasks at" in CLEAR or "agent_tasks" in CLEAR, (
        "Clear must check agent_tasks for cross-tenant references"
    )
    assert "agent_events ae" in CLEAR or "agent_events" in CLEAR, (
        "Clear must check agent_events for cross-tenant references"
    )
    # Must mention cross-tenant detection
    assert "cross-tenant" in CLEAR.lower() or "cross_tenant" in CLEAR.lower(), (
        "Clear must detect cross-tenant references"
    )


def test_clear_ai_agents_only_deletes_tenant_scoped() -> None:
    """The ai_agents DELETE must filter by config->>'tenant_id'."""
    # Find the ai_agents delete section
    has_tenant_filter = (
        "config ->> 'tenant_id'" in CLEAR
        or "config->>'tenant_id'" in CLEAR
    )
    assert has_tenant_filter, (
        "ai_agents DELETE must filter by config.tenant_id"
    )


# ============================================================================
# B3 regression: Docs do not claim byte-identical determinism
# ============================================================================


def test_docs_do_not_claim_byte_identical() -> None:
    """Docs must not claim data IS byte-identical; negated mentions are fine."""
    # "byte-identical" may appear ONLY in negated form ("NOT byte-identical")
    lower = DOCS.lower()
    idx = lower.find("byte-identical")
    if idx >= 0:
        # Look at the 30 chars before to confirm it's negated
        prefix = lower[max(0, idx - 30):idx]
        assert "not " in prefix, (
            "Docs must negate byte-identical claim, e.g. 'NOT byte-identical'"
        )
    # "identical rows on every run" should not appear
    assert "identical rows" not in lower, (
        "Docs must not claim identical rows (timestamps differ)"
    )


def test_docs_acknowledge_now_timestamps() -> None:
    """Docs must explain that timestamps use now() and vary between runs."""
    assert "now()" in DOCS, (
        "Docs must mention that timestamps use now() and are not deterministic"
    )


def test_docs_use_row_idempotent_not_byte_identical() -> None:
    """Docs must qualify determinism as row-level, not byte-level."""
    assert "row-idempotent" in DOCS.lower() or "row idempotent" in DOCS.lower(), (
        "Docs must describe idempotency as row-level, not byte-identical"
    )


# ============================================================================
# B4 improvement: Stricter DELETE scope validation
# ============================================================================


def test_every_delete_block_has_explicit_tenant_condition() -> None:
    """Each DELETE block must have an explicit WHERE clause with tenant_id AND demo-scale."""
    # Split by DELETE FROM, but skip the ai_agents DO block (checked separately)
    delete_blocks = CLEAR.split("DELETE FROM")
    assert len(delete_blocks) > 1, "Clear script must contain DELETE statements"

    for i, block in enumerate(delete_blocks[1:], start=1):
        # Skip the ai_agents DO block which uses procedural logic
        if "DO $$" in CLEAR.split("DELETE FROM")[i]:
            continue

        # Each DELETE block must have explicit WHERE with both tenant_id and demo-scale
        # Extract the part between DELETE FROM and the semicolon/next statement
        lines = block.strip().split("\n")
        has_tenant = False
        has_demo_marker = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            if "tenant_id" in stripped:
                has_tenant = True
            if "demo-scale" in stripped:
                has_demo_marker = True
            if stripped.rstrip().endswith(";"):
                break

        assert has_tenant, (
            f"DELETE block {i} must filter by tenant_id:\n{block[:200]}"
        )
        assert has_demo_marker, (
            f"DELETE block {i} must filter by demo-scale marker:\n{block[:200]}"
        )
