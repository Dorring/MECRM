"""
Anomaly Detector - Detects operational anomalies from observability signals.

Monitors for latency spikes, error storms, consumer lag, and DB contention.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import structlog
from opentelemetry import trace

logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


class AnomalyType(str, Enum):
    LATENCY_SPIKE = "latency_spike"
    ERROR_STORM = "error_storm"
    KAFKA_LAG = "kafka_lag"
    DB_CONTENTION = "db_contention"
    MEMORY_PRESSURE = "memory_pressure"
    CPU_SATURATION = "cpu_saturation"
    CONNECTION_EXHAUSTION = "connection_exhaustion"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Anomaly:
    """Detected anomaly from observability signals."""
    anomaly_type: AnomalyType
    severity: Severity
    confidence: float
    metric_name: str
    current_value: float
    threshold: float
    affected_services: list[str]
    detected_at: str
    metadata: dict[str, Any]


# Threshold configurations (would typically come from config)
THRESHOLDS = {
    "latency_p99_ms": {"warning": 500, "critical": 2000},
    "error_rate_percent": {"warning": 1.0, "critical": 5.0},
    "kafka_lag_messages": {"warning": 1000, "critical": 10000},
    "db_query_time_ms": {"warning": 100, "critical": 500},
    "memory_percent": {"warning": 80, "critical": 95},
    "cpu_percent": {"warning": 80, "critical": 95},
    "connection_pool_percent": {"warning": 80, "critical": 95},
}


class AnomalyDetector:
    """Detects anomalies from observability metrics."""
    
    def __init__(self, thresholds: dict[str, dict[str, float]] | None = None):
        self._thresholds = thresholds or THRESHOLDS
    
    def detect_anomalies(self, metrics: dict[str, Any]) -> list[Anomaly]:
        """
        Analyze metrics and detect anomalies.
        
        Args:
            metrics: Dictionary of metric values from observability sources
        
        Returns:
            List of detected anomalies
        """
        with tracer.start_as_current_span("anomaly_detector.detect"):
            anomalies = []
            now = datetime.now(timezone.utc).isoformat()
            
            # Check latency
            latency_anomaly = self._check_latency(metrics, now)
            if latency_anomaly:
                anomalies.append(latency_anomaly)
            
            # Check error rates
            error_anomaly = self._check_error_rate(metrics, now)
            if error_anomaly:
                anomalies.append(error_anomaly)
            
            # Check Kafka lag
            lag_anomaly = self._check_kafka_lag(metrics, now)
            if lag_anomaly:
                anomalies.append(lag_anomaly)
            
            # Check database
            db_anomaly = self._check_db_contention(metrics, now)
            if db_anomaly:
                anomalies.append(db_anomaly)
            
            # Check resource usage
            resource_anomalies = self._check_resource_usage(metrics, now)
            anomalies.extend(resource_anomalies)
            
            logger.info("Anomaly detection complete", anomaly_count=len(anomalies))
            return anomalies
    
    def _check_latency(self, metrics: dict[str, Any], now: str) -> Anomaly | None:
        """Check for latency spikes."""
        latency = metrics.get("latency_p99_ms")
        if latency is None:
            return None
        
        thresholds = self._thresholds.get("latency_p99_ms", {})
        critical = thresholds.get("critical", 2000)
        warning = thresholds.get("warning", 500)
        
        if latency >= critical:
            return Anomaly(
                anomaly_type=AnomalyType.LATENCY_SPIKE,
                severity=Severity.CRITICAL,
                confidence=0.9,
                metric_name="latency_p99_ms",
                current_value=latency,
                threshold=critical,
                affected_services=list(metrics.get("slow_services", [])),
                detected_at=now,
                metadata={"percentile": "p99"},
            )
        elif latency >= warning:
            return Anomaly(
                anomaly_type=AnomalyType.LATENCY_SPIKE,
                severity=Severity.MEDIUM,
                confidence=0.75,
                metric_name="latency_p99_ms",
                current_value=latency,
                threshold=warning,
                affected_services=list(metrics.get("slow_services", [])),
                detected_at=now,
                metadata={"percentile": "p99"},
            )
        return None
    
    def _check_error_rate(self, metrics: dict[str, Any], now: str) -> Anomaly | None:
        """Check for error rate spikes."""
        error_rate = metrics.get("error_rate_percent")
        if error_rate is None:
            return None
        
        thresholds = self._thresholds.get("error_rate_percent", {})
        critical = thresholds.get("critical", 5.0)
        warning = thresholds.get("warning", 1.0)
        
        if error_rate >= critical:
            return Anomaly(
                anomaly_type=AnomalyType.ERROR_STORM,
                severity=Severity.CRITICAL,
                confidence=0.95,
                metric_name="error_rate_percent",
                current_value=error_rate,
                threshold=critical,
                affected_services=list(metrics.get("error_services", [])),
                detected_at=now,
                metadata={"top_errors": metrics.get("top_errors", [])[:5]},
            )
        elif error_rate >= warning:
            return Anomaly(
                anomaly_type=AnomalyType.ERROR_STORM,
                severity=Severity.MEDIUM,
                confidence=0.8,
                metric_name="error_rate_percent",
                current_value=error_rate,
                threshold=warning,
                affected_services=list(metrics.get("error_services", [])),
                detected_at=now,
                metadata={"top_errors": metrics.get("top_errors", [])[:5]},
            )
        return None
    
    def _check_kafka_lag(self, metrics: dict[str, Any], now: str) -> Anomaly | None:
        """Check for Kafka consumer lag."""
        lag = metrics.get("kafka_lag_messages")
        if lag is None:
            return None
        
        thresholds = self._thresholds.get("kafka_lag_messages", {})
        critical = thresholds.get("critical", 10000)
        warning = thresholds.get("warning", 1000)
        
        if lag >= critical:
            return Anomaly(
                anomaly_type=AnomalyType.KAFKA_LAG,
                severity=Severity.HIGH,
                confidence=0.9,
                metric_name="kafka_lag_messages",
                current_value=lag,
                threshold=critical,
                affected_services=list(metrics.get("lagging_consumers", [])),
                detected_at=now,
                metadata={
                    "consumer_groups": metrics.get("lagging_consumer_groups", []),
                    "lag_by_topic": metrics.get("lag_by_topic", {}),
                },
            )
        elif lag >= warning:
            return Anomaly(
                anomaly_type=AnomalyType.KAFKA_LAG,
                severity=Severity.MEDIUM,
                confidence=0.8,
                metric_name="kafka_lag_messages",
                current_value=lag,
                threshold=warning,
                affected_services=list(metrics.get("lagging_consumers", [])),
                detected_at=now,
                metadata={},
            )
        return None
    
    def _check_db_contention(self, metrics: dict[str, Any], now: str) -> Anomaly | None:
        """Check for database contention."""
        query_time = metrics.get("db_query_time_ms")
        if query_time is None:
            return None
        
        thresholds = self._thresholds.get("db_query_time_ms", {})
        critical = thresholds.get("critical", 500)
        warning = thresholds.get("warning", 100)
        
        if query_time >= critical:
            return Anomaly(
                anomaly_type=AnomalyType.DB_CONTENTION,
                severity=Severity.HIGH,
                confidence=0.85,
                metric_name="db_query_time_ms",
                current_value=query_time,
                threshold=critical,
                affected_services=["database"],
                detected_at=now,
                metadata={
                    "slow_queries": metrics.get("slow_queries", [])[:5],
                    "active_connections": metrics.get("db_connections"),
                },
            )
        elif query_time >= warning:
            return Anomaly(
                anomaly_type=AnomalyType.DB_CONTENTION,
                severity=Severity.MEDIUM,
                confidence=0.7,
                metric_name="db_query_time_ms",
                current_value=query_time,
                threshold=warning,
                affected_services=["database"],
                detected_at=now,
                metadata={},
            )
        return None
    
    def _check_resource_usage(self, metrics: dict[str, Any], now: str) -> list[Anomaly]:
        """Check for resource exhaustion."""
        anomalies = []
        
        # Memory
        memory = metrics.get("memory_percent")
        if memory is not None:
            thresholds = self._thresholds.get("memory_percent", {})
            if memory >= thresholds.get("critical", 95):
                anomalies.append(Anomaly(
                    anomaly_type=AnomalyType.MEMORY_PRESSURE,
                    severity=Severity.CRITICAL,
                    confidence=0.95,
                    metric_name="memory_percent",
                    current_value=memory,
                    threshold=thresholds.get("critical", 95),
                    affected_services=list(metrics.get("high_memory_services", [])),
                    detected_at=now,
                    metadata={},
                ))
            elif memory >= thresholds.get("warning", 80):
                anomalies.append(Anomaly(
                    anomaly_type=AnomalyType.MEMORY_PRESSURE,
                    severity=Severity.MEDIUM,
                    confidence=0.8,
                    metric_name="memory_percent",
                    current_value=memory,
                    threshold=thresholds.get("warning", 80),
                    affected_services=list(metrics.get("high_memory_services", [])),
                    detected_at=now,
                    metadata={},
                ))
        
        # CPU
        cpu = metrics.get("cpu_percent")
        if cpu is not None:
            thresholds = self._thresholds.get("cpu_percent", {})
            if cpu >= thresholds.get("critical", 95):
                anomalies.append(Anomaly(
                    anomaly_type=AnomalyType.CPU_SATURATION,
                    severity=Severity.CRITICAL,
                    confidence=0.95,
                    metric_name="cpu_percent",
                    current_value=cpu,
                    threshold=thresholds.get("critical", 95),
                    affected_services=list(metrics.get("high_cpu_services", [])),
                    detected_at=now,
                    metadata={},
                ))
            elif cpu >= thresholds.get("warning", 80):
                anomalies.append(Anomaly(
                    anomaly_type=AnomalyType.CPU_SATURATION,
                    severity=Severity.MEDIUM,
                    confidence=0.8,
                    metric_name="cpu_percent",
                    current_value=cpu,
                    threshold=thresholds.get("warning", 80),
                    affected_services=list(metrics.get("high_cpu_services", [])),
                    detected_at=now,
                    metadata={},
                ))
        
        return anomalies
