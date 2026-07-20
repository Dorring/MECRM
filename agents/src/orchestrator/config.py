"""
Configuration settings for the AI Agent Layer.
"""

import os
from typing import List

# dotenv MUST load before Settings() constructs so that .env overrides
# are visible to every module that imports `settings`.
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Application settings loaded from environment."""

    # Kafka
    KAFKA_BROKERS: str = os.getenv("KAFKA_BROKERS", "localhost:9094")
    KAFKA_GROUP_ID: str = os.getenv("KAFKA_GROUP_ID", "ai-agents")

    # Topics to consume
    CONSUME_TOPICS: List[str] = [
        "crm.leads.created",
        "crm.leads.updated",
        "crm.deals.created",
        "crm.deals.updated",
        "crm.deals.stage-changed",
        "crm.deals.closed",
        "crm.tickets.created",
        "crm.tickets.updated",
        "crm.tickets.resolved",
        "crm.tickets.sla-breached",
        "crm.conversations.closed",
        "crm.customers.created",
        "crm.customers.updated",
        "crm.tasks.updated",
        "crm.user.activity",
        "crm.productivity.signal",
        "crm.journey.updated",
        "crm.analytics.prediction-generated",
        "crm.analytics.forecast-requested",
        "crm.approvals.decision",
        "crm.payments.recorded",
        "crm.automation.simulation.requested",
        "crm.knowledge.published",
    ]

    # AI runtime mode. Controls whether models are initialised at all.
    #   disabled      — no model init, no embedding init, no network access
    #   deterministic — local deterministic provider, no network, repeatable output
    #   live          — real model via AI_PROVIDER (requires explicit config)
    AI_MODE: str = os.getenv("AI_MODE", "deterministic").strip().lower()

    # Agent orchestration mode (Phase 5+). Independent of AI_MODE.
    #   legacy     — existing AgentRouter behaviour
    #   shadow     — legacy path active, supervisor path runs in parallel
    #   supervisor — only supervisor graph path active
    AGENT_ORCHESTRATION_MODE: str = (
        os.getenv("AGENT_ORCHESTRATION_MODE", "legacy").strip().lower()
    )

    # AI inference provider. MUST be set explicitly when AI_MODE=live.
    # No default — an empty value with AI_MODE=live will raise a config error.
    # Only consulted when AI_MODE=live.
    AI_PROVIDER: str = os.getenv("AI_PROVIDER", "").strip().lower()
    AI_REQUEST_TIMEOUT_SECONDS: float = float(
        os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "30")
    )
    AI_MAX_RETRIES: int = int(os.getenv("AI_MAX_RETRIES", "2"))

    # Ollama (optional local LLM / embeddings)
    OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.1")
    OLLAMA_EMBED_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

    # NVIDIA NIM (optional managed LLM / embeddings). The API key is consumed
    # exclusively by the agents service and must never be exposed to the UI.
    NVIDIA_BASE_URL: str = os.getenv(
        "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
    )
    NVIDIA_API_KEY: str = os.getenv("NVIDIA_API_KEY", "")
    NVIDIA_CHAT_MODEL: str = os.getenv("NVIDIA_CHAT_MODEL", "")
    NVIDIA_EMBED_MODEL: str = os.getenv("NVIDIA_EMBED_MODEL", "")

    # Weaviate (Vector Store)
    WEAVIATE_URL: str = os.getenv("WEAVIATE_URL", "http://localhost:8082")

    # OPA (Policy Engine)
    OPA_URL: str = os.getenv("OPA_URL", "http://localhost:8181")

    # Database
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql://localhost:5432/enterprise_crm"
    )

    # Gateway (CRM HTTP API)
    GATEWAY_URL: str = os.getenv("GATEWAY_URL", "http://localhost:4000")

    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379")

    # Health check
    HEALTH_PORT: int = int(os.getenv("AGENTS_PORT", "5010"))

    # Agent settings
    DEFAULT_CONFIDENCE_THRESHOLD: float = 0.7
    HIGH_RISK_CONFIDENCE_THRESHOLD: float = 0.9
    MAX_RETRIES: int = 3
    TASK_TIMEOUT_SECONDS: int = 300

    # DLQ retry policy (P0-8): when the DLQ send itself fails on a transient
    # broker outage, retry with exponential backoff before advancing the
    # offset, so a recoverable message is not silently dropped.
    DLQ_MAX_RETRIES: int = int(os.getenv("DLQ_MAX_RETRIES", "3"))
    DLQ_RETRY_BACKOFF_SECONDS: float = float(
        os.getenv("DLQ_RETRY_BACKOFF_SECONDS", "1.0")
    )


settings = Settings()
