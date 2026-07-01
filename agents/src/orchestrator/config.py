"""
Configuration settings for the AI Agent Layer.
"""

import os
from typing import List


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
    
    # Ollama (LLM)
    OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.1")
    
    # Weaviate (Vector Store)
    WEAVIATE_URL: str = os.getenv("WEAVIATE_URL", "http://localhost:8082")
    
    # OPA (Policy Engine)
    OPA_URL: str = os.getenv("OPA_URL", "http://localhost:8181")
    
    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://localhost:5432/enterprise_crm")

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
    DLQ_RETRY_BACKOFF_SECONDS: float = float(os.getenv("DLQ_RETRY_BACKOFF_SECONDS", "1.0"))


settings = Settings()
